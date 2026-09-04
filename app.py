import streamlit as st
import sqlite3
import hashlib
import re
import json
import requests
from datetime import datetime, timedelta, timezone, time as dtime

# --- 0. TIMEZONE HANDLING ---
# The crew portal logs every time in the LOCAL time of the airport where the
# event happens: check-in/departure in ORIGIN local time, arrival/check-out in
# DESTINATION local time. Naively subtracting them over/under-counts block
# hours (e.g. MEL -> CMB looked like 6h instead of ~10h30). We keep the local
# stamps as-is (they drive the calendar and the FAU-sheet allowance math such
# as overnights/meals) and ALSO compute a UTC twin of each stamp so elapsed
# time math (block hours, acting hours) is exact across time zones.
try:
    from zoneinfo import ZoneInfo
except ImportError:          # Python < 3.9 fallback
    ZoneInfo = None

AIRPORT_TZ = {               # IATA -> IANA timezone (DST-aware)
    "CMB": "Asia/Colombo", "MAA": "Asia/Kolkata", "DEL": "Asia/Kolkata",
    "BOM": "Asia/Kolkata", "BLR": "Asia/Kolkata", "HYD": "Asia/Kolkata",
    "CCU": "Asia/Kolkata", "COK": "Asia/Kolkata", "TRV": "Asia/Kolkata",
    "TRZ": "Asia/Kolkata", "MLE": "Indian/Maldives", "GAN": "Indian/Maldives",
    "KHI": "Asia/Karachi", "LHE": "Asia/Karachi", "DAC": "Asia/Dhaka",
    "DXB": "Asia/Dubai", "AUH": "Asia/Dubai", "DOH": "Asia/Qatar",
    "BAH": "Asia/Bahrain", "DMM": "Asia/Riyadh", "RUH": "Asia/Riyadh",
    "JED": "Asia/Riyadh", "KWI": "Asia/Kuwait", "MCT": "Asia/Muscat",
    "SIN": "Asia/Singapore", "KUL": "Asia/Kuala_Lumpur", "BKK": "Asia/Bangkok",
    "CGK": "Asia/Jakarta", "HKG": "Asia/Hong_Kong", "CAN": "Asia/Shanghai",
    "PVG": "Asia/Shanghai", "PEK": "Asia/Shanghai", "ICN": "Asia/Seoul",
    "NRT": "Asia/Tokyo", "KIX": "Asia/Tokyo", "IST": "Europe/Istanbul",
    "LHR": "Europe/London", "CDG": "Europe/Paris", "FRA": "Europe/Berlin",
    "ZRH": "Europe/Zurich", "SYD": "Australia/Sydney", "MEL": "Australia/Melbourne",
    "SEZ": "Indian/Mahe",
}

AIRPORT_OFFSET_H = {         # fixed UTC offsets — fallback if zoneinfo missing
    "CMB": 5.5, "MAA": 5.5, "DEL": 5.5, "BOM": 5.5, "BLR": 5.5, "HYD": 5.5,
    "CCU": 5.5, "COK": 5.5, "TRV": 5.5, "TRZ": 5.5, "MLE": 5.0, "GAN": 5.0,
    "KHI": 5.0, "LHE": 5.0, "DAC": 6.0, "DXB": 4.0, "AUH": 4.0, "DOH": 3.0,
    "BAH": 3.0, "DMM": 3.0, "RUH": 3.0, "JED": 3.0, "KWI": 3.0, "MCT": 4.0,
    "SIN": 8.0, "KUL": 8.0, "BKK": 7.0, "CGK": 7.0, "HKG": 8.0, "CAN": 8.0,
    "PVG": 8.0, "PEK": 8.0, "ICN": 9.0, "NRT": 9.0, "KIX": 9.0, "IST": 3.0,
    "LHR": 0.0, "CDG": 1.0, "FRA": 1.0, "ZRH": 1.0, "SYD": 10.0, "MEL": 10.0,
    "SEZ": 4.0,
}

def to_utc(dt, iata):
    """Convert a naive local datetime at airport `iata` to a naive UTC datetime."""
    if dt is None or dt.tzinfo is not None:
        return dt
    code = (iata or "").upper()
    if ZoneInfo is not None:
        try:
            tz = ZoneInfo(AIRPORT_TZ.get(code, "Asia/Colombo"))
            return dt.replace(tzinfo=tz).astimezone(timezone.utc).replace(tzinfo=None)
        except Exception:
            pass
    off = AIRPORT_OFFSET_H.get(code, 5.5)
    return dt - timedelta(hours=off)

# --- 1. DATABASE SETUP ---
def init_db():
    conn = sqlite3.connect('crew_companion.db')
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS users (
            username TEXT PRIMARY KEY,
            password TEXT,
            full_name TEXT,
            rank TEXT
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS rosters (
            username TEXT,
            roster_text TEXT,
            PRIMARY KEY (username)
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS profiles (
            username TEXT PRIMARY KEY,
            data TEXT
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS performed_rosters (
            username TEXT PRIMARY KEY,
            roster_text TEXT
        )
    ''')
    conn.commit()
    conn.close()

def make_hash(password):
    return hashlib.sha256(str.encode(password)).hexdigest()

def check_hash(password, hashed_text):
    return make_hash(password) == hashed_text

def add_user(username, password, full_name, rank):
    conn = sqlite3.connect('crew_companion.db')
    c = conn.cursor()
    try:
        c.execute('INSERT INTO users(username, password, full_name, rank) VALUES (?, ?, ?, ?)',
                  (username, make_hash(password), full_name, rank))
        conn.commit()
        conn.close()
        return True
    except sqlite3.IntegrityError:
        conn.close()
        return False

def login_user(username, password):
    conn = sqlite3.connect('crew_companion.db')
    c = conn.cursor()
    c.execute('SELECT * FROM users WHERE username = ?', (username,))
    data = c.fetchone()
    conn.close()
    if data and check_hash(password, data[1]):
        return data
    return None

def save_roster_to_db(username, text):
    conn = sqlite3.connect('crew_companion.db')
    c = conn.cursor()
    c.execute('REPLACE INTO rosters (username, roster_text) VALUES (?, ?)', (username, text))
    conn.commit()
    conn.close()

def save_performed_roster(username, text):
    conn = sqlite3.connect('crew_companion.db')
    c = conn.cursor()
    c.execute('REPLACE INTO performed_rosters (username, roster_text) VALUES (?, ?)', (username, text))
    conn.commit()
    conn.close()

def load_performed_roster(username):
    conn = sqlite3.connect('crew_companion.db')
    c = conn.cursor()
    c.execute('SELECT roster_text FROM performed_rosters WHERE username = ?', (username,))
    d = c.fetchone()
    conn.close()
    return d[0] if d else ''

def save_profile(username, data):
    conn = sqlite3.connect('crew_companion.db')
    c = conn.cursor()
    c.execute('REPLACE INTO profiles (username, data) VALUES (?, ?)', (username, json.dumps(data)))
    conn.commit()
    conn.close()

def load_profile(username):
    conn = sqlite3.connect('crew_companion.db')
    c = conn.cursor()
    c.execute('SELECT data FROM profiles WHERE username = ?', (username,))
    d = c.fetchone()
    conn.close()
    try:
        return json.loads(d[0]) if d else {}
    except Exception:
        return {}

def load_roster_from_db(username):
    conn = sqlite3.connect('crew_companion.db')
    c = conn.cursor()
    c.execute('SELECT roster_text FROM rosters WHERE username = ?', (username,))
    data = c.fetchone()
    conn.close()
    return data[0] if data else ""

# --- 2. ROBUST ROSTER PARSER ---
ROSTER_ANCHOR = datetime(2026, 10, 4).date()   # known roster period start (04Oct26-01Nov26)
ROSTER_PERIOD_DAYS = 28

def roster_period_bounds(d):
    """Return (start, end) of the 28-day roster period containing date d,
    derived from the known anchor period. Works for past and future periods."""
    k = (d - ROSTER_ANCHOR).days // ROSTER_PERIOD_DAYS
    start = ROSTER_ANCHOR + timedelta(days=ROSTER_PERIOD_DAYS * k)
    # Airline convention: the period label ends on the NEXT period's start
    # day (e.g. 04 Oct - 01 Nov), so the displayed span is 28 days + 1.
    return start, start + timedelta(days=ROSTER_PERIOD_DAYS)

def preprocess_roster_text(raw_text):
    """
    Crew-portal exports come in two shapes:
      * TAB-separated (one duty per line, header + rows) — already structured,
        must NOT be re-split or the check-in stamp gets glued onto the next
        activity cell and rows are lost.
      * One long concatenated string (no tabs) — split into one duty per line
        before HTL blocks and before any 'DDMMMYY HH:MM' stamp that directly
        starts a UL / SB / OFF duty.
    """
    t = raw_text.replace("\r", "\n")
    if "\t" in t:
        return t
    t = re.sub(r'[ \t]*HTL', '\nHTL', t)
    t = re.sub(r'(\d{2}[A-Z]{3}\d{2}[ \t]*\d{2}:\d{2})[ \t]*(?=UL\s*\d|SB|OFF|ROF|TOF)', r'\n\1', t)
    return t

# Training / recurrent / ground-duty codes that count as a DUTY DAY (not a
# day off): SEP, SEC, CRM, DGR, F/A, OBT, SPC, SVC, GNT, CBT, CDE, CSE, CFD,
# CSS, PEF, LSW, BCT, CSW, SER, DFT, ICT, CMW, CSP, CMP, CSC, DLV, OFG, DTL.
DUTY_CODES = ["SEP", "SEC", "CRM", "DGR", "F/A", "OBT", "SPC", "SVC", "GNT", "CBT",
              "CDE", "CSE", "CFD", "CSS", "PEF", "LSW", "BCT", "CSW", "SER", "DFT",
              "ICT", "CMW", "CSP", "CMP", "CSC", "DLV", "OFG", "DTL"]
_DUTY_PAT = "|".join(re.escape(c) for c in DUTY_CODES)


def _parse_tab_line(line_str):
    """Parse one tab-separated duty line from the crew-portal export.

    The portal's grid layout differs between pages and even between row types:
      * performed page:  Activity | Checkin | Start | Dep | Arr | End | Checkout
      * roster page:     (blank)  | Checkin | Activity | Start | Dep | Arr | End | Checkout
    and ground duties (OFF/HTL/ROF/TOF/SB) merge the Checkin/Activity cells so
    the activity code shifts left by one or two columns.  Parsing by CONTENT
    (activity token, datetime stamps, IATA codes) instead of fixed column
    numbers handles every layout — including return legs whose Check-In cell
    is empty.  Returns a parsed-row dict, or None if the line isn't a duty."""
    fields = line_str.split("\t")

    # 1) locate the activity token
    act_idx = act = None
    for i, f in enumerate(fields):
        s = f.strip()
        if re.match(r'^(UL\s*\d{1,4}|HTL|OFF|ROF|TOF|SB\d*|ALV|RLV|ALP|CLV|' + _DUTY_PAT + r')$', s):
            act_idx, act = i, s.upper()
            break
    if act is None:
        return None

    # 2) datetime stamps, in column order
    dts = []
    for i, f in enumerate(fields):
        s = f.strip()
        if re.match(r'^\d{2}[A-Z]{3}\d{2}\s+\d{2}:\d{2}$', s):
            try:
                dts.append((i, datetime.strptime(s, "%d%b%y %H:%M")))
            except ValueError:
                pass

    # 3) IATA codes (exactly 3 uppercase letters) after the activity cell
    iatas = [(i, f.strip()) for i, f in enumerate(fields)
             if i > act_idx and re.match(r'^[A-Z]{3}$', f.strip())]

    ci_dt = dep_dt = arr_dt = co_dt = None
    dep_iata = arr_iata = None
    flight_no = "-"

    m = re.match(r'^UL\s*(\d{1,4})$', act)
    if m:
        atype, code = "FLIGHT", f"UL{m.group(1)}"
        flight_no = f"UL {m.group(1)}"
        dep_iata = iatas[0][1] if iatas else "CMB"
        arr_iata = iatas[1][1] if len(iatas) > 1 else dep_iata
        dep_idx = iatas[0][0] if iatas else None
        arr_idx = iatas[1][0] if len(iatas) > 1 else None
        before_dep = [d for (i, d) in dts if dep_idx is not None and i < dep_idx]
        after_arr = [d for (i, d) in dts if arr_idx is not None and i > arr_idx]
        dep_dt = before_dep[-1] if before_dep else None
        if after_arr:
            arr_dt = after_arr[0]
            if len(after_arr) > 1:
                co_dt = after_arr[1]
        ci_dt = next((d for (i, d) in dts if dep_dt and d < dep_dt), None)
    elif act == "HTL":
        atype, code = "LAYOVER", "HTL"
        station = iatas[0][1] if iatas else "-"
        dep_iata = arr_iata = station
        if dts:
            dep_dt, arr_dt = dts[0][1], dts[-1][1]
        ci_dt, co_dt = dep_dt, arr_dt
    elif act in ("OFF", "ROF", "TOF"):
        atype, code = "DAY OFF", act
        if dts:
            dep_dt, arr_dt = dts[0][1], dts[-1][1]
    elif re.match(r'^SB\d*$', act):
        atype, code = "STANDBY", act
        if len(dts) >= 4:
            ci_dt, dep_dt, arr_dt, co_dt = dts[0][1], dts[1][1], dts[2][1], dts[3][1]
        elif len(dts) == 3:
            ci_dt = dep_dt = dts[0][1]
            arr_dt, co_dt = dts[1][1], dts[2][1]
        elif dts:
            ci_dt = dep_dt = dts[0][1]
            arr_dt = co_dt = dts[-1][1]
    elif act in ("ALV", "RLV", "ALP", "CLV"):
        atype, code = "LEAVE", act
        if dts:
            dep_dt, arr_dt = dts[0][1], dts[-1][1]
    elif act in DUTY_CODES:
        # training/duty day — carries check-in, start, end & check-out stamps
        atype, code = "DUTY", act
        if len(dts) >= 4:
            ci_dt, dep_dt, arr_dt, co_dt = dts[0][1], dts[1][1], dts[2][1], dts[3][1]
        elif len(dts) == 3:
            ci_dt = dep_dt = dts[0][1]
            arr_dt, co_dt = dts[1][1], dts[2][1]
        elif dts:
            ci_dt = dep_dt = dts[0][1]
            arr_dt = co_dt = dts[-1][1]
    else:
        return None

    # route / station & timezone origin/destination
    if atype == "FLIGHT":
        route = f"{dep_iata} ➔ {arr_iata}" if dep_iata and arr_iata else "-"
        origin_iata, dest_iata = (dep_iata or "CMB"), (arr_iata or "CMB")
    elif atype == "LAYOVER":
        route = dep_iata or "-"
        origin_iata = dest_iata = route if route != "-" else "CMB"
    else:
        route, origin_iata, dest_iata = "-", "CMB", "CMB"

    # calendar dates (flight chips sit on the DEPARTURE date)
    if atype == "FLIGHT":
        anchor = dep_dt or arr_dt
        row_dt_obj = datetime.combine(anchor.date(), datetime.min.time()) if anchor else None
        end_dt_obj = None
    else:
        start_dt = ci_dt or dep_dt
        end_dt = co_dt or arr_dt
        row_dt_obj = datetime.combine(start_dt.date(), datetime.min.time()) if start_dt else None
        end_dt_obj = (datetime.combine(end_dt.date(), datetime.min.time())
                      if end_dt and start_dt and end_dt.date() > start_dt.date() else None)
    row_date_str = row_dt_obj.strftime("%d%b%y").upper() if row_dt_obj else "-"

    def hm(dt_):
        return dt_.strftime("%H:%M") if dt_ else "-"

    ci_u = to_utc(ci_dt, origin_iata)
    dep_u = to_utc(dep_dt, origin_iata)
    arr_u = to_utc(arr_dt, dest_iata)
    co_u = to_utc(co_dt, dest_iata)

    return {
        "Date": row_date_str,
        "DateObj": row_dt_obj,
        "EndDateObj": end_dt_obj,
        "Type": atype,
        "Code": code,
        "Flight / Code": flight_no if atype == "FLIGHT" else atype,
        "Check-In": hm(ci_dt) if atype in ("FLIGHT", "STANDBY", "DUTY") else "-",
        "Departure": hm(dep_dt),
        "Route": route,
        "Arrival": hm(arr_dt),
        "Checkout": hm(co_dt) if atype in ("FLIGHT", "STANDBY", "DUTY") else "-",
        "Aircraft": "-",
        "CIdt": ci_dt, "DEPdt": dep_dt, "ARRdt": arr_dt, "COdt": co_dt,
        "CIdt_u": ci_u, "DEPdt_u": dep_u, "ARRdt_u": arr_u, "COdt_u": co_u,
    }


def parse_roster_text(raw_text):
    lines = preprocess_roster_text(raw_text).split('\n')
    parsed_rows = []
    current_date_str = "-"
    current_dt_obj = None

    for line in lines:
        line_str = line.strip()
        if not line_str:
            continue

        # Fast path: tab-separated portal export (one duty per line, columns
        # Activity / Checkin / Start / Dep / Arr / End / Checkout / ...).
        # Column-based parsing keeps return legs (empty Check-In) correct.
        if "\t" in line_str:
            row = _parse_tab_line(line_str)
            if row:
                parsed_rows.append(row)
            continue

        date_match = re.search(r'^(\d{2}[A-Z]{3}\d{2})', line_str)
        if date_match:
            current_date_str = date_match.group(1)
            try:
                current_dt_obj = datetime.strptime(current_date_str, "%d%b%y")
            except ValueError:
                pass

        # Every full 'DDMMMYY HH:MM' stamp on the line (portal format)
        dt_stamps = []
        for ds, ts in re.findall(r'(\d{2}[A-Z]{3}\d{2})\s*(\d{2}:\d{2})', line_str):
            try:
                dt_stamps.append(datetime.strptime(ds + ts, "%d%b%y%H:%M"))
            except ValueError:
                pass

        line_date_match = re.search(r'(\d{2}[A-Z]{3}\d{2})', line_str)
        row_dt_obj = current_dt_obj
        row_date_str = current_date_str
        if line_date_match:
            try:
                parsed_line_date = datetime.strptime(line_date_match.group(1), "%d%b%y")
                row_dt_obj = parsed_line_date
                row_date_str = line_date_match.group(1)
            except ValueError:
                pass

        if any(keyword in line_str for keyword in ["UL", "OFF", "HTL", "SB", "ROF", "TOF", "ALV", "RLV", "ALP", "CLV"]):
            activity_type = "OTHER"
            flight_no = "-"
            checkin_time = "-"
            dep_time = "-"
            route = "-"
            arr_time = "-"
            checkout_time = "-"
            ac_type = "-"
            end_dt_obj = None

            time_matches = re.findall(r'(\d{2}:\d{2})', line_str)

            if "OFF" in line_str or "ROF" in line_str or "TOF" in line_str:
                activity_type = "DAY OFF"
            elif any(c in line_str for c in ("ALV", "RLV", "ALP", "CLV")):
                activity_type = "LEAVE"
            elif "HTL" in line_str:
                activity_type = "LAYOVER"
            elif "SB" in line_str:
                activity_type = "STANDBY"
            elif "UL" in line_str:
                activity_type = "FLIGHT"
                # Bounded so 'UL60622SEP26' parses as UL606 + date, not UL60622
                m = re.search(r'UL\s*(\d{1,4}?)(?=\d{2}[A-Z]{3}\d{2}|\D|$)', line_str)
                if m:
                    flight_no = f"UL {m.group(1)}"

            # Raw activity code — needed for CLV exclusion & acting-duty marking
            if activity_type == "FLIGHT":
                code = flight_no.replace(" ", "")
            elif activity_type == "LAYOVER":
                code = "HTL"
            elif activity_type == "DAY OFF":
                code = next((c for c in ("ROF", "TOF", "OFF") if c in line_str), "OFF")
            elif activity_type == "STANDBY":
                m = re.search(r'\bSB\d*\b', line_str)
                code = m.group(0) if m else "SB"
            elif activity_type == "LEAVE":
                m = re.search(r'\b(ALV|RLV|ALP|CLV)\b', line_str)
                code = m.group(1) if m else "LEAVE"
            else:
                code = "OTHER"

            # Multi-day span support (layover/standby/off blocks with start+end stamps)
            if dt_stamps:
                start_dt, end_dt = min(dt_stamps), max(dt_stamps)
                if activity_type in ("LAYOVER", "STANDBY", "DAY OFF"):
                    row_dt_obj = datetime.combine(start_dt.date(), datetime.min.time())
                    row_date_str = start_dt.strftime("%d%b%y").upper()
                    if end_dt.date() > start_dt.date():
                        end_dt_obj = datetime.combine(end_dt.date(), datetime.min.time())
                elif activity_type == "FLIGHT" and len(dt_stamps) >= 2:
                    # Calendar chip belongs on the DEPARTURE date (2nd stamp),
                    # not the check-in date (1st stamp, may be previous evening)
                    dep_date = dt_stamps[1].date()
                    row_dt_obj = datetime.combine(dep_date, datetime.min.time())
                    row_date_str = dep_date.strftime("%d%b%y").upper()

            # Route: spaced 'CMB SYD' or concatenated 'CMBSYD'
            route_match = re.search(r'([A-Z]{3})\s+([A-Z]{3})', line_str)
            if not route_match:
                stripped = re.sub(r'\d{2}[A-Z]{3}\d{2}', ' ', line_str)
                route_match = re.search(r'(?<![A-Z])([A-Z]{3})([A-Z]{3})(?![A-Z])', stripped)
            if activity_type == "LAYOVER":
                stripped = re.sub(r'\d{2}[A-Z]{3}\d{2}', ' ', line_str)
                toks = [t for t in re.findall(r'[A-Z]{3}', stripped) if t != "HTL"]
                route = toks[0] if toks else "-"
            elif route_match:
                route = f"{route_match.group(1)} ➔ {route_match.group(2)}"

            if time_matches:
                if len(time_matches) >= 4:
                    checkin_time = time_matches[0]
                    dep_time = time_matches[1]
                    arr_time = time_matches[2]
                    checkout_time = time_matches[3]
                elif len(time_matches) == 3:
                    checkin_time = time_matches[0]
                    dep_time = time_matches[1]
                    arr_time = time_matches[2]
                elif len(time_matches) == 2:
                    checkin_time = time_matches[0]
                    dep_time = time_matches[0]
                    arr_time = time_matches[1]
                elif len(time_matches) == 1:
                    dep_time = time_matches[0]

            parts = line_str.split()
            for p in parts:
                if len(p) == 3 and p.isalnum() and p not in ["FA", "J28", "CMB", "CAN", "BKK", "TRZ", "MAA", "MLE", "DMM", "BLR", "DXB", "RUH", "LHE", "ICN", "DEL", "PUR"]:
                    ac_type = p

            # Exact datetimes for the rules engine (portal stamps preferred,
            # heuristic day-rollover reconstruction for spaced rosters)
            ci_dt = dep_dt = arr_dt = co_dt = None
            if activity_type == "FLIGHT":
                if len(dt_stamps) >= 4:
                    ci_dt, dep_dt, arr_dt, co_dt = dt_stamps[0], dt_stamps[1], dt_stamps[2], dt_stamps[3]
                elif len(dt_stamps) == 3:
                    ci_dt, dep_dt, arr_dt = dt_stamps[0], dt_stamps[1], dt_stamps[2]
                elif row_dt_obj is not None:
                    bd = row_dt_obj.date()
                    def _t(s):
                        try:
                            return datetime.strptime(s, "%H:%M").time()
                        except ValueError:
                            return None
                    tci, tdep, tarr, tco = _t(checkin_time), _t(dep_time), _t(arr_time), _t(checkout_time)
                    if tdep:
                        dep_dt = datetime.combine(bd, tdep)
                        if tci:
                            ci_dt = datetime.combine(bd, tci)
                            if ci_dt > dep_dt:
                                ci_dt -= timedelta(days=1)
                        if tarr:
                            arr_dt = datetime.combine(bd, tarr)
                            if arr_dt < dep_dt:
                                arr_dt += timedelta(days=1)
                        if tco and arr_dt:
                            co_dt = datetime.combine(arr_dt.date(), tco)
                            if co_dt < arr_dt:
                                co_dt += timedelta(days=1)
            elif activity_type in ("STANDBY", "LAYOVER") and dt_stamps:
                ci_dt, co_dt = min(dt_stamps), max(dt_stamps)

            # --- timezone normalization ---
            # Portal logs check-in/departure in ORIGIN local time and
            # arrival/check-out in DESTINATION local time. The LOCAL stamps are
            # kept untouched for calendar-date / allowance math (overnights &
            # meals follow the same convention as the FAU sheet). UTC twins are
            # computed alongside for true elapsed-time math (block hours).
            if activity_type == "LAYOVER":
                stn = route if route not in (None, "-") else "CMB"
                origin_iata = dest_iata = stn
            elif activity_type == "STANDBY":
                origin_iata = dest_iata = "CMB"
            elif route and "➔" in route:
                p = [x.strip() for x in route.split("➔")]
                origin_iata, dest_iata = (p + ["CMB", "CMB"])[:2]
            else:
                origin_iata = dest_iata = "CMB"
            ci_u = to_utc(ci_dt, origin_iata)
            dep_u = to_utc(dep_dt, origin_iata)
            arr_u = to_utc(arr_dt, dest_iata)
            co_u = to_utc(co_dt, dest_iata)

            parsed_rows.append({
                "Date": row_date_str,
                "DateObj": row_dt_obj,
                "EndDateObj": end_dt_obj,
                "Type": activity_type,
                "Code": code,
                "Flight / Code": flight_no if flight_no != "-" else activity_type,
                "Check-In": checkin_time,
                "Departure": dep_time,
                "Route": route,
                "Arrival": arr_time,
                "Checkout": checkout_time,
                "Aircraft": ac_type,
                "CIdt": ci_dt, "DEPdt": dep_dt, "ARRdt": arr_dt, "COdt": co_dt,
                "CIdt_u": ci_u, "DEPdt_u": dep_u, "ARRdt_u": arr_u, "COdt_u": co_u
            })

    return parsed_rows

# --- 2.5 FAU SOFT-RULES AUDIT ENGINE (Roster Guardian) ---
LONGHAUL_2OFF_LAYOVER = {"LHR", "FRA", "CDG", "FCO", "MXP", "NRT", "SYD", "MEL"}
TWO_OFF_TURNAROUND = {"JED"}
ONE_OFF_TURNAROUND = {"DOH", "BAH", "DMM"}
SOUTHASIA_TA = {"DEL", "BOM", "KHI"}
MIDEAST_TA = {"DXB", "AUH", "MCT"}
SEASIA_MORNING_TA = {"SIN", "KUL", "CGK"}
SEASIA_MORNING_FLIGHTS = {"UL314", "UL364"}   # known SIN/KUL/CGK morning T/A
MIN_BASE_REST_H = 17.5          # 17h30m chocks-on -> next report at base
REGIONAL_MAX_SECTOR_H = 4.0     # 'regional' = sector length under 4 hours
SBY2_CODE, SBY4_CODE = "SB2", "SB4"   # standby codes the FAU rules explicitly allow
MIDEAST_STATIONS = {"DXB", "AUH", "MCT", "DOH", "BAH", "DMM", "RUH", "JED", "KWI"}

def _route_od(route):
    if route and "➔" in route:
        p = [x.strip() for x in route.split("➔")]
        if len(p) == 2:
            return p[0], p[1]
    return None, None

def build_duties(rows):
    """Group individual sectors into duty periods (gap at a station <= 4h)."""
    fl = sorted([r for r in rows if r["Type"] == "FLIGHT" and r.get("DEPdt") and r.get("ARRdt")],
                key=lambda r: r["DEPdt"])
    duties = []
    for r in fl:
        o, d = _route_od(r["Route"])
        if r.get("DEPdt_u") and r.get("ARRdt_u"):
            block_h = (r["ARRdt_u"] - r["DEPdt_u"]).total_seconds() / 3600
        else:
            block_h = (r["ARRdt"] - r["DEPdt"]).total_seconds() / 3600
        sec = {"flight": r["Flight / Code"], "o": o, "d": d, "dep": r["DEPdt"], "arr": r["ARRdt"],
               "ci": r.get("CIdt"), "block_h": block_h}
        if duties and (sec["dep"] - duties[-1]["chocks_on"]).total_seconds() <= 4 * 3600 \
                and duties[-1]["dest"] == o:
            duties[-1]["sectors"].append(sec)
            duties[-1]["chocks_on"] = sec["arr"]
            duties[-1]["dest"] = d
        else:
            duties.append({"report": sec["ci"] or (sec["dep"] - timedelta(hours=1, minutes=30)),
                           "sectors": [sec], "chocks_on": sec["arr"], "origin": o, "dest": d})
    for du in duties:
        du["n"] = len(du["sectors"])
        du["stations"] = [s["d"] for s in du["sectors"] if s["d"]]
        du["is_turnaround"] = du["origin"] == "CMB" and du["dest"] == "CMB" and du["n"] >= 2
        du["max_sector_h"] = max(s["block_h"] for s in du["sectors"])
        du["label"] = " / ".join(s["flight"].replace(" ", "") for s in du["sectors"])
        du["numbers"] = {s["flight"].replace(" ", "") for s in du["sectors"]}
    return duties

def audit_roster(rows):
    """Audit the parsed roster against FAU soft rules. Returns findings list
    of (severity, message) where severity is 'violation' or 'note'."""
    findings = []
    duties = build_duties(rows)

    # Report events: flight duties + standby starts + duty/training days.
    # A training/duty day counts as a GROUND DUTY (its start time is a report).
    events = [{"dt": du["report"], "kind": "FLIGHT", "duty": du, "label": du["label"]} for du in duties]
    for sb in [r for r in rows if r["Type"] == "STANDBY"]:
        sdt = sb.get("CIdt")
        if not sdt and sb["DateObj"] is not None and sb["Departure"] != "-":
            try:
                sdt = datetime.combine(sb["DateObj"].date(), datetime.strptime(sb["Departure"], "%H:%M").time())
            except ValueError:
                sdt = None
        if sdt:
            events.append({"dt": sdt, "kind": "STANDBY", "duty": None,
                           "label": f"Standby {sb.get('Code') or ''}".strip(), "code": sb.get("Code")})
    duty_rows = [r for r in rows if r["Type"] == "DUTY" and r["DateObj"] is not None]
    duty_days = {r["DateObj"].date(): (r.get("Code") or "DUTY") for r in duty_rows}
    for dr in duty_rows:
        sdt = dr.get("CIdt") or dr.get("DEPdt")
        if sdt:
            events.append({"dt": sdt, "kind": "DUTY", "duty": None,
                           "label": f"{dr.get('Code') or 'Duty'} (training)"})
    events.sort(key=lambda e: e["dt"])
    duty_day_map = {}
    for e in events:
        duty_day_map.setdefault(e["dt"].date(), []).append(e)

    def flight_next_day_rule(du, arr, rule_name, layover_after=None, layovers_banned_before_2300=False):
        """Next-day flight restrictions (DEL/BOM/KHI, four-sector, DXB/AUH/MCT).
        Turnarounds: no report before 23:00, regional-only in the 23:00–05:59
        window. Layovers: banned before 23:00 (South Asia) or before layover_after."""
        day1 = arr.date() + timedelta(days=1)
        day2 = arr.date() + timedelta(days=2)
        for e in duty_day_map.get(day1, []) + duty_day_map.get(day2, []):
            if e["kind"] != "FLIGHT":
                continue
            rep, nd = e["dt"], e["duty"]
            if rep.date() == day2 and rep.time() >= dtime(6, 0):
                continue  # anything allowed after 06:00 on the second day
            in_night = ((rep.date() == day1 and rep.time() >= dtime(23, 0)) or
                        (rep.date() == day2 and rep.time() <= dtime(5, 59)))
            if nd["is_turnaround"]:
                if rep.date() == day1 and rep.time() < dtime(23, 0):
                    findings.append(("violation",
                        f"{rule_name}: after {du['label']} (arr {arr:%d %b %H:%M}) no turnaround may report before 23:00 next day — {nd['label']} reports {rep:%d %b %H:%M}."))
                elif in_night and nd["max_sector_h"] >= REGIONAL_MAX_SECTOR_H:
                    findings.append(("violation",
                        f"{rule_name}: 23:00–05:59 allows only a regional flight (<{REGIONAL_MAX_SECTOR_H:.0f}h sector) — {nd['label']} ({nd['max_sector_h']:.1f}h) reports {rep:%d %b %H:%M}."))
            else:
                if layovers_banned_before_2300:
                    if rep.date() == day1 and rep.time() < dtime(23, 0):
                        findings.append(("violation",
                            f"{rule_name}: after {du['label']} (arr {arr:%d %b %H:%M}) no layover may report before 23:00 next day — {nd['label']} reports {rep:%d %b %H:%M}."))
                    elif in_night and nd["max_sector_h"] >= REGIONAL_MAX_SECTOR_H:
                        findings.append(("violation",
                            f"{rule_name}: 23:00–05:59 allows only a regional flight (<{REGIONAL_MAX_SECTOR_H:.0f}h sector) — {nd['label']} ({nd['max_sector_h']:.1f}h) reports {rep:%d %b %H:%M}."))
                elif layover_after:
                    if rep.date() == day1 and rep.time() < layover_after:
                        findings.append(("violation",
                            f"{rule_name}: a one-sector layover may only report after {layover_after:%H:%M} the following day — {nd['label']} reports {rep:%d %b %H:%M}."))

    def sb_next_day_rule(du, arr, rule_name, only_code):
        day1 = arr.date() + timedelta(days=1)
        for e in duty_day_map.get(day1, []):
            if e["kind"] == "STANDBY" and e.get("code") != only_code:
                findings.append(("violation",
                    f"{rule_name}: after {du['label']} the next-day standby must be {only_code} — {e.get('code') or 'SB'} rostered ({e['dt']:%d %b %H:%M})."))

    def require_days_off(arr, n_days, why):
        for k in range(1, n_days + 1):
            d = arr.date() + timedelta(days=k)
            for e in duty_day_map.get(d, []):
                if e["kind"] == "DUTY":
                    continue  # handled by the duty_days check below
                findings.append(("violation",
                    f"{why}: {arr.date():%d %b} arrival entitles arrival day + {n_days} day(s) off — but {e['label']} is rostered on {d:%d %b}."))
            if d in duty_days:
                findings.append(("violation",
                    f"{why}: {arr.date():%d %b} arrival entitles arrival day + {n_days} day(s) off — but a duty/training day ({duty_days[d]}) is rostered on {d:%d %b}."))

    for du in duties:
        arr = du["chocks_on"]
        nxt = [e for e in events if e["dt"] > arr]

        # R1 — 17h30 minimum rest at base: chocks-on → next report (flight,
        # standby OR duty/training report), never ground→ground.
        if du["dest"] == "CMB" and nxt:
            rest = (nxt[0]["dt"] - arr).total_seconds() / 3600
            if rest < MIN_BASE_REST_H:
                findings.append(("violation",
                    f"Min base rest: only {rest:.1f}h between {du['label']} chocks-on ({arr:%d %b %H:%M}) and next report ({nxt[0]['dt']:%d %b %H:%M}, {nxt[0]['label']}) — minimum is 17h30m."))

        if du["is_turnaround"]:
            hit_sa = set(du["stations"]) & SOUTHASIA_TA
            # R3 — DEL/BOM/KHI arriving 00:01–11:59 → 24h rest
            if hit_sa and arr.time() < dtime(12, 0) and nxt:
                rest = (nxt[0]["dt"] - arr).total_seconds() / 3600
                if rest < 24:
                    findings.append(("violation",
                        f"24h rest rule: {du['label']} ({'/'.join(sorted(hit_sa))}) arrived {arr:%d %b %H:%M} (before 12:00) — needs 24h to next report, got {rest:.1f}h ({nxt[0]['label']} at {nxt[0]['dt']:%d %b %H:%M})."))
            # R2 — DEL/BOM/KHI arriving 12:00–23:59 → next-day restrictions
            if hit_sa and arr.time() >= dtime(12, 0):
                flight_next_day_rule(du, arr, "DEL/BOM/KHI rule", layovers_banned_before_2300=True)
            # R5 — DXB/AUH/MCT turnarounds (any arrival time); UL231/232 is
            # the special case handled below, so it's excluded here.
            if set(du["stations"]) & MIDEAST_TA and not (du["numbers"] & {"UL231", "UL232"}):
                flight_next_day_rule(du, arr, "DXB/AUH/MCT rule", layover_after=dtime(16, 0))
                sb_next_day_rule(du, arr, "DXB/AUH/MCT rule", SBY4_CODE)
            # R6 — UL231/232 (DXB): next day only SBY2 (06:00–18:00) or a flight in that window
            if du["numbers"] & {"UL231", "UL232"}:
                d1 = arr.date() + timedelta(days=1)
                for e in duty_day_map.get(d1, []):
                    if e["kind"] == "FLIGHT" and not (dtime(6, 0) <= e["dt"].time() <= dtime(18, 0)):
                        findings.append(("violation",
                            f"UL231/232 rule: following-day flight must report within 06:00–18:00 — {e['label']} reports {e['dt']:%d %b %H:%M}."))
                    elif e["kind"] == "STANDBY" and e.get("code") != SBY2_CODE:
                        findings.append(("violation",
                            f"UL231/232 rule: following-day standby must be SBY2 — {e.get('code') or 'SB'} rostered ({e['dt']:%d %b %H:%M})."))
            # R8 — DOH/BAH/DMM turnaround: arrival day + 1 day off
            hit_1off = set(du["stations"]) & ONE_OFF_TURNAROUND
            if hit_1off:
                require_days_off(arr, 1, f"{'/'.join(sorted(hit_1off))} turnaround")
            # JED turnaround: arrival day + 2 days off
            hit_jed = set(du["stations"]) & TWO_OFF_TURNAROUND
            if hit_jed:
                require_days_off(arr, 2, "JED turnaround")
            # R9 — SIN/KUL/CGK morning turnaround → next-day flights after 18:00, or SBY4
            hit_sea = set(du["stations"]) & SEASIA_MORNING_TA
            s0 = du["sectors"][0]
            # 'morning' = a hardcoded morning flight number, or a CMB→SIN
            # departure between 07:00–08:00 (the old CMB–SIN morning T/A).
            cmb_sin_morning = (s0["o"] == "CMB" and s0["d"] == "SIN"
                               and isinstance(s0["dep"], datetime)
                               and dtime(7, 0) < s0["dep"].time() < dtime(8, 0))
            is_morning = bool(du["numbers"] & SEASIA_MORNING_FLIGHTS) or cmb_sin_morning
            if hit_sea and is_morning:
                d1 = arr.date() + timedelta(days=1)
                for e in duty_day_map.get(d1, []):
                    if e["kind"] == "FLIGHT" and e["dt"].time() < dtime(18, 0):
                        findings.append(("violation",
                            f"{'/'.join(sorted(hit_sea))} morning turnaround rule: next-day flights may only report after 18:00 — {e['label']} reports {e['dt']:%d %b %H:%M}."))
                    elif e["kind"] == "STANDBY" and e.get("code") != SBY4_CODE:
                        findings.append(("violation",
                            f"{'/'.join(sorted(hit_sea))} morning turnaround rule: next-day standby must be SBY4 — {e.get('code') or 'SB'} rostered ({e['dt']:%d %b %H:%M})."))
        # R4 — four-sector days arriving before 17:30
        if du["n"] >= 4 and arr.time() <= dtime(17, 30):
            flight_next_day_rule(du, arr, "Four-sector day rule", layover_after=dtime(18, 0))
            sb_next_day_rule(du, arr, "Four-sector day rule", SBY4_CODE)

    # R7 — long-haul layovers: on the RETURN leg's arrival at CMB, 2 days off
    for du in duties:
        if du["dest"] != "CMB":
            continue
        origins = {s["o"] for s in du["sectors"] if s["o"]}
        lh = origins & LONGHAUL_2OFF_LAYOVER
        if lh:
            require_days_off(du["chocks_on"], 2, f"{'/'.join(sorted(lh))} layover")

    # de-duplicate
    seen, out = set(), []
    for sev, msg in findings:
        if msg not in seen:
            seen.add(msg)
            out.append((sev, msg))
    return out

# --- 3. LIVE TELEMETRY: FLIGHTSTATS (CIRIUM) PRIMARY + FLIGHTRADAR24 FALLBACK ---
# Both sources are keyless with no hard quota. FlightStats carries true
# airline-filed disruption data (delays, cancellations, DIVERSIONS) that
# FR24's free feed often misses or papers over with synthetic estimates
# (any FR24 time ending in '*' is a historical-average guess, not real).
# Results cached for 10 minutes per flight to be polite and keep reruns fast.

FR24_URL = "https://api.flightradar24.com/common/v1/flight/list.json"
FR24_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/124.0 Safari/537.36"),
    "Accept": "application/json",
}

@st.cache_data(ttl=600, show_spinner=False)
def flightstats_fetch(carrier, number, year, month, day):
    """
    Scrape Cirium/FlightStats flight-tracker page: real airline-filed status
    (delayed / cancelled / diverted) embedded as JSON in the page. Keyless.
    Returns the 'flight' dict or None.
    """
    url = f"https://www.flightstats.com/v2/flight-tracker/{carrier}/{number}"
    params = {"year": year, "month": month, "date": day}
    resp = requests.get(url, params=params, headers=FR24_HEADERS, timeout=20)
    resp.raise_for_status()
    m = re.search(r'__NEXT_DATA__\s*=\s*(\{.*?\})\s*;?\s*</script>', resp.text, re.S)
    if not m:
        m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(\{.*?\})</script>', resp.text, re.S)
    if not m:
        return None
    raw = m.group(1).split(";__NEXT_LOADED_PAGES__")[0]
    data = json.loads(raw)
    flight = (data.get("props", {}).get("initialState", {})
                  .get("flightTracker", {}).get("flight")) or None
    # A valid result has a schedule block; an empty shell means no data for that date
    if flight and (flight.get("schedule") or {}).get("scheduledDeparture"):
        return flight
    return None

def _fs_minutes_between(sched_iso, actual_iso):
    try:
        s = datetime.fromisoformat(sched_iso.replace("Z", "+00:00"))
        a = datetime.fromisoformat(actual_iso.replace("Z", "+00:00"))
        return int(round((a - s).total_seconds() / 60))
    except Exception:
        return 0

def query_flightstats(flight_no, flight_date):
    """Primary check: airline-filed status from Cirium/FlightStats."""
    clean_fn = flight_no.replace(" ", "").upper()
    m = re.match(r'([A-Z0-9]{2})\s*(\d+)', clean_fn)
    if not m:
        return None
    carrier, number = m.group(1), m.group(2)
    try:
        fl = flightstats_fetch(carrier, number, flight_date.year, flight_date.month, flight_date.day)
    except Exception:
        return None
    if not fl:
        return None

    status = fl.get("status", {}) or {}
    schedule = fl.get("schedule", {}) or {}
    code = (status.get("statusCode") or "").upper()          # S/A/L/C/D/R...
    status_words = (status.get("status") or "").strip()      # e.g. "Diverted to CMB"
    final_status = (status.get("finalStatus") or "").strip() # e.g. "Diverted"
    color = (status.get("color") or "").lower()

    is_cancelled = code == "C" or "cancel" in status_words.lower()
    is_diverted = bool(status.get("diverted")) or code == "D" or "divert" in status_words.lower()

    # Departure delay: airline-filed minutes first, else computed from times
    delay_obj = ((status.get("delay") or {}).get("departure") or {})
    dep_delay = int(delay_obj.get("minutes") or 0)
    sched_utc = schedule.get("scheduledDepartureUTC")
    act_utc = schedule.get("estimatedActualDepartureUTC")
    if dep_delay == 0 and sched_utc and act_utc:
        dep_delay = max(dep_delay, _fs_minutes_between(sched_utc, act_utc))
    delay_wording = ((status.get("delayStatus") or {}).get("wording") or "").strip()
    is_delayed = (not is_cancelled and not is_diverted
                  and (dep_delay >= 15 or "delay" in status_words.lower()
                       or "delay" in delay_wording.lower() or color == "red"))

    sched_local = (schedule.get("scheduledDeparture") or "")[11:16] or "-"
    act_local = (schedule.get("estimatedActualDeparture") or "")[11:16] or "-"

    return {
        "status_known": True,
        "is_cancelled": is_cancelled,
        "is_diverted": is_diverted,
        "is_delayed": is_delayed,
        "delay_minutes": dep_delay,
        "status_words": status_words or final_status or "Scheduled",
        "delay_wording": delay_wording,
        "sched_dep_local": sched_local,
        "est_dep_local": act_local,
        "last_updated": status.get("lastUpdatedText", ""),
        "source": "FlightStats (Cirium)",
    }

@st.cache_data(ttl=600, show_spinner=False)
def fr24_fetch_flight_history(flight_no_compact):
    """Pull up to 25 recent/upcoming operations of a flight number from FR24."""
    params = {
        "query": flight_no_compact,   # e.g. "UL225"
        "fetchBy": "flight",
        "limit": 25,
        "page": 1,
    }
    resp = requests.get(FR24_URL, params=params, headers=FR24_HEADERS, timeout=15)
    resp.raise_for_status()
    payload = resp.json()
    return (payload.get("result", {})
                   .get("response", {})
                   .get("data", None)) or []

def _local_date_at_origin(epoch_utc, tz_offset_seconds):
    """Convert a UTC epoch to the calendar date at the departure airport."""
    if epoch_utc is None:
        return None
    return (datetime.fromtimestamp(epoch_utc, tz=timezone.utc)
            + timedelta(seconds=tz_offset_seconds or 0)).date()

def _fmt_local_time(epoch_utc, tz_offset_seconds):
    if epoch_utc is None:
        return "-"
    return (datetime.fromtimestamp(epoch_utc, tz=timezone.utc)
            + timedelta(seconds=tz_offset_seconds or 0)).strftime("%H:%M")

def query_fr24(flight_no, flight_date):
    """
    Fallback / enrichment check via FR24's keyless list feed.
    flight_date: datetime.date of the departure (origin local time).
    """
    clean_fn = flight_no.replace(" ", "").upper()
    try:
        entries = fr24_fetch_flight_history(clean_fn)
    except Exception as e:
        return {"status_known": False, "is_delayed": False,
                "error": f"FR24 unreachable ({type(e).__name__})"}

    if not entries:
        return {"status_known": False, "is_delayed": False,
                "error": "Flight number not found on Flightradar24"}

    # Find the operation whose scheduled departure date (origin local time)
    # matches the roster date exactly.
    matched = None
    for entry in entries:
        t = entry.get("time", {}) or {}
        sched_dep = (t.get("scheduled") or {}).get("departure")
        origin = ((entry.get("airport") or {}).get("origin") or {})
        tz_off = ((origin.get("timezone") or {}).get("offset")) or 0
        if _local_date_at_origin(sched_dep, tz_off) == flight_date:
            matched = entry
            break

    if matched is None:
        return {"status_known": False, "is_delayed": False,
                "error": f"No {clean_fn} operation listed for {flight_date.strftime('%d %b %Y')} (schedule may not be published yet)"}

    t = matched.get("time", {}) or {}
    sched_dep = (t.get("scheduled") or {}).get("departure")
    est_dep = (t.get("estimated") or {}).get("departure")
    real_dep = (t.get("real") or {}).get("departure")
    origin = ((matched.get("airport") or {}).get("origin") or {})
    tz_off = ((origin.get("timezone") or {}).get("offset")) or 0

    status_obj = matched.get("status", {}) or {}
    status_text = (status_obj.get("text") or "").strip()
    generic = ((status_obj.get("generic") or {}).get("status") or {})
    generic_text = (generic.get("text") or "").lower()
    is_live = bool(status_obj.get("live"))

    # FR24 times ending in '*' are synthetic historical-average guesses,
    # NOT real airline data — never treat them as verification of anything.
    is_synthetic = "*" in status_text

    is_cancelled = "cancel" in generic_text or "cancel" in status_text.lower()
    is_diverted = bool(generic.get("diverted")) or "divert" in status_text.lower()

    # Delay = estimated/actual departure later than schedule, or FR24 flags it.
    delay_mins = 0
    ref_dep = real_dep or (None if is_synthetic else est_dep)
    if sched_dep and ref_dep and ref_dep > sched_dep:
        delay_mins = int(round((ref_dep - sched_dep) / 60))
    flagged_delayed = ("delay" in generic_text or "delay" in status_text.lower()
                       or generic.get("color") == "red")
    is_delayed = (not is_cancelled) and (flagged_delayed or delay_mins >= 15)

    return {
        "status_known": True,
        "is_delayed": is_delayed,
        "is_cancelled": is_cancelled,
        "is_diverted": is_diverted,
        "is_synthetic": is_synthetic,
        "delay_minutes": delay_mins,
        "is_live": is_live,
        "fr24_status": status_text or generic_text.capitalize() or "Scheduled",
        "sched_dep_local": _fmt_local_time(sched_dep, tz_off),
        "est_dep_local": _fmt_local_time(ref_dep, tz_off) if ref_dep else "-",
        "aircraft": ((matched.get("aircraft") or {}).get("model") or {}).get("code") or "-",
        "registration": ((matched.get("aircraft") or {}).get("registration")) or "-",
        "sched_dep_epoch": sched_dep,
        "origin_iata": ((origin.get("code") or {}) or {}).get("iata"),
        "has_departed": bool(real_dep),
        "source": "Flightradar24",
    }

# --- INBOUND AIRCRAFT TRACKER (tail-number watch via FR24, keyless) ---
@st.cache_data(ttl=600, show_spinner=False)
def fr24_fetch_by_reg(reg):
    """All recent/upcoming sectors flown by a specific tail number."""
    params = {"query": reg, "fetchBy": "reg", "limit": 25, "page": 1}
    resp = requests.get(FR24_URL, params=params, headers=FR24_HEADERS, timeout=15)
    resp.raise_for_status()
    payload = resp.json()
    return (payload.get("result", {}).get("response", {}).get("data", None)) or []

def analyze_inbound(reg, origin_iata, our_sched_dep_epoch):
    """
    Find the sector this exact aircraft flies INTO our departure airport
    right before our flight, and measure how late it is running.
    """
    if not reg or reg == "-" or not origin_iata or not our_sched_dep_epoch:
        return None
    try:
        entries = fr24_fetch_by_reg(reg)
    except Exception:
        return None
    best, best_sa = None, 0
    for e in entries:
        dest = ((((e.get("airport") or {}).get("destination") or {}).get("code")) or {}).get("iata")
        sa = (((e.get("time") or {}).get("scheduled")) or {}).get("arrival")
        if dest == origin_iata and sa and sa <= our_sched_dep_epoch + 1800 and sa > best_sa:
            best, best_sa = e, sa
    if best is None:
        return None
    t = best.get("time", {}) or {}
    sa = (t.get("scheduled") or {}).get("arrival")
    ea = (t.get("estimated") or {}).get("arrival")
    ra = (t.get("real") or {}).get("arrival")
    st_obj = best.get("status", {}) or {}
    status_text = (st_obj.get("text") or "").strip()
    synthetic = "*" in status_text
    ref = ra or (None if synthetic else ea)
    delay = int((ref - sa) / 60) if (ref and sa and ref > sa) else 0
    dtz = ((((best.get("airport") or {}).get("destination") or {}).get("timezone")) or {}).get("offset") or 0
    num = (((best.get("identification") or {}).get("number") or {}).get("default")) or "?"
    from_iata = ((((best.get("airport") or {}).get("origin") or {}).get("code")) or {}).get("iata") or "?"
    return {"flight_no": num, "from": from_iata,
            "delay_mins": delay, "landed": bool(ra), "live": bool(st_obj.get("live")),
            "sched_arr_local": _fmt_local_time(sa, dtz),
            "est_arr_local": _fmt_local_time(ref, dtz) if ref else "-",
            "turnaround_min": int((our_sched_dep_epoch - sa) / 60) if sa else None}

def _telemetry_core(flight_no, flight_date, route, scheduled_dep):
    """
    flight_date is a datetime.date object taken straight from the roster.
    Strategy: FlightStats/Cirium (airline-filed disruptions) is authoritative;
    FR24 is fallback + enrichment. A disruption reported by EITHER source
    is alerted — never let one source's 'on time' mask the other's alert.
    """
    fs = query_flightstats(flight_no, flight_date)
    fr = query_fr24(flight_no, flight_date)
    fr_ok = fr.get("status_known", False)

    reg_bits = []
    if fr_ok and fr.get("registration", "-") != "-":
        reg_bits.append(fr["registration"])
    if fr_ok and fr.get("aircraft", "-") != "-":
        reg_bits.append(fr["aircraft"])
    tail = f" · {' / '.join(reg_bits)}" if reg_bits else ""

    if not fs and not fr_ok:
        err = fr.get("error", "no data from FlightStats or FR24")
        return {"is_delayed": False, "severity": "unknown",
                "status_message": f"ℹ️ {flight_no} ({route}) — {err}."}

    date_label = flight_date.strftime("%d %b")

    # --- Merge disruption flags (either source can raise an alert) ---
    cancelled = bool(fs and fs["is_cancelled"]) or bool(fr_ok and fr.get("is_cancelled"))
    diverted = bool(fs and fs["is_diverted"]) or bool(fr_ok and fr.get("is_diverted"))
    delayed = bool(fs and fs["is_delayed"]) or bool(fr_ok and fr.get("is_delayed"))
    delay_mins = max(fs["delay_minutes"] if fs else 0,
                     fr.get("delay_minutes", 0) if fr_ok else 0)

    primary = fs if fs else fr
    src = "FlightStats/Cirium" if fs else "Flightradar24"
    sched = primary.get("sched_dep_local", scheduled_dep)
    updated = f" ({fs['last_updated']})" if fs and fs.get("last_updated") else ""

    if cancelled:
        return {"is_delayed": True, "severity": "cancelled",
                "status_message": f"🚫 CANCELLED — {src} reports {flight_no} on {date_label} is cancelled{tail}.{updated}"}

    if diverted:
        det = fs["status_words"] if fs else fr.get("fr24_status", "Diverted")
        return {"is_delayed": True, "severity": "diverted",
                "status_message": f"🔀 DIVERTED — {src}: '{det}' for {flight_no} on {date_label} (sched dep {sched}){tail}.{updated}"}

    if delayed:
        mins_str = f" by ~{delay_mins} min" if delay_mins > 0 else ""
        est = primary.get("est_dep_local", "-")
        est_str = f" New est. departure {est}." if est not in ("-", sched) else ""
        wording = f" {fs['delay_wording']}." if fs and fs.get("delay_wording") else ""
        return {"is_delayed": True, "severity": "delayed",
                "status_message": f"⚠️ DELAYED{mins_str} — {src}: {flight_no} sched {sched}.{est_str}{wording}{tail}{updated}"}

    # --- No disruption filed ---
    if fs:
        return {"is_delayed": False, "severity": "ok",
                "status_message": f"✅ {fs['status_words']} — {flight_no} dep {sched}, no disruption filed (FlightStats/Cirium){tail}.{updated}"}

    live_str = " (airborne now)" if fr.get("is_live") else ""
    if fr.get("is_synthetic"):
        return {"is_delayed": False, "severity": "ok",
                "status_message": f"✅ No disruption filed — {flight_no} dep {sched}{live_str}. FR24 estimate is predictive only; will alert if a delay/cancellation is filed{tail}."}
    return {"is_delayed": False, "severity": "ok",
            "status_message": f"✅ {fr.get('fr24_status', 'On time')}{live_str} — {flight_no} dep {sched} verified via Flightradar24{tail}."}


MIN_TURNAROUND_MIN = 60   # minimum realistic turnaround for the aircraft
MIN_REST_HOURS = 17.5     # FAU: 17h30m chocks-on to next report at base

def fetch_live_flight_telemetry(flight_no, flight_date, route, scheduled_dep):
    """Core dual-source status + inbound-aircraft watch on top."""
    res = _telemetry_core(flight_no, flight_date, route, scheduled_dep)
    res.setdefault("inbound_note", None)
    res.setdefault("inbound_risk", False)
    fr = query_fr24(flight_no, flight_date)   # cached — no extra network cost
    if (fr.get("status_known") and not fr.get("has_departed")
            and res.get("severity") in ("ok", "delayed", "unknown")):
        inb = analyze_inbound(fr.get("registration"), fr.get("origin_iata"), fr.get("sched_dep_epoch"))
        if inb and inb["delay_mins"] >= 15:
            remaining = (inb["turnaround_min"] or 0) - inb["delay_mins"]
            risk = remaining < MIN_TURNAROUND_MIN
            arr_word = "landed" if inb["landed"] else ("ETA" if inb["est_arr_local"] != "-" else "due")
            res["inbound_note"] = (
                f"🛬 Inbound aircraft {inb['flight_no']} ({inb['from']} ➔ {fr.get('origin_iata')}) "
                f"running ~{inb['delay_mins']} min late ({arr_word} {inb['est_arr_local']}, sched {inb['sched_arr_local']}). "
                f"Turnaround buffer left: {max(remaining, 0)} min — "
                + ("DEPARTURE AT RISK. Expect a delay call." if risk else "your departure should hold."))
            res["inbound_risk"] = risk
    return res

def rest_impact_note(rows, flight_no, fdate, delay_mins):
    """
    If a monitored flight is running late, recompute the rest period before
    the NEXT duty in the roster and flag if it drops below MIN_REST_HOURS.
    """
    if not delay_mins or delay_mins <= 0:
        return None
    for i, r in enumerate(rows):
        if (r["Flight / Code"] == flight_no and r["DateObj"] is not None
                and r["DateObj"].date() == fdate and r["Arrival"] != "-"):
            try:
                arr_t = datetime.strptime(r["Arrival"], "%H:%M").time()
                arr_dt = datetime.combine(fdate, arr_t)
                if r["Departure"] != "-" and r["Arrival"] < r["Departure"]:
                    arr_dt += timedelta(days=1)
            except Exception:
                return None
            for j in range(i + 1, len(rows)):
                nr = rows[j]
                if nr["Type"] in ("FLIGHT", "STANDBY") and nr["DateObj"] is not None:
                    tstr = nr["Check-In"] if nr["Check-In"] != "-" else nr["Departure"]
                    if tstr == "-":
                        return None
                    try:
                        ci_dt = datetime.combine(nr["DateObj"].date(), datetime.strptime(tstr, "%H:%M").time())
                    except Exception:
                        return None
                    rest0 = (ci_dt - arr_dt).total_seconds() / 3600
                    if rest0 <= 0:
                        return None
                    new_rest = rest0 - delay_mins / 60
                    nxt = nr["Flight / Code"]
                    if new_rest >= MIN_REST_HOURS:
                        return (f"🛏 Rest impact: {rest0:.1f}h → {new_rest:.1f}h before {nxt} — "
                                f"rest NOT affected (min 17h30m).")
                    return (f"🛏 Rest impact: {rest0:.1f}h → {new_rest:.1f}h before {nxt} — "
                            f"BELOW the 17h30m FAU MINIMUM. Contact crew control; report/pickup time must shift.")
            return None
    return None


def suggest_standby_for_cancel(row):
    """SOFT advisory only — never edits the roster. Given a CANCELLED flight's
    roster row, return the standby code that is typically inserted, per the
    FAU map:
      * midnight flight (departs 00:00–05:59): report before midnight → SBY4,
        report after midnight → SBY1
      * Middle-East flight reporting before 18:00 → SBY3 (18:00+ → none)
      * morning report (any flight): before 06:00 → SBY1, 06:00–11:59 → SBY2
    Returns None when the flight doesn't match any category."""
    ci = row.get("CIdt") or row.get("DEPdt")
    dep = row.get("DEPdt")
    if not isinstance(ci, datetime) or not isinstance(dep, datetime):
        return None
    _, d = _route_od(row.get("Route") or "")

    # midnight flight (small-hours departure)
    if dep.time() < dtime(6, 0):
        return "SB4" if ci.date() < dep.date() else "SB1"

    # Middle-East flight reporting before 18:00 → SBY3 (18:00+ → no suggestion)
    if d in MIDEAST_STATIONS and ci.time() < dtime(18, 0):
        return "SB3"

    # Morning turnaround — keyed on report/check-in time, any flight:
    #   report before 06:00 → SBY1 ;  morning report 06:00–11:59 → SBY2
    if ci.time() < dtime(6, 0):
        return "SB1"
    if ci.time() < dtime(12, 0):
        return "SB2"

    return None


# --- 3.5 ANALYTICS, STATION INTEL & WEATHER (KEYLESS) ---
import math

STATION_INFO = {
    # iata: (city, country, lat, lon, [spots] or None)
    "CMB": ("Colombo", "Sri Lanka", 6.9271, 79.8612, ["☕ Barefoot Garden Cafe", "🌊 Galle Face Green Walk", "🍛 Ministry of Crab"]),
    "DXB": ("Dubai", "UAE", 25.2532, 55.3657, ["🌆 Dubai Marina Walk", "🛍 Gold Souk", "🍽 Al Seef Waterfront"]),
    "AUH": ("Abu Dhabi", "UAE", 24.4539, 54.3773, None),
    "DOH": ("Doha", "Qatar", 25.2854, 51.5310, ["🏛 Museum of Islamic Art", "🛍 Souq Waqif", "🌊 Corniche Walk"]),
    "RUH": ("Riyadh", "Saudi Arabia", 24.7136, 46.6753, None),
    "DMM": ("Dammam", "Saudi Arabia", 26.4207, 50.0888, None),
    "JED": ("Jeddah", "Saudi Arabia", 21.4858, 39.1925, None),
    "KWI": ("Kuwait City", "Kuwait", 29.3759, 47.9774, None),
    "BAH": ("Manama", "Bahrain", 26.2285, 50.5860, None),
    "MCT": ("Muscat", "Oman", 23.5880, 58.3829, None),
    "BKK": ("Bangkok", "Thailand", 13.7563, 100.5018, ["🛕 Wat Arun (Sunset)", "🍜 Chinatown Street Food", "🛍 Chatuchak Market"]),
    "SIN": ("Singapore", "Singapore", 1.3521, 103.8198, ["🌳 Gardens by the Bay", "🍜 Maxwell Hawker Centre", "🌆 Marina Bay Walk"]),
    "KUL": ("Kuala Lumpur", "Malaysia", 3.1390, 101.6869, ["🌆 Petronas Towers", "🍜 Jalan Alor Food Street", "🛕 Batu Caves"]),
    "CGK": ("Jakarta", "Indonesia", -6.2088, 106.8456, None),
    "HKG": ("Hong Kong", "China", 22.3193, 114.1694, None),
    "CAN": ("Guangzhou", "China", 23.1291, 113.2644, None),
    "PVG": ("Shanghai", "China", 31.2304, 121.4737, None),
    "PEK": ("Beijing", "China", 39.9042, 116.4074, None),
    "ICN": ("Seoul", "South Korea", 37.5665, 126.9780, ["🏯 Gyeongbokgung Palace", "🍜 Myeongdong Street Food", "🌆 N Seoul Tower"]),
    "NRT": ("Tokyo", "Japan", 35.6762, 139.6503, ["⛩ Senso-ji Temple", "🍣 Tsukiji Outer Market", "🌆 Shibuya Crossing"]),
    "KIX": ("Osaka", "Japan", 34.6937, 135.5023, None),
    "MLE": ("Malé", "Maldives", 4.1755, 73.5093, ["🏖 Artificial Beach", "🐟 Fish Market", "☕ Seagull Cafe"]),
    "GAN": ("Gan Island", "Maldives", -0.6936, 73.1556, None),
    "MAA": ("Chennai", "India", 13.0827, 80.2707, ["🏖 Marina Beach", "🛕 Kapaleeshwarar Temple", "🍛 Murugan Idli Shop"]),
    "DEL": ("New Delhi", "India", 28.6139, 77.2090, ["🏛 Humayun's Tomb", "🛍 Khan Market", "🍛 Karim's Old Delhi"]),
    "BLR": ("Bengaluru", "India", 12.9716, 77.5946, None),
    "BOM": ("Mumbai", "India", 19.0760, 72.8777, None),
    "HYD": ("Hyderabad", "India", 17.3850, 78.4867, None),
    "CCU": ("Kolkata", "India", 22.5726, 88.3639, None),
    "COK": ("Kochi", "India", 9.9312, 76.2673, None),
    "TRV": ("Thiruvananthapuram", "India", 8.5241, 76.9366, None),
    "TRZ": ("Tiruchirappalli", "India", 10.7905, 78.7047, None),
    "DAC": ("Dhaka", "Bangladesh", 23.8103, 90.4125, None),
    "KHI": ("Karachi", "Pakistan", 24.8607, 67.0011, None),
    "LHE": ("Lahore", "Pakistan", 31.5204, 74.3587, None),
    "SEZ": ("Mahé", "Seychelles", -4.6796, 55.4920, None),
    "LHR": ("London", "UK", 51.5074, -0.1278, ["🎡 South Bank Walk", "🛍 Borough Market", "🏛 British Museum"]),
    "CDG": ("Paris", "France", 48.8566, 2.3522, ["🗼 Eiffel Tower", "☕ Le Marais Cafes", "🖼 Louvre"]),
    "FRA": ("Frankfurt", "Germany", 50.1109, 8.6821, None),
    "ZRH": ("Zurich", "Switzerland", 47.3769, 8.5417, None),
    "IST": ("Istanbul", "Turkey", 41.0082, 28.9784, None),
    "SYD": ("Sydney", "Australia", -33.8688, 151.2093, ["☕ Single O Surry Hills", "🌳 Royal Botanic Garden", "🍽 Opera Bar (Harbour Views)"]),
    "MEL": ("Melbourne", "Australia", -37.8136, 144.9631, ["☕ Degraves Street Lanes", "🌳 Fitzroy Gardens", "🍽 Queen Vic Market"]),
}
DEFAULT_SPOTS = ["☕ Top-rated cafe near crew hotel", "🌳 City walk / park", "🍜 Local food spot"]

AIRPORT_NAME = {
    # IATA -> full airport name (shown in Layover Intel)
    "CMB": "Bandaranaike International Airport",
    "MAA": "Chennai International Airport",
    "DEL": "Indira Gandhi International Airport",
    "BOM": "Chhatrapati Shivaji Maharaj International Airport",
    "BLR": "Kempegowda International Airport",
    "HYD": "Rajiv Gandhi International Airport",
    "CCU": "Netaji Subhas Chandra Bose International Airport",
    "COK": "Cochin International Airport",
    "TRV": "Thiruvananthapuram International Airport",
    "TRZ": "Tiruchirappalli International Airport",
    "MLE": "Velana International Airport",
    "GAN": "Gan International Airport",
    "KHI": "Jinnah International Airport",
    "LHE": "Allama Iqbal International Airport",
    "DAC": "Hazrat Shahjalal International Airport",
    "DXB": "Dubai International Airport",
    "AUH": "Zayed International Airport",
    "DOH": "Hamad International Airport",
    "BAH": "Bahrain International Airport",
    "DMM": "King Fahd International Airport",
    "RUH": "King Khalid International Airport",
    "JED": "King Abdulaziz International Airport",
    "KWI": "Kuwait International Airport",
    "MCT": "Muscat International Airport",
    "SIN": "Singapore Changi Airport",
    "KUL": "Kuala Lumpur International Airport",
    "BKK": "Suvarnabhumi Airport",
    "CGK": "Soekarno–Hatta International Airport",
    "HKG": "Hong Kong International Airport",
    "CAN": "Guangzhou Baiyun International Airport",
    "PVG": "Shanghai Pudong International Airport",
    "PEK": "Beijing Capital International Airport",
    "ICN": "Incheon International Airport",
    "NRT": "Narita International Airport",
    "KIX": "Kansai International Airport",
    "IST": "Istanbul Airport",
    "LHR": "London Heathrow Airport",
    "CDG": "Paris Charles de Gaulle Airport",
    "FRA": "Frankfurt Airport",
    "ZRH": "Zurich Airport",
    "SYD": "Sydney Kingsford Smith Airport",
    "MEL": "Melbourne Airport",
    "SEZ": "Seychelles International Airport",
}

PER_DIEM = {"LHR": 120, "CDG": 115, "FRA": 110, "ZRH": 130, "SYD": 110, "MEL": 105,
            "NRT": 110, "KIX": 105, "ICN": 100, "HKG": 100, "SIN": 95, "DXB": 90,
            "AUH": 88, "DOH": 88, "MLE": 85, "BKK": 70, "KUL": 65, "CGK": 65,
            "BOM": 65, "DEL": 60, "BLR": 60, "MAA": 55}
PER_DIEM_DEFAULT = 70

def wx_label(code):
    if code == 0: return "☀️", "Clear"
    if code in (1,): return "🌤", "Mostly Clear"
    if code in (2,): return "⛅", "Partly Cloudy"
    if code in (3,): return "☁️", "Overcast"
    if code in (45, 48): return "🌫", "Fog"
    if 51 <= code <= 67: return "🌧", "Rain"
    if 71 <= code <= 77: return "🌨", "Snow"
    if 80 <= code <= 82: return "🌦", "Showers"
    if code >= 95: return "⛈", "Thunderstorm"
    return "🌡", "—"

@st.cache_data(ttl=1800, show_spinner=False)
def fetch_station_weather(iata):
    """Live weather + local time via Open-Meteo (free, keyless, no limits)."""
    info = STATION_INFO.get(iata)
    if not info:
        return None
    city, country, lat, lon, _ = info
    try:
        r = requests.get("https://api.open-meteo.com/v1/forecast",
                         params={"latitude": lat, "longitude": lon,
                                 "current": "temperature_2m,weather_code",
                                 "timezone": "auto"}, timeout=10)
        r.raise_for_status()
        j = r.json()
        cur = j.get("current", {})
        icon, desc = wx_label(int(cur.get("weather_code", -1)))
        off = int(j.get("utc_offset_seconds", 0))
        local = datetime.now(timezone.utc) + timedelta(seconds=off)
        gmt = f"GMT{'+' if off >= 0 else '-'}{abs(off)//3600}" + (f":{(abs(off)%3600)//60:02d}" if off % 3600 else "")
        return {"city": city, "country": country, "icon": icon, "desc": desc,
                "temp": round(cur.get("temperature_2m", 0)),
                "local_time": local.strftime("%I:%M %p").lstrip("0"), "gmt": gmt}
    except Exception:
        return {"city": city, "country": country, "icon": "🌡", "desc": "n/a",
                "temp": None, "local_time": "-", "gmt": ""}

def _mins_between(dep_str, arr_str):
    try:
        d = datetime.strptime(dep_str, "%H:%M")
        a = datetime.strptime(arr_str, "%H:%M")
        if a <= d:
            a += timedelta(days=1)
        return int((a - d).total_seconds() // 60)
    except Exception:
        return 0

def compute_analytics(rows):
    block_min, redeyes, n_flights = 0, 0, 0
    duty_days, daily_min = set(), {}
    layovers = enrich_layovers(rows)
    for r in rows:
        if r["Type"] == "FLIGHT":
            if r.get("DEPdt_u") and r.get("ARRdt_u"):
                m = int((r["ARRdt_u"] - r["DEPdt_u"]).total_seconds() // 60)
                if m <= 0:
                    m = 0
            elif r["Departure"] != "-" and r["Arrival"] != "-":
                m = _mins_between(r["Departure"], r["Arrival"])
            else:
                continue
            block_min += m
            n_flights += 1
            try:
                h = int(r["Departure"][:2])
                if h >= 22 or h < 6:
                    redeyes += 1
            except Exception:
                pass
            if r["DateObj"]:
                d = r["DateObj"].date()
                duty_days.add(d)
                daily_min[d] = daily_min.get(d, 0) + m
    days = sorted(duty_days)
    max_streak = streak = 1 if days else 0
    for i in range(1, len(days)):
        streak = streak + 1 if (days[i] - days[i-1]).days == 1 else 1
        max_streak = max(max_streak, streak)
    block_hrs = block_min / 60
    fatigue = min(10.0, round(1.5 + redeyes * 1.4 + max_streak * 0.7 + (block_hrs / 85) * 3.0, 1))
    fat_label = "Low" if fatigue < 4 else ("Moderate" if fatigue < 7 else "High")
    allow_rows = []
    for lv in layovers:
        stn = lv["station"] or "?"
        nights = lv.get("nights") or (max(1, int((lv["ground_hrs"] or 24) // 24)) if lv["ground_hrs"] else 1)
        rate = PER_DIEM.get(stn, PER_DIEM_DEFAULT)
        allow_rows.append((stn, nights, rate, nights * rate))
    return {"block_hrs": round(block_hrs, 1), "block_target": 85, "flights": n_flights,
            "redeyes": redeyes, "max_streak": max_streak, "fatigue": fatigue,
            "fatigue_label": fat_label, "daily_min": daily_min,
            "allowance_rows": allow_rows, "allowance_total": sum(a[3] for a in allow_rows),
            "layovers": layovers}

def enrich_layovers(rows):
    out, n = [], len(rows)
    for i, r in enumerate(rows):
        if r["Type"] != "LAYOVER":
            continue
        station = r["Route"] if r.get("Route") and r["Route"] != "-" and len(r["Route"]) == 3 else None
        arr_dt, dep_dt = None, None
        for j in range(i - 1, -1, -1):
            if rows[j]["Type"] == "FLIGHT":
                codes = re.findall(r'[A-Z]{3}', rows[j]["Route"])
                if station is None and len(codes) >= 2:
                    station = codes[1]
                if rows[j]["DateObj"] and rows[j]["Arrival"] != "-":
                    try:
                        t = datetime.strptime(rows[j]["Arrival"], "%H:%M").time()
                        arr_dt = datetime.combine(rows[j]["DateObj"].date(), t)
                        if rows[j]["Departure"] != "-" and rows[j]["Arrival"] < rows[j]["Departure"]:
                            arr_dt += timedelta(days=1)
                    except Exception:
                        pass
                break
        for j in range(i + 1, n):
            if rows[j]["Type"] == "FLIGHT" and rows[j]["DateObj"]:
                tstr = rows[j]["Check-In"] if rows[j]["Check-In"] != "-" else rows[j]["Departure"]
                if tstr != "-":
                    try:
                        dep_dt = datetime.combine(rows[j]["DateObj"].date(),
                                                  datetime.strptime(tstr, "%H:%M").time())
                    except Exception:
                        pass
                break
        ground = None
        if arr_dt and dep_dt and dep_dt > arr_dt:
            ground = round((dep_dt - arr_dt).total_seconds() / 3600, 1)
        nights = None
        if r.get("EndDateObj") and r["DateObj"]:
            nights = max(1, (r["EndDateObj"].date() - r["DateObj"].date()).days)
        out.append({"date": r["DateObj"].date() if r["DateObj"] else None,
                    "station": station, "ground_hrs": ground, "nights": nights})
    return out

# --- SVG WIDGETS (no chart libraries needed) ---
def donut_svg(pct, center, sub, color="#00bcd4"):
    pct = max(0.0, min(1.0, pct))
    r, c = 44, 2 * math.pi * 44
    return (f"<svg width='120' height='120' viewBox='0 0 120 120'>"
            f"<circle cx='60' cy='60' r='{r}' fill='none' stroke='#1f2b3a' stroke-width='11'/>"
            f"<circle cx='60' cy='60' r='{r}' fill='none' stroke='{color}' stroke-width='11' "
            f"stroke-linecap='round' stroke-dasharray='{c*pct:.1f} {c:.1f}' transform='rotate(-90 60 60)'/>"
            f"<text x='60' y='58' text-anchor='middle' fill='#fff' font-size='19' font-weight='700'>{center}</text>"
            f"<text x='60' y='76' text-anchor='middle' fill='#7e8ba0' font-size='10'>{sub}</text></svg>")

def _arc(cx, cy, r, a0, a1):
    x0, y0 = cx + r * math.cos(math.radians(a0)), cy + r * math.sin(math.radians(a0))
    x1, y1 = cx + r * math.cos(math.radians(a1)), cy + r * math.sin(math.radians(a1))
    return f"M {x0:.1f} {y0:.1f} A {r} {r} 0 0 1 {x1:.1f} {y1:.1f}"

def gauge_svg(score):
    segs, out = [("#4caf50", 180, 225), ("#8bc34a", 225, 270), ("#ffc107", 270, 315), ("#ff5252", 315, 360)], []
    for color, a0, a1 in segs:
        out.append(f"<path d='{_arc(70, 66, 48, a0, a1)}' stroke='{color}' stroke-width='10' fill='none' stroke-linecap='round'/>")
    ang = 180 + (max(0, min(10, score)) / 10) * 180
    nx, ny = 70 + 36 * math.cos(math.radians(ang)), 66 + 36 * math.sin(math.radians(ang))
    out.append(f"<line x1='70' y1='66' x2='{nx:.1f}' y2='{ny:.1f}' stroke='#fff' stroke-width='3' stroke-linecap='round'/>")
    out.append("<circle cx='70' cy='66' r='4.5' fill='#fff'/>")
    return f"<svg width='140' height='80' viewBox='0 0 140 80'>{''.join(out)}</svg>"

def sparkline_svg(values, color="#ff5252"):
    if not values:
        return ""
    w, h, mx = 150, 34, max(values) or 1
    step = w / max(1, len(values) - 1) if len(values) > 1 else w
    pts = " ".join(f"{i*step:.1f},{h - 4 - (v/mx)*(h-8):.1f}" for i, v in enumerate(values))
    return (f"<svg width='{w}' height='{h}' viewBox='0 0 {w} {h}'>"
            f"<polyline points='{pts}' fill='none' stroke='{color}' stroke-width='2'/></svg>")

def _compact_flight_label(nums):
    """'UL404','UL405' -> 'UL404/5'; 'UL133','UL134' -> 'UL133/4'."""
    nums = [n for n in nums if n]
    if not nums:
        return "?"
    if len(nums) == 1:
        return nums[0]
    prefix = ""
    for chars in zip(*nums):
        if all(c == chars[0] for c in chars):
            prefix += chars[0]
        else:
            break
    return prefix + "/".join(n[len(prefix):] for n in nums)


def _calendar_duties(rows):
    """Group consecutive sectors for CALENDAR display. A new chip starts when
    a sector departs base (CMB), when it doesn't continue from the previous
    sector's destination, or when the ground gap exceeds 4 hours — so a
    turnaround (CMB→X→CMB) shows as one chip but a fresh CMB departure after
    returning does not merge into it."""
    fl = [r for r in rows if r["Type"] == "FLIGHT" and r.get("DEPdt") and r.get("ARRdt")]
    groups = []
    for r in fl:
        o, d = _route_od(r["Route"])
        sec = {"flight": r["Flight / Code"], "o": o, "d": d,
               "ci": r.get("CIdt"), "dep": r["DEPdt"], "arr": r["ARRdt"], "co": r.get("COdt"),
               "dep_u": r.get("DEPdt_u"), "arr_u": r.get("ARRdt_u")}
        prev = groups[-1][-1] if groups else None
        gap_ok = (prev is not None and sec["dep"] is not None and prev["arr"] is not None
                  and (sec["dep"] - prev["arr"]).total_seconds() <= 4 * 3600)
        if prev is not None and o != "CMB" and prev["d"] == o and gap_ok:
            groups[-1].append(sec)
        else:
            groups.append([sec])
    return groups


def _fmt_hm(mins):
    """310 -> '5h10', 55 -> '55m', 120 -> '2h'."""
    h, m = divmod(max(0, int(mins)), 60)
    if h and m:
        return f"{h}h{m:02d}"
    if h:
        return f"{h}h"
    return f"{m}m"


# --- 3.6 FDP CALCULATOR — FOM Part A Chapter 08 (cabin crew) ---
# Table A/B hold the FLIGHT-CREW maxima in minutes; cabin crew get +1:00 (8.3.a).
# All times are Colombo (CMB) local (user instruction — same convention as the
# meal-allowance math). "Local time of start" for Table A = first departure − 1h
# (the standard flight-crew reporting time).

FDP_TABLE_A = {   # acclimatized — keyed on local time of start
    "0600-0759": [780, 735, 690, 645, 600, 570, 540, 540],
    "0800-1259": [840, 795, 750, 705, 660, 630, 600, 570],
    "1300-1759": [780, 735, 690, 645, 600, 570, 540, 540],
    "1800-2159": [720, 675, 630, 585, 540, 540, 540, 540],
    "2200-0559": [660, 615, 570, 540, 540, 540, 540, 540],
}
FDP_TABLE_B = {   # not acclimatized — keyed on length of preceding rest
    "up18_or_over30": [780, 750, 690, 645, 600, 555, 540, 540],
    "between18_30":   [690, 660, 630, 585, 540, 540, 540, 540],
}


def fdp_band(t):
    """Table A row key for a given local-time-of-start."""
    if dtime(6, 0) <= t <= dtime(7, 59):
        return "0600-0759"
    if dtime(8, 0) <= t <= dtime(12, 59):
        return "0800-1259"
    if dtime(13, 0) <= t <= dtime(17, 59):
        return "1300-1759"
    if dtime(18, 0) <= t <= dtime(21, 59):
        return "1800-2159"
    return "2200-0559"


def fdp_limit_min(acclimatized, band, sectors, preceding_rest_h=None):
    """Cabin-crew max FDP in minutes = table value + 60 (8.3.a)."""
    sectors = max(1, min(int(sectors), 8))
    if acclimatized:
        row = FDP_TABLE_A.get(band, FDP_TABLE_A["0600-0759"])
    else:
        bucket = ("between18_30" if preceding_rest_h is not None and 18 < preceding_rest_h < 30
                  else "up18_or_over30")
        row = FDP_TABLE_B[bucket]
    return row[sectors - 1] + 60


def _duty_chip(sectors):
    """One compact chip per duty: a turnaround collapses to 'UL404/5' with one
    line per sector — route, dep–arr times and TRUE flying time (UTC-corrected
    block). No check-in/check-out clutter; midnight crossings get a tiny
    '▸ starts / ↳ lands' marker on the adjacent day instead."""
    title = _compact_flight_label([s["flight"].replace(" ", "") for s in sectors])
    lines = []
    for s in sectors:
        dep_t = s["dep"].strftime("%H:%M") if isinstance(s["dep"], datetime) else ""
        arr_t = s["arr"].strftime("%H:%M") if isinstance(s["arr"], datetime) else ""
        a, b = s.get("arr_u"), s.get("dep_u")
        if not (isinstance(a, datetime) and isinstance(b, datetime)):
            a, b = s.get("arr"), s.get("dep")
        dur = ""
        if isinstance(a, datetime) and isinstance(b, datetime) and a > b:
            dur = f" · {_fmt_hm((a - b).total_seconds() // 60)}"
        lines.append(f"<span>{s['o']}→{s['d']} {dep_t}–{arr_t}{dur}</span>")
    return f"<div class='chip chip-flt'>✈ <b>{title}</b><br>{'<br>'.join(lines)}</div>"


def _duty_begin_chip(sectors):
    """Tiny marker on the day BEFORE a duty's first departure (check-in day)."""
    title = _compact_flight_label([s["flight"].replace(" ", "") for s in sectors])
    t = sectors[0]["ci"].strftime("%H:%M") if isinstance(sectors[0].get("ci"), datetime) else ""
    return f"<div class='chip chip-begin'>▸ <b>{title}</b>{(' ' + t) if t else ''}</div>"


def _duty_cont_chip(sectors):
    """Tiny marker on the day AFTER a duty's last departure (lands day)."""
    title = _compact_flight_label([s["flight"].replace(" ", "") for s in sectors])
    t = sectors[-1]["arr"].strftime("%H:%M") if isinstance(sectors[-1].get("arr"), datetime) else ""
    return (f"<div class='chip chip-cont'>↳ <b>{title}</b> lands {t}</div>" if t
            else f"<div class='chip chip-cont'>↳ <b>{title}</b></div>")


def build_calendar_html(rows, span=None):
    valid = [r for r in rows if r["DateObj"] is not None]
    if not valid:
        return "<div style='color:#7e8ba0;font-size:14px;'>No dated duties parsed.</div>"
    # Non-flight duties map onto every day they cover (multi-day HTL / SB / OFF);
    # flights are grouped into DUTIES so a turnaround shows as ONE chip with
    # both legs (e.g. 'UL404/5') instead of only the return leg.
    rmap = {}
    for r in rows:
        if not r["DateObj"] or r["Type"] == "FLIGHT":
            continue
        if r["Type"] == "LAYOVER":
            stn = r["Route"] if r["Route"] != "-" else "Layover"
            chip = f"<div class='chip chip-lay'>🏨 {stn}</div>"
        elif r["Type"] == "DAY OFF":
            chip = "<div class='chip chip-off'>🟢 OFF</div>"
        elif r["Type"] == "STANDBY":
            code = r.get("Code") or "SB"
            s0 = r.get("CIdt") or r.get("DEPdt")
            s1 = r.get("COdt") or r.get("ARRdt")
            t = ""
            if isinstance(s0, datetime) and isinstance(s1, datetime):
                t = f"{s0.strftime('%H:%M')}–{s1.strftime('%H:%M')}"
                if s1.date() != s0.date():
                    t = f"{s0.day} {s0.strftime('%H:%M')}–{s1.day} {s1.strftime('%H:%M')}"
            chip = f"<div class='chip chip-sby'>⏱ <b>{code}</b>{(' · ' + t) if t else ''}</div>"
        elif r["Type"] == "LEAVE":
            chip = "<div class='chip chip-lay'>🌴 Leave</div>"
        elif r["Type"] == "DUTY":
            chip = f"<div class='chip chip-duty'>📚 {r.get('Code') or 'Duty'}</div>"
        else:
            continue
        d0 = r["DateObj"].date()
        d1 = r["EndDateObj"].date() if r.get("EndDateObj") else d0
        d = d0
        while d <= d1:
            rmap.setdefault(d, []).append(chip)
            d += timedelta(days=1)
    for grp in _calendar_duties(rows):
        first, last = grp[0], grp[-1]
        dep_dt = first.get("dep")
        if not isinstance(dep_dt, datetime):
            continue
        # A duty spans check-in → check-out: UL254 dep 8th lands 9th, UL191 CI
        # 3rd dep 4th. Show the full chip on the main (departure) day, a
        # "duty starts" marker on the check-in day, and a "lands/CO" marker on
        # the check-out day so every touched calendar day shows the duty.
        start = first.get("ci") or dep_dt
        end = last.get("co") or last.get("arr")
        if not isinstance(end, datetime):
            end = start
        main_day = dep_dt.date()
        d0, d1 = start.date(), end.date()
        d = d0
        while d <= d1:
            if d == main_day:
                chip = _duty_chip(grp)
            elif d < main_day:
                chip = _duty_begin_chip(grp)
            else:
                chip = _duty_cont_chip(grp)
            rmap.setdefault(d, []).append(chip)
            d += timedelta(days=1)
    rmin = min(r["DateObj"] for r in valid).date()
    rmax = max((r["EndDateObj"] or r["DateObj"]) for r in valid).date()
    if span:
        dmin, dmax = span
    else:
        dmin, dmax = rmin, rmax
    start = dmin - timedelta(days=dmin.weekday())
    end = dmax + timedelta(days=6 - dmax.weekday())
    today = datetime.now().date()
    cells = ["<div class='cal'>"]
    for wd in ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]:
        cells.append(f"<div class='cal-hd'>{wd}</div>")
    d = start
    while d <= end:
        in_period = dmin <= d <= dmax
        cls = "cal-cell" + ("" if in_period else " cal-dim") + (" cal-today" if d == today else "")
        chips = list(rmap.get(d, []))
        if in_period and (rmin <= d <= rmax) and not chips:
            chips.append("<div class='chip chip-none'>No duty</div>")
        cells.append(f"<div class='{cls}'><div class='cal-date'>{d.day} {d.strftime('%b') if d.day == 1 or d == start else ''}</div>{''.join(chips)}</div>")
        d += timedelta(days=1)
    cells.append("</div>")
    return "".join(cells)


# --- 3.7 SALARY ENGINE (ported from the FAU sheet formulas + code.gs) ---
HOURLY_PAY = {   # category: (hourly $ rate <75h, hourly $ rate >75h)
    "PUR>10Yrs": (20.0, 38.0), "PUR(5-10Yrs)": (20.0, 38.0), "PUR<5Yrs": (20.0, 38.0),
    "CS>5Yrs": (13.5, 32.0), "CS<5Yrs": (13.5, 32.0),
    "C3": (8.5, 26.0), "C2": (6.5, 23.0), "C1": (6.5, 11.5), "C": (6.5, 11.5),
}
SPECIAL_PREMIUM = {"PUR>10Yrs": 35000, "PUR(5-10Yrs)": 30000, "PUR<5Yrs": 25000,
                   "CS>5Yrs": 15000, "CS<5Yrs": 10000, "C3": 5000, "C2": 0, "C1": 0, "C": 0}
OVERNIGHT_RATE_USD = {"PUR>10Yrs": 30, "PUR(5-10Yrs)": 30, "PUR<5Yrs": 30,
                      "CS>5Yrs": 20, "CS<5Yrs": 20, "C3": 18, "C2": 15, "C1": 15, "C": 15}
LEAVE_RATE_RS = {"PUR>10Yrs": 2000, "PUR(5-10Yrs)": 2000, "PUR<5Yrs": 2000,
                 "CS>5Yrs": 1500, "CS<5Yrs": 1500, "C3": 1200, "C2": 800, "C1": 500, "C": 500}
MEAL_RATE_USD = 25.0
SPLIT_MIN = 4500          # 75h threshold in minutes
MEAL_TRIGGERS = [(7, 30), (12, 30), (19, 30)]   # B / L / D trigger instants
# FBPP — Flt Base Pro Pay: paid once per TURNAROUND, on the sector arriving CMB
# whose previous roster row is not HTL. $28 if scheduled duration > 4h, else $21.
FBPP_OVER4_USD, FBPP_UNDER4_USD, FBPP_SPLIT_MIN = 28, 21, 240
UL_SCHED_MIN = {  # scheduled durations (minutes) from the sheet's "UL #" tab
    "UL102": 170, "UL104": 170, "UL116": 170, "UL1174": 175, "UL120": 230,
    "UL122": 170, "UL124": 170, "UL128": 170, "UL132": 120, "UL134": 120,
    "UL138": 110, "UL140": 110, "UL142": 300, "UL144": 305, "UL152": 430,
    "UL154": 470, "UL162": 120, "UL166": 145, "UL168": 145, "UL172": 175,
    "UL174": 175, "UL178": 230, "UL182": 445, "UL184": 430, "UL186": 470,
    "UL190": 390, "UL192": 430, "UL196": 430, "UL208": 550, "UL218": 590,
    "UL226": 550, "UL232": 549, "UL264": 615, "UL266": 655, "UL303": 480,
    "UL309": 480, "UL315": 445, "UL365": 555, "UL405": 415, "UL604": 1235,
}

def apit_tax(t):
    """Sri Lanka APIT slabs — identical to Breakdown!G16."""
    if t <= 150000: return 0
    if t <= 233333.34: return (t - 150000) * 0.06
    if t <= 275000: return 5000 + (t - 233333.34) * 0.18
    if t <= 316666.67: return 12500 + (t - 275000) * 0.24
    if t <= 358333.34: return 22500 + (t - 316666.67) * 0.30
    return 35000 + (t - 358333.34) * 0.36

def parse_hhmm_minutes(s):
    h = re.search(r'(\d+)\s*h', s or "")
    m = re.search(r'(\d+)\s*m', s or "")
    return (int(h.group(1)) if h else 0) * 60 + (int(m.group(1)) if m else 0)

def _meal_counts(start, end):
    """Count B/L/D trigger instants inside [start, end) — same as CALCULATE_MEALS."""
    if not start or not end or end <= start:
        return 0, 0, 0
    out = [0, 0, 0]
    d = start.date() - timedelta(days=1)
    while d <= end.date():
        for i, (h, m) in enumerate(MEAL_TRIGGERS):
            t = datetime.combine(d, dtime(h, m))
            if start <= t < end:
                out[i] += 1
        d += timedelta(days=1)
    return tuple(out)

def compute_salary(rows, prof, acting=None):
    """Full payslip from parsed roster + crew profile. Mirrors the sheet:
    meal entitlements (P), on-board meal deduction (Q/OBOPMA), layover &
    turnaround overnights, SCHBLK-guaranteed split-rate productivity, APIT."""
    cat = prof["cat"]
    rate = float(prof["rate"])
    flights = [(i, r) for i, r in enumerate(rows) if r["Type"] == "FLIGHT"]

    def _block_mins(r):
        """True elapsed block minutes — UTC twins when available (handles the
        portal's mixed-local timestamps), else the local stamps as fallback."""
        if r.get("ARRdt_u") and r.get("DEPdt_u"):
            return max(0, int((r["ARRdt_u"] - r["DEPdt_u"]).total_seconds() // 60))
        if r.get("ARRdt") and r.get("DEPdt"):
            return max(0, int((r["ARRdt"] - r["DEPdt"]).total_seconds() // 60))
        return 0

    def win(r):
        return (r.get("CIdt") or r.get("DEPdt")), (r.get("COdt") or r.get("ARRdt"))

    def is_layover_paired(i):
        return any(0 <= j < len(rows) and rows[j]["Type"] == "LAYOVER" for j in (i - 1, i + 1))

    ent = [0, 0, 0]      # entitled meals B/L/D (sheet col P)
    ob = [0, 0, 0]       # on-board meals (sheet col Q — deducted)
    detail = []
    for i, r in flights:
        s, e = win(r)
        if s and e and is_layover_paired(i):
            b, l, d = _meal_counts(s, e)
            for k, v in enumerate((b, l, d)):
                ent[k] += v
                ob[k] += v
            detail.append((f"✈ {r['Flight / Code']} ({r['Route']})", f"{b}B {l}L {d}D", "on board"))

    l_nights = 0
    layover_allow = []      # per-layover allowance breakdown (dashboard estimator)
    claimed_flights = set()
    for i, r in enumerate(rows):
        if r["Type"] != "LAYOVER":
            continue
        pf = next((rows[j] for j in range(i - 1, -1, -1) if rows[j]["Type"] == "FLIGHT"), None)
        nf = next((rows[j] for j in range(i + 1, len(rows)) if rows[j]["Type"] == "FLIGHT"), None)
        hs = r.get("CIdt") or ((pf.get("COdt") or pf.get("ARRdt")) if pf else None)
        he = r.get("COdt") or ((nf.get("CIdt") or nf.get("DEPdt")) if nf else None)
        pf_end = (pf.get("COdt") or pf.get("ARRdt")) if pf else None
        ms = max(hs, pf_end) if (hs and pf_end) else (hs or pf_end)
        lay_meals = 0
        if ms and he:
            b, l, d = _meal_counts(ms, he)
            lay_meals = b + l + d
            for k, v in enumerate((b, l, d)):
                ent[k] += v
            detail.append((f"🏨 Layover {r['Route']}", f"{b}B {l}L {d}D", "hotel"))
        # meals eaten ON BOARD the flights into/out of this layover also belong
        # to this layover trip (paid as allowance, deducted from salary)
        for f in (pf, nf):
            if f is None or id(f) in claimed_flights:
                continue
            fs, fe = win(f)
            if fs and fe:
                fb, fl_, fd = _meal_counts(fs, fe)
                lay_meals += fb + fl_ + fd
                claimed_flights.add(id(f))
        # Layover O/N count — nights AWAY FROM BASE, from the outbound flight's
        # report (check-in) to the return flight's check-out after landing.
        # Mirrors the sheet's 'Hours & ONights' column I ("L Overnight"):
        #   INT(next_checkout_or_end) - INT(prev_checkin_or_checkout)
        prev_time = (pf.get("CIdt") or pf.get("COdt") or pf.get("ARRdt")) if pf else (r.get("CIdt") or hs)
        if nf is not None:
            next_time = nf.get("COdt") or nf.get("ARRdt") or he
        else:
            next_time = he or r.get("EndDateObj")
        lay_nights = 0
        if prev_time and next_time and next_time > prev_time:
            lay_nights = (next_time.date() - prev_time.date()).days
            l_nights += lay_nights
        layover_allow.append({
            "station": r["Route"] if r.get("Route") and r["Route"] != "-" else "LAY",
            "meals": lay_meals, "nights": lay_nights,
            "usd": lay_meals * MEAL_RATE_USD + lay_nights * OVERNIGHT_RATE_USD[cat],
        })

    t_on = 0
    for i, r in flights:
        if is_layover_paired(i):
            continue
        s, e = win(r)
        if s and e and s.date() != e.date():
            t_on += 1

    block_min = sum(_block_mins(r) for _, r in flights)
    # CLV (casual leave) is NOT paid — only ALV / RLV / ALP (sheet C17)
    leave_days = sum(1 for r in rows if r.get("Code") in ("ALV", "RLV", "ALP"))

    # --- FBPP: turnaround allowance (H&O col O formula) ---
    # Inbound sector to CMB, previous roster row not HTL → $28 if scheduled
    # duration > 4h else $21. Scheduled time from UL # table; falls back to
    # actual block time for flight numbers not in the table.
    fbpp_usd = 0.0
    fbpp_items = []
    fbpp_missing = []
    fbpp_overrides = (prof.get("fbpp_overrides") or {})
    for i, r in flights:
        _o, _d = _route_od(r["Route"])
        if _d != "CMB":
            continue
        if i - 1 >= 0 and rows[i - 1]["Type"] == "LAYOVER":
            continue  # returning from a layover — not a turnaround
        fl = str(r["Flight / Code"]).replace(" ", "").upper()
        # FBPP pays on the COMBINED up+down SCHEDULED time, keyed by the return
        # flight number (UL # table). Actual block time under-reports (e.g.
        # 2h + 1h50 = 3h50 < 4h), so we never fall back to actual silently.
        sched = UL_SCHED_MIN.get(fl) or fbpp_overrides.get(fl)
        if sched is None:
            hint = None
            pr = rows[i - 1] if i - 1 >= 0 else None
            if pr is not None:
                b1 = _block_mins(pr)
                b2 = _block_mins(r)
                if b1 > 0 and b2 > 0:
                    hint = b1 + b2
            fbpp_missing.append((fl, f"{_o} ➔ CMB", hint))
            continue
        amt = FBPP_OVER4_USD if sched > FBPP_SPLIT_MIN else FBPP_UNDER4_USD
        fbpp_usd += amt
        fbpp_items.append((f"{fl} {_o}➔CMB", sched, amt, False))
    fbpp_rs = fbpp_usd * rate

    # --- acting duty (sheet Breakdown!J12-J15) ---
    # Hours flown in a higher category are paid at the ACTING category's <75h
    # hourly rate (CS → CS<5Yrs $13.5, PUR → PUR<5Yrs $20), and are excluded
    # from the regular 75h-split productivity pay.
    act_min, act_pay_rs, act_detail = 0, 0.0, []
    acting_marks = acting or {}
    if acting_marks:
        for i, r in flights:
            if not r.get("DateObj"):
                continue
            key = f"{r.get('Code') or str(r['Flight / Code']).replace(' ', '')}@{r['DateObj'].date()}"
            cat_act = acting_marks.get(key)
            if not cat_act:
                continue
            lookup = {"CS": "CS<5Yrs", "PUR": "PUR<5Yrs"}.get(cat_act, cat_act)
            rate_act = HOURLY_PAY.get(lookup, (0, 0))[0]
            mins = _block_mins(r)
            act_min += mins
            act_pay_rs += (mins / 60) * rate_act * rate
            act_detail.append((r["Flight / Code"], cat_act, mins))

    # --- productivity pay (Breakdown!B6/B7/B8/C15) ---
    # Regular paid minutes: if total (regular + acting) is under SCHBLK the
    # guarantee tops the REGULAR part up to SCHBLK − acting; otherwise the
    # regular part is paid as flown.
    reg_min = block_min - act_min
    schblk_min = int(prof["schblk_min"])
    final_min = max(0, schblk_min - act_min) if block_min < schblk_min else reg_min
    m75, mex = min(final_min, SPLIT_MIN), max(0, final_min - SPLIT_MIN)
    r75, rex = HOURLY_PAY[cat]
    ob_count = sum(ob)
    ent_count = sum(ent)
    ob_deduct_rs = ob_count * MEAL_RATE_USD * rate
    productivity_rs = (m75 * r75 / 60 + mex * rex / 60) * rate - ob_deduct_rs

    premium = SPECIAL_PREMIUM[cat]
    leave_rs = leave_days * LEAVE_RATE_RS[cat]
    earnings = (float(prof["basic"]) + premium + float(prof["crge"])
                + productivity_rs + fbpp_rs + leave_rs + act_pay_rs)

    epf = round((float(prof["basic"]) + premium) * float(prof.get("epf_pct", 10)) / 100)
    tax = apit_tax(earnings)
    fest = 5000 if prof.get("festival") else 0
    deductions = (epf + float(prof["transport"]) + float(prof["medical"]) + float(prof["fau"])
                  + fest + float(prof.get("stamp", 0)) + float(prof.get("apiit", 0)) + tax)
    net = earnings - deductions

    meal_usd = ent_count * MEAL_RATE_USD
    on_usd = l_nights * OVERNIGHT_RATE_USD[cat]
    ta_on_usd = t_on * OVERNIGHT_RATE_USD[cat]
    return {
        "block_min": block_min, "final_min": final_min, "m75": m75, "mex": mex,
        "ent": ent, "ent_count": ent_count, "ob_count": ob_count, "detail": detail,
        "l_nights": l_nights, "t_on": t_on, "leave_days": leave_days,
        "layover_allow": layover_allow,
        "productivity_rs": productivity_rs, "ob_deduct_rs": ob_deduct_rs,
        "fbpp_usd": fbpp_usd, "fbpp_rs": fbpp_rs, "fbpp_items": fbpp_items,
        "fbpp_missing": fbpp_missing,
        "act_min": act_min, "act_pay_rs": act_pay_rs, "act_detail": act_detail,
        "premium": premium, "leave_rs": leave_rs,
        "earnings": earnings, "epf": epf, "tax": tax, "festival": fest,
        "deductions": deductions, "net": net,
        "meal_usd": meal_usd, "on_usd": on_usd, "ta_on_usd": ta_on_usd,
        "allow_usd_total": meal_usd + on_usd + ta_on_usd,
        "allow_rs_total": (meal_usd + on_usd + ta_on_usd) * rate,
    }

# --- 4. STREAMLIT CONFIG & UI ---
st.set_page_config(page_title="Crew Companion", page_icon="✈️", layout="wide")
init_db()

st.markdown("""
    <style>
    .stApp { background-color: #0b1420; color: #ffffff; }
    .block-container { padding-top: 1.2rem; }
    .card { background:#121e2c; border:1px solid #1f2b3a; border-radius:12px; padding:16px; margin-bottom:14px; }
    .card h5 { margin:0 0 10px 0; font-size:14px; color:#e8eef7; }
    .muted { color:#7e8ba0; font-size:11px; }
    .cal { display:grid; grid-template-columns:repeat(7,1fr); gap:6px; }
    .cal-hd { text-align:center; color:#7e8ba0; font-size:14px; padding:4px 0; }
    .cal-cell { background:#0f1926; border:1px solid #1f2b3a; border-radius:8px; min-height:96px; padding:6px 7px; }
    .cal-dim { opacity:.35; }
    .cal-today { border-color:#00bcd4; box-shadow:0 0 0 1px #00bcd4 inset; }
    .cal-date { font-size:13px; color:#9fb3c8; margin-bottom:4px; font-weight:600; }
    .chip { border-radius:6px; padding:4px 6px; font-size:12.5px; line-height:1.3; margin-bottom:4px; }
    .chip span { color:#9fb3c8; font-size:11px; }
    .chip-flt { background:#0d3340; color:#4dd0e1; }
    .chip-lay { background:#33260f; color:#ffb74d; }
    .chip-off { background:#12301f; color:#66bb6a; }
    .chip-sby { background:#251a38; color:#b39ddb; }
    .chip-duty { background:#1c2333; color:#9fb3c8; }
    .chip-none { background:transparent; color:#3b4a5e; }
    .chip .chip-win { color:#8fb0c4; font-size:10.5px; }
    .chip-begin { background:#0c2833; color:#4dd0e1; border:1px dashed #1f6b7a; }
    .chip-cont { background:#0c2833; color:#4dd0e1; border-left:3px solid #4dd0e1; }
    .hbar { display:flex; align-items:center; justify-content:space-between; background:#121e2c;
            border:1px solid #1f2b3a; border-radius:12px; padding:10px 18px; margin-bottom:14px; }
    .avatar { width:38px; height:38px; border-radius:50%; background:#0d3340; color:#4dd0e1;
              display:inline-flex; align-items:center; justify-content:center; font-weight:700; margin-right:10px; }
    .spot { background:#0f1926; border:1px solid #1f2b3a; border-radius:8px; padding:9px 11px;
            font-size:12px; margin:4px 6px 4px 0; display:inline-block; }
    .bidrow { display:flex; justify-content:space-between; font-size:12px; padding:7px 4px; border-bottom:1px solid #1f2b3a; }
    </style>
""", unsafe_allow_html=True)

if 'logged_in' not in st.session_state:
    st.session_state['logged_in'] = False
    st.session_state['username'] = ''
    st.session_state['full_name'] = ''
    st.session_state['rank'] = ''
if 'acked' not in st.session_state:
    st.session_state['acked'] = set()

# --- AUTHENTICATION SCREEN ---
if not st.session_state['logged_in']:
    col1, col2, col3 = st.columns([1, 2, 1])
    with col2:
        st.markdown("<h1 style='text-align: center;'>✈️ Crew Companion</h1>", unsafe_allow_html=True)
        st.markdown("<h3 style='text-align: center; color: #888;'>Enterprise Roster & Analytics Hub</h3>", unsafe_allow_html=True)

        tab_login, tab_reg = st.tabs(["Log In", "Register Account"])

        with tab_login:
            with st.form("login_form", clear_on_submit=False):
                user_input = st.text_input("Staff Email / Username", key="login_user_main")
                pass_input = st.text_input("Password", type="password", key="login_pass_main")
                login_submitted = st.form_submit_button("Access Dashboard", use_container_width=True)
            if login_submitted:
                user_record = login_user(user_input.strip(), pass_input)
                if user_record:
                    st.session_state['logged_in'] = True
                    st.session_state['username'] = user_record[0]
                    st.session_state['full_name'] = user_record[2]
                    st.session_state['rank'] = user_record[3]
                    st.rerun()
                else:
                    st.error("Invalid credentials.")

        with tab_reg:
            with st.form("register_form", clear_on_submit=False):
                new_user = st.text_input("Choose Username / Email", key="reg_user_main")
                new_pass = st.text_input("Choose Password", type="password", key="reg_pass_main")
                new_name = st.text_input("Full Name", key="reg_name_main")
                new_rank = st.selectbox("Rank", ["Senior Cabin Crew", "Cabin Crew", "Purser", "Flight Deck"], key="reg_rank_main")
                reg_submitted = st.form_submit_button("Create Account", use_container_width=True)
            if reg_submitted:
                missing = []
                if not new_user.strip():
                    missing.append("Username")
                if not new_pass.strip():
                    missing.append("Password")
                if not new_name.strip():
                    missing.append("Full Name")
                if missing:
                    st.warning(f"Please complete: {', '.join(missing)}.")
                elif add_user(new_user.strip(), new_pass, new_name.strip(), new_rank):
                    st.success("✅ Account created! Switch to the Log In tab.")
                else:
                    st.error("Username already taken.")

else:
    # ---------- DATA PREP ----------
    if 'current_roster' not in st.session_state:
        st.session_state['current_roster'] = load_roster_from_db(st.session_state['username'])
    active_text = st.session_state.get('current_roster', '')
    parsed_rows = parse_roster_text(active_text) if active_text else []
    analytics = compute_analytics(parsed_rows)
    # Allowance estimate for the dashboard reuses the SALARY CALCULATOR's own
    # logic (meals + layover overnights + T/A overnights) via compute_salary,
    # so the figure matches the salary tab exactly. Only allowances are shown.
    _saved_prof = load_profile(st.session_state['username'])
    _est_prof = {
        "cat": _saved_prof.get("cat", "C3") if _saved_prof.get("cat", "C3") in HOURLY_PAY else "C3",
        "rate": float(_saved_prof.get("rate", 318.56) or 318.56),
        "basic": float(_saved_prof.get("basic", 0.0) or 0.0),
        "crge": float(_saved_prof.get("crge", 10000.0) or 0.0),
        "transport": float(_saved_prof.get("transport", 1000.0) or 0.0),
        "medical": float(_saved_prof.get("medical", 500.0) or 0.0),
        "fau": float(_saved_prof.get("fau", 2100.0) or 0.0),
        "stamp": float(_saved_prof.get("stamp", 0.0) or 0.0),
        "apiit": float(_saved_prof.get("apiit", 0.0) or 0.0),
        "epf_pct": float(_saved_prof.get("epf_pct", 10.0) or 10.0),
        "festival": bool(_saved_prof.get("festival", False)),
        "schblk_min": parse_hhmm_minutes(_saved_prof.get("schblk", "70h 00m")) or 4200,
        "fbpp_overrides": _saved_prof.get("fbpp_overrides", {}) or {},
    }
    est_sal = compute_salary(parsed_rows, _est_prof) if parsed_rows else None
    valid_dates_all = [r["DateObj"].date() for r in parsed_rows if r["DateObj"] is not None]
    if valid_dates_all:
        _p0, _p1 = roster_period_bounds(min(valid_dates_all))
        month_label = f"Roster {_p0.strftime('%d %b')} – {_p1.strftime('%d %b %Y')}"
    else:
        _p0, _p1 = roster_period_bounds(datetime.now().date())
        month_label = f"Roster {_p0.strftime('%d %b')} – {_p1.strftime('%d %b %Y')}"

    # ---------- HEADER ----------
    initials = "".join(w[0] for w in st.session_state['full_name'].split()[:2]).upper() or "?"
    n_alerts = st.session_state.get('alert_count', 0)
    bell = f"🔔 <span style='color:#ff5252;font-weight:700;'>{n_alerts}</span>" if n_alerts else "🔔"
    hcol1, hcol2 = st.columns([6, 1])
    with hcol1:
        st.markdown(
            f"<div class='hbar'>"
            f"<div style='font-size:18px;font-weight:800;'>🌲 CrewAI &nbsp;<span style='font-weight:400;color:#9fb3c8;'>| Roster Companion — {month_label}</span></div>"
            f"<div style='display:flex;align-items:center;'>"
            f"<span style='margin-right:18px;font-size:16px;'>{bell}</span>"
            f"<span class='avatar'>{initials}</span>"
            f"<span style='font-size:13px;'>{st.session_state['full_name']}<br><span class='muted'>({st.session_state['rank']})</span></span>"
            f"</div></div>", unsafe_allow_html=True)
    with hcol2:
        if st.button("Log Out", use_container_width=True):
            st.session_state['logged_in'] = False
            st.rerun()

    page_dash, page_salary, page_fdp = st.tabs(["📋 Dashboard", "💰 Salary Calculator", "⏱ FDP Calculator"])

    with page_dash:
        left_col, main_col, right_col = st.columns([1, 2.3, 1.3])

        # ---------- LEFT: ANALYTICS & FATIGUE ----------
        with left_col:
            st.markdown("#### Analytics & Fatigue Tracker")
            pct = analytics["block_hrs"] / analytics["block_target"] if analytics["block_target"] else 0
            donut = donut_svg(pct, str(analytics["block_hrs"]), f"of {analytics['block_target']} hrs")
            st.markdown(
                f"<div class='card' style='text-align:center;'><h5>Cumulative Block Hours</h5>"
                f"{donut}"
                f"<div class='muted'>this roster ({pct*100:.0f}%) · {analytics['flights']} sectors</div></div>",
                unsafe_allow_html=True)
            spark = sparkline_svg([analytics['daily_min'].get(d, 0) for d in sorted(analytics['daily_min'])] or [0])
            fat_color = "#4caf50" if analytics['fatigue'] < 4 else ("#ff9800" if analytics['fatigue'] < 7 else "#ff5252")
            st.markdown(
                f"<div class='card' style='text-align:center;'><h5>Fatigue Score</h5>"
                f"{gauge_svg(analytics['fatigue'])}"
                f"<div style='color:{fat_color};font-weight:700;font-size:14px;'>{analytics['fatigue_label']} ({analytics['fatigue']}/10)</div>"
                f"<div style='margin-top:6px;'>{spark}</div>"
                f"<div class='muted'>{analytics['redeyes']} red-eye dep · {analytics['max_streak']} consecutive duty days</div>"
                f"<div class='muted' style='margin-top:4px;'>1.5 + 1.4·redeyes + 0.7·max-streak + 3.0·(block/85) — capped at 10</div></div>",
                unsafe_allow_html=True)
            if est_sal:
                est_total = est_sal["meal_usd"] + est_sal["on_usd"]
                rows_html = ""
                for lv in est_sal.get("layover_allow", []):
                    city = STATION_INFO.get(lv["station"], (lv["station"],))[0]
                    rows_html += (
                        f"<div class='bidrow'><span>{city} ({lv['station']})"
                        f"<span class='muted'> · {lv['nights']} night(s), {lv['meals']} meals</span></span>"
                        f"<span>~${lv['usd']:,.0f}</span></div>"
                    )
                if not rows_html:
                    rows_html = "<div class='muted'>No layovers in this roster.</div>"
                st.markdown(
                    f"<div class='card'><h5>Estimated Allowances</h5>"
                    f"<div style='font-size:26px;font-weight:800;color:#4caf50;'>${est_total:,.0f} USD</div>"
                    f"<div class='muted' style='margin-bottom:6px;'>Per-layover breakdown (meals + overnights)</div>{rows_html}</div>",
                    unsafe_allow_html=True)
            else:
                st.markdown(
                    f"<div class='card'><h5>Estimated Allowances</h5>"
                    f"<div class='muted'>No roster parsed — paste your roster above.</div></div>",
                    unsafe_allow_html=True)

        # ---------- CENTER: CALENDAR + LAYOVER INTEL ----------
        with main_col:
            st.markdown("#### Main Roster Calendar View")
            with st.expander("📝 Paste Roster (Instant Parse)"):
                roster_input = st.text_area("Paste Raw Roster Here", value=st.session_state['current_roster'], height=120)
                if st.button("Auto-Process Roster"):
                    if roster_input.strip():
                        save_roster_to_db(st.session_state['username'], roster_input)
                        st.session_state['current_roster'] = roster_input
                        st.success("Roster updated!")
                        st.rerun()
                    else:
                        st.warning("Please paste roster text.")

            # 28-day roster period navigation (anchored to known 04Oct26-01Nov26 period)
            if valid_dates_all:
                pmin = roster_period_bounds(min(valid_dates_all))[0]
                pmax = roster_period_bounds(max(valid_dates_all))[0]
                periods, p = [], pmin
                while p <= pmax:
                    periods.append((p, p + timedelta(days=ROSTER_PERIOD_DAYS)))
                    p += timedelta(days=ROSTER_PERIOD_DAYS)
                starts = [x[0] for x in periods]
                t0 = roster_period_bounds(datetime.now().date())[0]
                if 'cal_period_idx' not in st.session_state or st.session_state['cal_period_idx'] >= len(periods):
                    st.session_state['cal_period_idx'] = starts.index(t0) if t0 in starts else 0
                idx = st.session_state['cal_period_idx']
                nav1, nav2, nav3 = st.columns([1, 4, 1])
                with nav1:
                    if st.button("‹", use_container_width=True, disabled=idx == 0):
                        st.session_state['cal_period_idx'] -= 1
                        st.rerun()
                with nav3:
                    if st.button("›", use_container_width=True, disabled=idx == len(periods) - 1):
                        st.session_state['cal_period_idx'] += 1
                        st.rerun()
                with nav2:
                    p0, p1 = periods[idx]
                    st.markdown(f"<div style='text-align:center;font-weight:700;padding-top:6px;'>Roster Period: {p0.strftime('%d %b')} – {p1.strftime('%d %b %Y')}</div>", unsafe_allow_html=True)
                sel_span = periods[idx]
            else:
                sel_span = None

            st.markdown(f"<div class='card'>{build_calendar_html(parsed_rows, span=sel_span)}</div>", unsafe_allow_html=True)

            # Layover Intel
            layovers = [lv for lv in analytics["layovers"] if lv["station"]]
            if layovers:
                opts = list(range(len(layovers)))
                today = datetime.now().date()
                def_idx = next((i for i, lv in enumerate(layovers) if lv["date"] and lv["date"] >= today), 0)
                sel = st.selectbox("Layover Intel:", opts, index=def_idx,
                                   format_func=lambda i: f"{STATION_INFO.get(layovers[i]['station'], (layovers[i]['station'],))[0]} ({layovers[i]['station']}) — {layovers[i]['date'].strftime('%d %b') if layovers[i]['date'] else '?'}")
                lv = layovers[sel]
                wx = fetch_station_weather(lv["station"])
                info = STATION_INFO.get(lv["station"])
                spots = (info[4] if info and info[4] else DEFAULT_SPOTS)
                city = wx["city"] if wx else lv["station"]
                apname = AIRPORT_NAME.get(lv["station"], "")
                wx_html = (f"{wx['icon']} {wx['temp']}°C · {wx['desc']}" if wx and wx["temp"] is not None else "n/a")
                lt_html = f"{wx['local_time']} ({wx['gmt']})" if wx else "-"
                gt_html = f"{lv['ground_hrs']} hrs" if lv["ground_hrs"] else "-"
                spots_html = "".join(f"<span class='spot'>{s}</span>" for s in spots)
                st.markdown(
                    f"<div class='card' style='border-color:#00bcd4;'>"
                    f"<h5>🏨 Layover Intel: {city} ({lv['station']})" + (f" — {lv['date'].strftime('%d %b')}" if lv['date'] else "") + "</h5>"
                    + (f"<div class='muted' style='margin-bottom:8px;'>✈ {apname}</div>" if apname else "")
                    + f"<div style='display:flex;gap:28px;font-size:13px;margin-bottom:10px;'>"
                    f"<div><div class='muted'>Weather (live)</div>{wx_html}</div>"
                    f"<div><div class='muted'>Local Time</div>{lt_html}</div>"
                    f"<div><div class='muted'>Ground Time</div>{gt_html}</div></div>"
                    f"<div class='muted' style='margin-bottom:4px;'>Explore Spots</div>{spots_html}</div>",
                    unsafe_allow_html=True)
            elif active_text:
                st.info("No layovers detected in roster for Layover Intel.")
            else:
                st.info("Paste your roster above to populate the calendar, analytics and layover intel.")

            # ---------- ROSTER GUARDIAN: FAU SOFT-RULES AUDIT ----------
            if parsed_rows:
                st.markdown("#### 🛡 Roster Guardian — FAU Soft-Rules Audit")
                findings = audit_roster(parsed_rows)
                violations = [f for f in findings if f[0] == "violation"]
                notes = [f for f in findings if f[0] == "note"]
                if not findings:
                    st.markdown(
                        "<div class='card' style='border-color:#4caf50;'><span style='color:#a5d6a7;'>✅ No soft-rule breaches detected in this roster "
                        "(min rest, post-flight day-off entitlements, next-day assignment limits all OK).</span></div>",
                        unsafe_allow_html=True)
                else:
                    st.markdown(
                        f"<div class='card' style='border-color:#ff5252;'><b style='color:#ff8a8a;'>{len(violations)} possible breach(es)</b>"
                        + (f" · <span style='color:#ffb74d;'>{len(notes)} advisory note(s)</span>" if notes else "") +
                        "<div class='muted' style='margin-top:4px;'>Cross-check with crew control before filing — parser-based audit, scheduled times only.</div></div>",
                        unsafe_allow_html=True)
                    for sev, msg in findings:
                        if sev == "violation":
                            st.markdown(f"<div style='font-size:12.5px;background:#2c1f1f;border:1px solid #ff5252;color:#ff8a8a;padding:10px;border-radius:8px;margin-bottom:8px;'>⚠️ {msg}</div>", unsafe_allow_html=True)
                        else:
                            st.markdown(f"<div style='font-size:12.5px;background:#33260f;border:1px solid #ffc107;color:#ffd54f;padding:10px;border-radius:8px;margin-bottom:8px;'>ℹ️ {msg}</div>", unsafe_allow_html=True)
                with st.expander("📖 FAU quick reference (standby insertion & rules summary)"):
                    st.markdown("""
    - **Min base rest:** 17h30 from chocks-on to next flight/ground-duty report (not ground→ground).
    - **DEL / BOM / KHI turnarounds** arriving 12:00–23:59 → next day: no flight (turnaround **or** layover) before 23:00 report; 23:00–05:59 regional (<4h sector) only; anything after 06:00 on day 2.
    - **DEL / BOM / KHI turnarounds** arriving 00:01–11:59 → **24h rest** chocks-on to next report.
    - **Four-sector days** (arrival before 17:30) → next day: 1-sector layover after 18:00 only; same turnaround limits as above; **SBY4 only**.
    - **DXB / AUH / MCT turnarounds** → next day: 1-sector layover after 16:00 only; same turnaround limits; **SBY4 only**.
    - **UL231/232 (DXB)** → next day only SBY2 (06:00–18:00) or a flight within that window.
    - **LHR / FRA / CDG / FCO / MXP / NRT / SYD / MEL layovers & JED turnaround** → arrival day + **2 days off**.
    - **DOH / BAH / DMM turnarounds** → arrival day + **1 day off**.
    - **SIN / KUL / CGK morning turnarounds** (UL314/UL364, or a CMB–SIN departure 07:00–08:00) → next-day flights report after 18:00, or SBY4.
    - **Standby windows:** SBY1 00:01–11:59 · SBY2 06:00–18:00 · SBY3 12:00–23:59 · SBY4 18:00–05:59.
    - **Standby insertion if your flight cancels:** morning T/A (report after 06:00) → SBY2 · morning T/A (report before 06:00) → SBY1 · Middle-East flight reporting before 18:00 → SBY3 · midnight flight reporting before midnight → SBY4 · midnight flight reporting after midnight → SBY1.
    - Training/duty codes (SEP, SEC, CRM, DGR, …) count as a duty day — they are **not** days off.
                    """)

        # ---------- RIGHT: AGENT ALERTS + BIDDING ----------
        with right_col:
            st.markdown("#### Flight Monitoring Agent")
            agent_on = st.toggle("Agent: Real-Time Flight Monitor", value=True)

            available_roster_dates = sorted(set(valid_dates_all))
            if available_roster_dates:
                real_today = datetime.now().date()
                if real_today in available_roster_dates:
                    default_idx = available_roster_dates.index(real_today)
                else:
                    future = [d for d in available_roster_dates if d >= real_today]
                    default_idx = available_roster_dates.index(future[0]) if future else len(available_roster_dates) - 1
                simulated_today = st.selectbox(
                    "Roster anchor day (auto-set to today):",
                    options=available_roster_dates, index=default_idx,
                    format_func=lambda x: x.strftime("%d %b %Y") + ("  ← today" if x == real_today else ""))
            else:
                simulated_today = datetime.now().date()
            simulated_tomorrow = simulated_today + timedelta(days=1)

            active_target_flights, seen = [], set()
            for row in parsed_rows:
                if row["Type"] == "FLIGHT" and row["DateObj"] is not None:
                    fd = row["DateObj"].date()
                    if fd in [simulated_today, simulated_tomorrow]:
                        key = (row["Flight / Code"], fd)
                        if key in seen:
                            continue
                        seen.add(key)
                        active_target_flights.append({"flight_no": row["Flight / Code"], "date_obj": fd,
                                                      "route": row["Route"], "dep_time": row["Departure"]})

            flight_check_results = []
            if agent_on and active_target_flights:
                with st.spinner("Querying FlightStats & Flightradar24 live feeds..."):
                    for flight in active_target_flights:
                        telemetry = fetch_live_flight_telemetry(flight["flight_no"], flight["date_obj"],
                                                                flight["route"], flight["dep_time"])
                        # Rest-period impact for delayed/diverted flights
                        rest_note = None
                        if telemetry.get("severity") in ("delayed", "diverted"):
                            delay_guess = None
                            m = re.search(r'by ~(\d+) min', telemetry.get("status_message", ""))
                            if m:
                                delay_guess = int(m.group(1))
                            rest_note = rest_impact_note(parsed_rows, flight["flight_no"],
                                                         flight["date_obj"], delay_guess or 0)
                        # Cancellation → soft standby suggestion (roster unchanged)
                        sb_suggest = None
                        if telemetry.get("severity") == "cancelled":
                            frow = next((r for r in parsed_rows
                                         if r["Type"] == "FLIGHT" and r.get("DateObj")
                                         and r["DateObj"].date() == flight["date_obj"]
                                         and str(r["Flight / Code"]).replace(" ", "") == str(flight["flight_no"]).replace(" ", "")), None)
                            if frow:
                                sb_suggest = suggest_standby_for_cancel(frow)
                        flight_check_results.append({"flight": flight["flight_no"], "route": flight["route"],
                                                     "date": flight["date_obj"].strftime("%d %b %Y"),
                                                     "delayed": telemetry["is_delayed"],
                                                     "severity": telemetry.get("severity", "ok"),
                                                     "status": telemetry["status_message"],
                                                     "inbound_note": telemetry.get("inbound_note"),
                                                     "inbound_risk": telemetry.get("inbound_risk", False),
                                                     "rest_note": rest_note,
                                                     "sb_suggest": sb_suggest})
            st.session_state['alert_count'] = sum(
                1 for f in flight_check_results
                if (f["severity"] in ("delayed", "cancelled", "diverted") or f["inbound_risk"])
                and (f["flight"], f["date"]) not in st.session_state['acked'])

            if not agent_on:
                st.markdown("<div class='card muted'>Real-time monitor paused.</div>", unsafe_allow_html=True)
            elif flight_check_results:
                st.markdown(
                    f"<div class='card' style='font-size:12px;'><b style='color:#00bcd4;'>Agent Scan:</b> "
                    f"Verified {len(flight_check_results)} flight(s) via FlightStats/Cirium + FR24 (keyless, cached 10 min).</div>",
                    unsafe_allow_html=True)
                for df in flight_check_results:
                    key = (df["flight"], df["date"])
                    acked = key in st.session_state['acked']
                    if df["severity"] == "cancelled":
                        bc, bg, tc, icon = "#ff1744", "#331414", "#ff8a8a", "🚫"
                    elif df["severity"] == "diverted":
                        bc, bg, tc, icon = "#ff6d00", "#332414", "#ffb74d", "🔀"
                    elif df["severity"] == "delayed":
                        bc, bg, tc, icon = "#ff5252", "#2c1f1f", "#ff8a8a", "⚠️"
                    elif df["severity"] == "unknown":
                        bc, bg, tc, icon = "#607d8b", "#1c2429", "#b0bec5", "ℹ️"
                    else:
                        bc, bg, tc, icon = "#4caf50", "#12301f", "#a5d6a7", "✈️"
                    op = "opacity:.5;" if acked else ""
                    extra = ""
                    if df.get("inbound_note"):
                        inb_bc = "#ff6d00" if df["inbound_risk"] else "#ffc107"
                        extra += (f"<div style='margin-top:8px;padding:8px;border-radius:6px;background:#2b2413;"
                                  f"border:1px solid {inb_bc};color:#ffd54f;font-size:11.5px;'>{df['inbound_note']}</div>")
                    if df.get("rest_note"):
                        rest_bad = "BELOW" in df["rest_note"]
                        r_bc = "#ff5252" if rest_bad else "#4caf50"
                        r_tc = "#ff8a8a" if rest_bad else "#a5d6a7"
                        extra += (f"<div style='margin-top:8px;padding:8px;border-radius:6px;background:#131f2b;"
                                  f"border:1px solid {r_bc};color:{r_tc};font-size:11.5px;'>{df['rest_note']}</div>")
                    if df.get("sb_suggest"):
                        extra += (f"<div style='margin-top:8px;padding:8px;border-radius:6px;background:#2b2413;"
                                  f"border:1px solid #ffc107;color:#ffd54f;font-size:11.5px;'>"
                                  f"📋 Soft suggestion (roster unchanged): if cancelled, insert <b>{df['sb_suggest']}</b> standby.</div>")
                    st.markdown(
                        f"<div style='font-size:13px;background:{bg};padding:12px;border-radius:8px;margin-top:10px;border:1px solid {bc};{op}'>"
                        f"{icon} <b style='font-size:14px;'>{df['flight']}</b> ({df['route']}) — <span style='color:#ccc;'><i>{df['date']}</i></span>"
                        f"<div style='margin-top:5px;color:{tc};font-size:12px;'>{df['status']}</div>{extra}</div>",
                        unsafe_allow_html=True)
                    if (df["severity"] in ("delayed", "cancelled", "diverted") or df["inbound_risk"]) and not acked:
                        if st.button("Acknowledge", key=f"ack_{df['flight']}_{df['date']}", use_container_width=True):
                            st.session_state['acked'].add(key)
                            st.rerun()
                if st.button("🔄 Force Refresh Live Data", use_container_width=True):
                    fr24_fetch_flight_history.clear()
                    flightstats_fetch.clear()
                    fr24_fetch_by_reg.clear()
                    st.rerun()
            else:
                st.markdown(
                    f"<div class='card muted'>No flights found for {simulated_today.strftime('%d %b')} or {simulated_tomorrow.strftime('%d %b')}.</div>",
                    unsafe_allow_html=True)


    # ================= 💰 SALARY CALCULATOR PAGE =================
    with page_salary:
        st.markdown("#### 💰 Salary Calculator")
        st.markdown("<div class='muted' style='margin-bottom:10px;'>Computed from your saved roster — same engine as the FAU sheet (meals, overnights, SCHBLK guarantee, 75h split, APIT). Set your profile once; everything else is automatic.</div>", unsafe_allow_html=True)

        saved = load_profile(st.session_state['username'])
        pcol, rcol = st.columns([1, 2.2])

        with pcol:
            st.markdown("##### Crew Profile")
            cats = list(HOURLY_PAY.keys())
            cat = st.selectbox("Category", cats, index=cats.index(saved.get("cat", "C3")) if saved.get("cat", "C3") in cats else 5)
            usd_rate = st.number_input("USD → LKR rate", value=float(saved.get("rate", 318.56)), step=0.01, format="%.2f")
            schblk = st.text_input("SCHBLK (scheduled block hrs)", value=saved.get("schblk", "70h 00m"))
            basic = st.number_input("Basic Salary (Rs)", value=float(saved.get("basic", 0.0)), step=500.0)
            festival = st.toggle("Festival Advance taken (Rs 5,000)", value=bool(saved.get("festival", False)))
            with st.expander("⚙️ Advanced (salary components & deductions)"):
                crge = st.number_input("CRGE (Rs)", value=float(saved.get("crge", 10000.0)), step=500.0)
                transport = st.number_input("Transport deduction (Rs)", value=float(saved.get("transport", 1000.0)), step=100.0)
                medical = st.number_input("Medical contribution (Rs)", value=float(saved.get("medical", 500.0)), step=100.0)
                fau = st.number_input("FAU subs (Rs)", value=float(saved.get("fau", 2100.0)), step=100.0)
                stamp = st.number_input("Stamp duty (Rs)", value=float(saved.get("stamp", 0.0)), step=5.0)
                apiit = st.number_input("APIIT (Rs)", value=float(saved.get("apiit", 0.0)), step=100.0)
                epf_pct = st.number_input("EPF %", value=float(saved.get("epf_pct", 10.0)), step=1.0)
            if st.button("💾 Save Profile", use_container_width=True):
                save_profile(st.session_state['username'],
                             {"cat": cat, "rate": usd_rate, "schblk": schblk, "festival": festival,
                              "basic": basic, "crge": crge, "transport": transport,
                              "medical": medical, "fau": fau, "stamp": stamp, "apiit": apiit,
                              "epf_pct": epf_pct})
                st.success("Profile saved — it will load automatically next time.")

        with rcol:
            # --- performed roster input (salary is based on the PERFORMED month, not the live roster) ---
            if 'performed_roster' not in st.session_state:
                st.session_state['performed_roster'] = load_performed_roster(st.session_state['username'])
            perf_saved = st.session_state.get('performed_roster', '')
            with st.expander("📋 Performed Roster (paste here)", expanded=not bool(perf_saved)):
                st.markdown("<div class='muted' style='margin-bottom:6px;'>Salary is calculated per <b>calendar month (1st – end)</b>. Paste your <b>performed</b> roster from the crew portal — it can span two months / roster periods; you'll pick the salary month below. Saved separately from your live roster.</div>", unsafe_allow_html=True)
                perf_input = st.text_area("Performed roster text", value=perf_saved, height=160,
                                          label_visibility="collapsed",
                                          placeholder="Paste your performed roster for the month here...")
                pb1, pb2 = st.columns([1, 1])
                if pb1.button("⚙️ Process & Save", use_container_width=True, key="perf_save"):
                    st.session_state['performed_roster'] = perf_input
                    save_performed_roster(st.session_state['username'], perf_input)
                    st.rerun()
                if pb2.button("🗑 Clear", use_container_width=True, key="perf_clear"):
                    st.session_state['performed_roster'] = ''
                    save_performed_roster(st.session_state['username'], '')
                    st.rerun()

            perf_text = st.session_state.get('performed_roster', '')
            perf_rows_all = parse_roster_text(perf_text) if perf_text.strip() else []
            # Salary is per CALENDAR MONTH (1st–end), while rosters run in 28-day
            # periods that straddle months — so filter duties to the chosen month.
            months = sorted({(r["DateObj"].year, r["DateObj"].month)
                             for r in perf_rows_all if r.get("DateObj")})
            perf_rows = perf_rows_all
            sel_month = None
            if months:
                def _mcount(ym):
                    return sum(1 for r in perf_rows_all if r.get("DateObj")
                               and (r["DateObj"].year, r["DateObj"].month) == ym
                               and r["Type"] == "FLIGHT")
                default_ym = max(months, key=lambda ym: (_mcount(ym), ym))
                labels = [datetime(y, m, 1).strftime("%B %Y") for y, m in months]
                pick = st.selectbox("Salary month (1st – end of month)", labels,
                                    index=months.index(default_ym))
                sel_month = months[labels.index(pick)]
                perf_rows = [r for r in perf_rows_all if r.get("DateObj")
                             and (r["DateObj"].year, r["DateObj"].month) == sel_month]
            flights_exist = any(r["Type"] == "FLIGHT" for r in perf_rows)
            if not flights_exist:
                st.info("Paste your performed roster above and hit Process & Save — the payslip is computed from it instantly.")
            else:
                _pd = [r["DateObj"].date() for r in perf_rows if r.get("DateObj")]
                if _pd:
                    _n_f = sum(1 for r in perf_rows if r["Type"] == "FLIGHT")
                    _mlabel = datetime(sel_month[0], sel_month[1], 1).strftime("%B %Y") if sel_month else ""
                    st.markdown(f"<div class='muted' style='margin-bottom:8px;'>✅ Computing <b>{_mlabel}</b>: <b>{_n_f} sectors</b> · duties {min(_pd).strftime('%d %b')} – {max(_pd).strftime('%d %b')}</div>", unsafe_allow_html=True)
                # --- acting duty marks (persisted per flight + date) ---
                acting_saved = saved.get("acting", {}) or {}
                flight_rows = [r for r in perf_rows if r["Type"] == "FLIGHT" and r.get("DateObj")]
                acting_live = {}
                if flight_rows:
                    with st.expander("🎭 Acting Duty (flights flown in a higher category)", expanded=bool(acting_saved)):
                        st.markdown("<div class='muted' style='margin-bottom:6px;'>Tick any flight you operated <b>acting</b> (e.g. C/C as CS, or CS as PUR). Acting hours are paid at the acting category's rate (<b>CS $13.5/h · PUR $20/h</b>) on top of your regular pay, and are excluded from the regular 75h-split.</div>", unsafe_allow_html=True)
                        for idx, r in enumerate(flight_rows):
                            code = r.get("Code") or str(r["Flight / Code"]).replace(" ", "")
                            skey = f"{code}@{r['DateObj'].date()}"
                            c1, c2 = st.columns([3.2, 1])
                            on = c1.checkbox(f"{r['Flight / Code']} · {r['Route']} · {r['DateObj'].strftime('%d %b')}",
                                             value=skey in acting_saved, key=f"act_{skey}_{idx}")
                            cat_act = c2.selectbox("Acting cat", ["CS", "PUR"],
                                                   index=1 if acting_saved.get(skey) == "PUR" else 0,
                                                   key=f"actcat_{skey}_{idx}", disabled=not on)
                            if on:
                                acting_live[skey] = cat_act
                        if st.button("💾 Save Acting Marks", use_container_width=True, key="act_save"):
                            marks = {}
                            for idx, r in enumerate(flight_rows):
                                code = r.get("Code") or str(r["Flight / Code"]).replace(" ", "")
                                skey = f"{code}@{r['DateObj'].date()}"
                                if st.session_state.get(f"act_{skey}_{idx}"):
                                    marks[skey] = st.session_state.get(f"actcat_{skey}_{idx}", "CS")
                            save_profile(st.session_state["username"], {**saved, "acting": marks})
                            st.success("Acting marks saved.")
                            st.rerun()

                prof = {"cat": cat, "rate": usd_rate, "schblk_min": parse_hhmm_minutes(schblk),
                        "festival": festival, "basic": basic, "crge": crge,
                        "transport": transport, "medical": medical, "fau": fau,
                        "stamp": stamp, "apiit": apiit, "epf_pct": epf_pct,
                        "fbpp_overrides": saved.get("fbpp_overrides", {})}
                s = compute_salary(perf_rows, prof, acting=acting_live)

                ta_on_rs = s['ta_on_usd'] * float(usd_rate)
                gross_no_ta_usd = s['allow_usd_total'] - s['ta_on_usd']
                gross_no_ta_rs = s['allow_rs_total'] - ta_on_rs
                h1, h2, h3, h4 = st.columns(4)
                h1.markdown(f"<div class='card' style='text-align:center;border-color:#4caf50;min-height:150px;'><div class='muted'>NET SALARY (Rs)</div><div style='font-size:22px;font-weight:800;color:#4caf50;'>Rs {s['net']:,.0f}</div><div class='muted'>after tax & deductions</div></div>", unsafe_allow_html=True)
                h2.markdown(f"<div class='card' style='text-align:center;min-height:150px;'><div class='muted'>TURNAROUND O/N (USD)</div><div style='font-size:22px;font-weight:800;color:#8bc34a;'>${s['ta_on_usd']:,.0f}</div><div class='muted'>≈ Rs {ta_on_rs:,.0f}</div><div class='muted' style='margin-top:6px;'>{s['t_on']} overnight(s) × ${OVERNIGHT_RATE_USD[cat]}</div></div>", unsafe_allow_html=True)
                h3.markdown(f"<div class='card' style='text-align:center;min-height:150px;'><div class='muted'>ALLOWANCES (USD — gross)</div><div style='font-size:22px;font-weight:800;color:#00bcd4;'>${gross_no_ta_usd:,.0f}</div><div class='muted'>≈ Rs {gross_no_ta_rs:,.0f}</div><div class='muted' style='margin-top:6px;'>Layover Meal Allowance + Layover Overnights</div></div>", unsafe_allow_html=True)
                h4.markdown(f"<div class='card' style='text-align:center;min-height:150px;'><div class='muted'>TOTAL TAKE-HOME</div><div style='font-size:22px;font-weight:800;color:#ffb74d;'>Rs {s['net'] + s['allow_rs_total']:,.0f}</div><div class='muted'>Salary + All Allowances + T/A Overnight Allowances</div></div>", unsafe_allow_html=True)

                # earnings composition bar
                parts = [("Productivity", max(s['productivity_rs'], 0), "#00bcd4"),
                         ("Premium", s['premium'], "#8bc34a"),
                         ("CRGE", float(crge), "#ffb74d"),
                         ("Basic", float(basic), "#b39ddb"),
                         ("FBPP", s['fbpp_rs'], "#f06292"),
                         ("Duty Day", s['leave_rs'], "#4dd0e1"),
                         ("Acting", s['act_pay_rs'], "#ce93d8")]
                tot = sum(p[1] for p in parts) or 1
                seg = "".join(f"<div style='width:{100*v/tot:.1f}%;background:{c};height:14px;'></div>" for _, v, c in parts if v > 0)
                leg = " ".join(f"<span style='font-size:11px;color:{c};'>■ {n}</span>" for n, v, c in parts if v > 0)
                st.markdown(f"<div class='card'><div class='muted' style='margin-bottom:6px;'>Earnings composition — Rs {s['earnings']:,.0f} total</div><div style='display:flex;border-radius:6px;overflow:hidden;'>{seg}</div><div style='margin-top:6px;'>{leg}</div></div>", unsafe_allow_html=True)

                b1, b2 = st.columns(2)
                with b1:
                    st.markdown(
                        "<div class='card'><h5>Earnings (Rs)</h5>"
                        + f"<div class='bidrow'><span>Basic Salary</span><span>{basic:,.0f}</span></div>"
                        + f"<div class='bidrow'><span>Special Premium ({cat})</span><span>{s['premium']:,.0f}</span></div>"
                        + f"<div class='bidrow'><span>CRGE</span><span>{crge:,.0f}</span></div>"
                        + f"<div class='bidrow'><span>Productivity Pay*</span><span>{s['productivity_rs']:,.1f}</span></div>"
                        + f"<div class='bidrow'><span>Flt Base Pro Pay ({len(s['fbpp_items'])} T/A · ${s['fbpp_usd']:,.0f})</span><span>{s['fbpp_rs']:,.1f}</span></div>"
                        + f"<div class='bidrow'><span>Duty Day Pay ({s['leave_days']}d)</span><span>{s['leave_rs']:,.0f}</span></div>"
                        + (f"<div class='bidrow'><span>Acting Pay ({s['act_min']//60}h {s['act_min']%60}m)</span><span>{s['act_pay_rs']:,.0f}</span></div>" if s["act_min"] else "")
                        + f"<div class='bidrow'><b>Total Earnings</b><b>{s['earnings']:,.1f}</b></div>"
                        + f"<div class='muted' style='margin-top:6px;'>*{s['final_min']//60}h {s['final_min']%60}m paid ({s['m75']}min ≤75h + {s['mex']}min >75h), minus {s['ob_count']} on-board meals (−Rs {s['ob_deduct_rs']:,.0f}). Flown: {s['block_min']//60}h {s['block_min']%60}m.</div>"
                        + "</div>", unsafe_allow_html=True)
                with b2:
                    st.markdown(
                        "<div class='card'><h5>Deductions (Rs)</h5>"
                        + (f"<div class='bidrow' style='color:#ffb74d;'><span>On-board meals (O/B Overpayed M/A · {s['ob_count']} meals)</span><span>−Rs {s['ob_deduct_rs']:,.0f}</span></div>"
                           + "<div class='muted' style='margin-bottom:6px;'>Already deducted inside Productivity Pay — shown here so it's not a mystery.</div>"
                           if s['ob_count'] else "")
                        + f"<div class='bidrow'><span>EPF ({epf_pct:.0f}%)</span><span>{s['epf']:,.0f}</span></div>"
                        + f"<div class='bidrow'><span>Transport</span><span>{transport:,.0f}</span></div>"
                        + f"<div class='bidrow'><span>Medical</span><span>{medical:,.0f}</span></div>"
                        + f"<div class='bidrow'><span>FAU Subs</span><span>{fau:,.0f}</span></div>"
                        + f"<div class='bidrow'><span>Festival Advance</span><span>{s['festival']:,.0f}</span></div>"
                        + f"<div class='bidrow'><span>Stamp / APIIT</span><span>{stamp + apiit:,.0f}</span></div>"
                        + f"<div class='bidrow'><span>APIT Tax</span><span>{s['tax']:,.1f}</span></div>"
                        + f"<div class='bidrow'><b>Total Deductions</b><b>{s['deductions']:,.1f}</b></div>"
                        + "</div>", unsafe_allow_html=True)

                ob_usd = s['ob_count'] * MEAL_RATE_USD
                net_allow_usd = gross_no_ta_usd - ob_usd
                st.markdown(
                    "<div class='card'><h5>Allowances (USD — paid separately)</h5>"
                    + f"<div class='bidrow'><span>Meals: {s['ent'][0]}B {s['ent'][1]}L {s['ent'][2]}D ({s['ent_count']} × $25)</span><span>${s['meal_usd']:,.0f}</span></div>"
                    + f"<div class='bidrow'><span>Layover overnights ({s['l_nights']} × ${OVERNIGHT_RATE_USD[cat]})</span><span>${s['on_usd']:,.0f}</span></div>"
                    + f"<div class='bidrow'><b>Total allowance earned (meals + layover)</b><b>${gross_no_ta_usd:,.0f} ≈ Rs {gross_no_ta_rs:,.0f}</b></div>"
                    + (f"<div class='bidrow' style='color:#ff8a8a;'><span>On-board meals deducted from salary ({s['ob_count']} × $25)</span><span>−${ob_usd:,.0f} ≈ −Rs {s['ob_deduct_rs']:,.0f}</span></div>"
                       + f"<div class='bidrow'><b>Net allowance you actually pocket</b><b>${net_allow_usd:,.0f} ≈ Rs {gross_no_ta_rs - s['ob_deduct_rs']:,.0f}</b></div>"
                       if s['ob_count'] else "")
                    + "<div class='muted' style='margin-top:6px;'>Turnaround overnight pay is shown in its own card above. You receive the full allowance in USD, but the on-board meal part is clawed back from your salary — so month-end you effectively pocket the <b>net</b> figure.</div>"
                    + "</div>", unsafe_allow_html=True)

                with st.expander("🍽 Meals breakdown (per duty)"):
                    st.markdown("<div class='muted' style='margin-bottom:6px;'>This is where each B / L / D meal came from: ✈ flight meals are eaten <b>on board</b> → deducted from salary · 🏨 hotel meals → paid as meal allowance.</div>", unsafe_allow_html=True)
                    for name, meals, kind in s["detail"]:
                        tag = "on-board → deducted from salary" if kind == "on board" else "hotel → paid as allowance"
                        color = "#ff8a8a" if kind == "on board" else "#a5d6a7"
                        st.markdown(f"<div class='bidrow'><span>{name}</span><span style='color:{color};'>{meals} <span class='muted'>({tag})</span></span></div>", unsafe_allow_html=True)

                if s["fbpp_items"]:
                    with st.expander(f"✈ FBPP turnaround detail ({len(s['fbpp_items'])} × T/A = ${s['fbpp_usd']:,.0f})"):
                        for name, sched, amt, approx in s["fbpp_items"]:
                            band = ">4h" if sched > FBPP_SPLIT_MIN else "≤4h"
                            st.markdown(f"<div class='bidrow'><span>{name} <span class='muted'>({sched//60}h {sched%60:02d}m {band} · scheduled up+down)</span></span><span>${amt}</span></div>", unsafe_allow_html=True)

                if s.get("fbpp_missing"):
                    st.markdown(
                        f"<div style='font-size:12.5px;background:#33260f;border:1px solid #ffc107;color:#ffd54f;padding:10px;border-radius:8px;margin-bottom:8px;'>"
                        f"⚠️ <b>FBPP:</b> {len(s['fbpp_missing'])} turnaround return flight(s) not in the scheduled-duration table — currently paying <b>$0</b> for them. "
                        f"Enter their <b>combined up+down scheduled</b> minutes below (pre-filled with actual flown time, which under-counts).</div>",
                        unsafe_allow_html=True)
                    for fl, route, hint in s["fbpp_missing"]:
                        c1, c2 = st.columns([3, 1])
                        ovr = c1.number_input(f"{fl} ({route}) — combined scheduled minutes",
                                              value=float(hint or 240), step=5.0, key=f"fbpp_{fl}")
                        if c2.button("💾 Save", key=f"fbpp_save_{fl}"):
                            fbpp_overrides = dict(saved.get("fbpp_overrides", {}) or {})
                            fbpp_overrides[fl] = int(ovr)
                            save_profile(st.session_state["username"], {**saved, "fbpp_overrides": fbpp_overrides})
                            st.success(f"{fl} saved — FBPP now uses {int(ovr)} min (${'28' if int(ovr) > FBPP_SPLIT_MIN else '21'}).")
                            st.rerun()

                st.markdown("<div class='muted' style='margin-top:8px;'>⚠️ Independent estimate for personal guidance only — refer to your official payslip for final figures.</div>", unsafe_allow_html=True)

    # ================= ⏱ FDP CALCULATOR PAGE =================
    with page_fdp:
        st.markdown("#### ⏱ FDP Calculator")
        st.markdown(
            "<div class='muted' style='margin-bottom:10px;'>Standalone Flight Duty Period calculator for "
            "<b>cabin crew</b> — FOM Part A Chapter 08. Enter all times in <b>Colombo (CMB) local time</b>. "
            "Max FDP = Table A/B value + 1:00 cabin-crew allowance (&sect;8.3.a).</div>", unsafe_allow_html=True)

        lcol, rcol2 = st.columns([1, 1.15])

        with lcol:
            st.markdown("##### 1 · Current duty")
            ci_date = st.date_input("Check-in (report) date", value=datetime.now().date(), key="fdp_ci_date")
            ci_t = st.time_input("Check-in (report) time", value=dtime(6, 15), key="fdp_ci_t") or dtime(6, 15)
            dep_t = st.time_input("First sector departure time", value=dtime(7, 35), key="fdp_dep_t") or dtime(7, 35)
            dep_next = st.checkbox("Departure is the day AFTER check-in", value=False, key="fdp_dep_next")
            arr_t = st.time_input("Final sector arrival (chocks-on) time", value=dtime(15, 45), key="fdp_arr_t") or dtime(15, 45)
            arr_next = st.checkbox("Chocks-on is the day AFTER departure", value=False, key="fdp_arr_next")
            sectors = st.selectbox("Sectors in this duty", list(range(1, 9)), index=1, key="fdp_sectors",
                                   format_func=lambda n: f"{n} sector" + ("" if n == 1 else "s"))

            st.markdown("##### 2 · Previous duty (optional)")
            st.markdown("<div class='muted' style='margin-top:-6px;margin-bottom:6px;'>Used for preceding rest "
                        "(Table B), the min-rest check and the acclimatization hint.</div>", unsafe_allow_html=True)
            use_prev = st.toggle("Include previous duty", value=False, key="fdp_use_prev")
            prev_ci_date = prev_ci_t = prev_co_date = prev_co_t = None
            prev_stn = "CMB"
            if use_prev:
                prev_ci_date = st.date_input("Previous check-in date", value=ci_date - timedelta(days=1), key="fdp_pci_date")
                prev_ci_t = st.time_input("Previous check-in time", value=dtime(6, 0), key="fdp_pci_t") or dtime(6, 0)
                prev_co_next = st.checkbox("Previous chocks-on was the day AFTER its check-in", value=False, key="fdp_pco_next")
                prev_co_t = st.time_input("Previous chocks-on time", value=dtime(18, 0), key="fdp_pco_t") or dtime(18, 0)
                prev_co_date = prev_ci_date + (timedelta(days=1) if prev_co_next else timedelta(0))
                stations = sorted(AIRPORT_OFFSET_H.keys())
                prev_stn = st.selectbox("Previous duty ended at (station)", stations,
                                        index=stations.index("CMB") if "CMB" in stations else 0, key="fdp_pstn")

            st.markdown("##### 3 · Acclimatization")
            sugg_no = False
            sugg_txt = "Yes — acclimatized (home base CMB)"
            sugg_col = "#4caf50"
            if use_prev and prev_stn:
                off = AIRPORT_OFFSET_H.get(prev_stn, 5.5)
                if abs(off - 5.5) > 2:
                    sugg_no = True
                    sugg_txt = f"No — last duty ended at {prev_stn} (UTC{off:+.1f}, more than 2h from CMB)"
                    sugg_col = "#ff8a8a"
            st.markdown(f"<div style='font-size:12px;color:{sugg_col};margin-bottom:6px;'>Suggestion: {sugg_txt} "
                        f"(override below)</div>", unsafe_allow_html=True)
            acclim = st.radio("Acclimatized at start of this duty?", ["Yes", "No"],
                              index=1 if sugg_no else 0, key="fdp_acclim")

        with rcol2:
            ci_dt = datetime.combine(ci_date, ci_t)
            dep_dt = datetime.combine(ci_date + (timedelta(days=1) if dep_next else timedelta(0)), dep_t)
            arr_dt = datetime.combine(dep_dt.date() + (timedelta(days=1) if arr_next else timedelta(0)), arr_t)

            band_t = (dep_dt - timedelta(hours=1)).time()
            band = fdp_band(band_t)

            prev_dur_h = preceding_rest_h = None
            if use_prev and prev_ci_date is not None and prev_co_date is not None:
                pci_dt = datetime.combine(prev_ci_date, prev_ci_t)
                pco_dt = datetime.combine(prev_co_date, prev_co_t)
                prev_dur_h = (pco_dt - pci_dt).total_seconds() / 3600
                if prev_dur_h <= 0:
                    prev_dur_h += 24
                preceding_rest_h = (ci_dt - pco_dt).total_seconds() / 3600
                if preceding_rest_h < 0:
                    preceding_rest_h += 24

            acclim_bool = (acclim == "Yes")
            max_fdp = fdp_limit_min(acclim_bool, band, sectors, preceding_rest_h)
            actual_fdp = int((arr_dt - ci_dt).total_seconds() // 60)
            if actual_fdp <= 0:
                actual_fdp += 24 * 60
            latest_co = ci_dt + timedelta(minutes=max_fdp)
            margin = max_fdp - actual_fdp
            flight_crew_val = max_fdp - 60

            if margin >= 0:
                vc_bg, vc_bc, vc_tc, vc_icon = "#12301f", "#4caf50", "#a5d6a7", "✅"
                v_title, v_sub = "WITHIN LIMITS", f"{_fmt_hm(margin)} to spare"
            else:
                vc_bg, vc_bc, vc_tc, vc_icon = "#331414", "#ff1744", "#ff8a8a", "❌"
                v_title, v_sub = "EXCEEDS MAX FDP", f"over by {_fmt_hm(-margin)}"
            st.markdown(
                f"<div class='card' style='text-align:center;background:{vc_bg};border:1px solid {vc_bc};'>"
                f"<div style='font-size:26px;'>{vc_icon}</div>"
                f"<div style='font-size:20px;font-weight:800;color:{vc_tc};'>{v_title}</div>"
                f"<div class='muted'>{v_sub}</div></div>", unsafe_allow_html=True)

            rows = [
                ("Local time of start (dep − 1h)", band_t.strftime("%H:%M") + f" → band <b>{band}</b>"),
                ("Sectors", f"{sectors}"),
                ("Table used", ("Table A — acclimatized" if acclim_bool else "Table B — not acclimatized")),
            ]
            if not acclim_bool and preceding_rest_h is not None:
                bucket = "between 18h and 30h" if 18 < preceding_rest_h < 30 else "up to 18h / over 30h"
                rows.append(("Preceding rest (Table B key)", f"{_fmt_hm(int(preceding_rest_h * 60))} → {bucket}"))
            rows += [
                ("Flight-crew table value", _fmt_hm(flight_crew_val)),
                ("Cabin crew (+1:00)", _fmt_hm(max_fdp)),
                ("FDP starts (check-in)", ci_dt.strftime("%d %b %H:%M")),
                ("Latest allowed chocks-on", latest_co.strftime("%d %b %H:%M")),
                ("Actual chocks-on", arr_dt.strftime("%d %b %H:%M")),
                ("Actual FDP", _fmt_hm(actual_fdp)),
            ]
            rows_html = "".join(f"<div class='bidrow'><span>{k}</span><span>{v}</span></div>" for k, v in rows)
            st.markdown(f"<div class='card'>{rows_html}</div>", unsafe_allow_html=True)

            this_dur_h = actual_fdp / 60
            need_rest_h = max(this_dur_h - 1, 11.0)
            earliest_next = arr_dt + timedelta(hours=need_rest_h)
            st.markdown(
                f"<div class='card' style='font-size:12.5px;'><b style='color:#00bcd4;'>Rest needed after this duty:</b> "
                f"max({_fmt_hm(actual_fdp)} − 1h, 11h) = <b>{_fmt_hm(int(need_rest_h * 60))}</b> · "
                f"earliest next check-in <b>{earliest_next.strftime('%d %b %H:%M')}</b></div>",
                unsafe_allow_html=True)

            if use_prev and prev_dur_h is not None and preceding_rest_h is not None:
                req_rest_h = max(prev_dur_h - 1, 11.0)
                rest_ok = preceding_rest_h >= req_rest_h
                rb_c, rb_t = ("#4caf50", "#a5d6a7") if rest_ok else ("#ff5252", "#ff8a8a")
                verdict = "✅ rest met" if rest_ok else f"⚠️ rest short by {_fmt_hm(int((req_rest_h - preceding_rest_h) * 60))}"
                st.markdown(
                    f"<div class='card' style='font-size:12.5px;'>"
                    f"<b style='color:#00bcd4;'>Previous duty:</b> duration <b>{_fmt_hm(int(prev_dur_h * 60))}</b> · "
                    f"rest before this duty <b>{_fmt_hm(int(preceding_rest_h * 60))}</b><br>"
                    f"Required rest after previous duty = max({_fmt_hm(int(prev_dur_h * 60))} − 1h, 11h) = "
                    f"<b>{_fmt_hm(int(req_rest_h * 60))}</b> → "
                    f"<span style='color:{rb_t};border-bottom:1px solid {rb_c};'>{verdict}</span></div>",
                    unsafe_allow_html=True)

            with st.expander("📖 FDP tables (Chapter 08)"):
                ta = "| Local time of start | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8+ |\n|---|---|---|---|---|---|---|---|---|\n"
                for bandk, row in FDP_TABLE_A.items():
                    ta += f"| {bandk} | " + " | ".join(_fmt_hm(v) for v in row) + " |\n"
                st.markdown("**Table A — Acclimatized** (flight crew; cabin crew +1:00)")
                st.markdown(ta)
                tb = "| Preceding rest | 1 | 2 | 3 | 4 | 5 | 6 | 7+ |\n|---|---|---|---|---|---|---|---|\n"
                for bandk, row in FDP_TABLE_B.items():
                    label = "Up to 18h / over 30h" if bandk == "up18_or_over30" else "Between 18h and 30h"
                    tb += f"| {label} | " + " | ".join(_fmt_hm(v) for v in row[:7]) + " |\n"
                st.markdown("**Table B — Not acclimatized** (flight crew; cabin crew +1:00)")
                st.markdown(tb)
                st.markdown("<div class='muted'>All times Colombo local. FDP = check-in → final chocks-on. "
                            "Local time of start = first departure − 1h (flight-crew report time).</div>",
                            unsafe_allow_html=True)
