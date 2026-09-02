import streamlit as st
import sqlite3
import hashlib
import re
import json
import requests
from datetime import datetime, timedelta, timezone, time as dtime

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
    Crew-portal exports often arrive as one long concatenated string.
    Split it into one duty per line: break before HTL blocks and before any
    'DDMMMYY HH:MM' stamp that directly starts a UL / SB / OFF duty.
    Harmless for rosters that already have proper line breaks.
    """
    t = raw_text.replace("\r", "\n")
    t = re.sub(r'[ \t]*HTL', '\nHTL', t)
    t = re.sub(r'(\d{2}[A-Z]{3}\d{2}\s*\d{2}:\d{2})\s*(?=UL\s*\d|SB|OFF|ROF|TOF)', r'\n\1', t)
    return t

def parse_roster_text(raw_text):
    lines = preprocess_roster_text(raw_text).split('\n')
    parsed_rows = []
    current_date_str = "-"
    current_dt_obj = None

    for line in lines:
        line_str = line.strip()
        if not line_str:
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

        if any(keyword in line_str for keyword in ["UL", "OFF", "HTL", "SB", "ROF", "TOF"]):
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
            elif activity_type == "STANDBY" and dt_stamps:
                ci_dt, co_dt = min(dt_stamps), max(dt_stamps)

            parsed_rows.append({
                "Date": row_date_str,
                "DateObj": row_dt_obj,
                "EndDateObj": end_dt_obj,
                "Type": activity_type,
                "Flight / Code": flight_no if flight_no != "-" else activity_type,
                "Check-In": checkin_time,
                "Departure": dep_time,
                "Route": route,
                "Arrival": arr_time,
                "Checkout": checkout_time,
                "Aircraft": ac_type,
                "CIdt": ci_dt, "DEPdt": dep_dt, "ARRdt": arr_dt, "COdt": co_dt
            })

    return parsed_rows

# --- 2.5 FAU SOFT-RULES AUDIT ENGINE (Roster Guardian) ---
LONGHAUL_2OFF_LAYOVER = {"LHR", "FRA", "CDG", "FCO", "MXP", "NRT", "HND", "SYD", "MEL"}
TWO_OFF_TURNAROUND = {"JED"}
ONE_OFF_TURNAROUND = {"DOH", "BAH", "DMM"}
SOUTHASIA_TA = {"DEL", "BOM", "KHI"}
MIDEAST_TA = {"DXB", "AUH", "MCT"}
SEASIA_MORNING_TA = {"SIN", "KUL", "CGK"}
MIN_BASE_REST_H = 17.5          # 17h30m chocks-on -> next report at base
REGIONAL_MAX_SECTOR_H = 4.0     # 'regional' = sector length under 4 hours

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
        sec = {"flight": r["Flight / Code"], "o": o, "d": d, "dep": r["DEPdt"], "arr": r["ARRdt"],
               "ci": r.get("CIdt"), "block_h": (r["ARRdt"] - r["DEPdt"]).total_seconds() / 3600}
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
        du["label"] = " / ".join(s["flight"] for s in du["sectors"])
        du["numbers"] = {s["flight"].replace(" ", "") for s in du["sectors"]}
    return duties

def audit_roster(rows):
    """Audit the parsed roster against FAU soft rules. Returns findings list
    of (severity, message) where severity is 'violation' or 'note'."""
    findings = []
    duties = build_duties(rows)

    # All future report events (flight duties + standby starts)
    events = [{"dt": du["report"], "kind": "FLIGHT", "duty": du, "label": du["label"]} for du in duties]
    for sb in [r for r in rows if r["Type"] == "STANDBY"]:
        sdt = sb.get("CIdt")
        if not sdt and sb["DateObj"] is not None and sb["Departure"] != "-":
            try:
                sdt = datetime.combine(sb["DateObj"].date(), datetime.strptime(sb["Departure"], "%H:%M").time())
            except ValueError:
                sdt = None
        if sdt:
            events.append({"dt": sdt, "kind": "STANDBY", "duty": None, "label": "Standby"})
    events.sort(key=lambda e: e["dt"])
    duty_day_map = {}
    for e in events:
        duty_day_map.setdefault(e["dt"].date(), []).append(e)

    def check_next_day_restrictions(du, arr, rule_name, layover_ok_after=None, layover_strict=False):
        """Common pattern: following-day turnaround/layover reporting limits."""
        day1 = arr.date() + timedelta(days=1)
        day2 = arr.date() + timedelta(days=2)
        for e in duty_day_map.get(day1, []) + duty_day_map.get(day2, []):
            if e["kind"] != "FLIGHT":
                continue  # standby insertion is allowed by the guidelines
            rep = e["dt"]
            nd = e["duty"]
            in_night_window = ((rep.date() == day1 and rep.time() >= dtime(23, 0)) or
                               (rep.date() == day2 and rep.time() <= dtime(5, 59)))
            if rep.date() == day2 and rep.time() >= dtime(6, 0):
                continue  # anything allowed after 06:00 on the second day
            if nd["is_turnaround"]:
                if rep.date() == day1 and rep.time() < dtime(23, 0):
                    findings.append(("violation", f"{rule_name}: after {du['label']} (arr {arr:%d %b %H:%M}) no turnaround may report before 23:00 next day — {nd['label']} reports {rep:%d %b %H:%M}."))
                elif in_night_window and nd["max_sector_h"] >= REGIONAL_MAX_SECTOR_H:
                    findings.append(("violation", f"{rule_name}: between 23:00–05:59 only a regional turnaround (<{REGIONAL_MAX_SECTOR_H:.0f}h sector) is allowed — {nd['label']} ({nd['max_sector_h']:.1f}h sector) reports {rep:%d %b %H:%M}."))
            else:
                if rep.date() == day1 and layover_ok_after and rep.time() < layover_ok_after:
                    findings.append(("violation", f"{rule_name}: a one-sector layover may only report after {layover_ok_after:%H:%M} the following day — {nd['label']} reports {rep:%d %b %H:%M}."))
                elif rep.date() == day1 and layover_strict and rep.time() < dtime(23, 0):
                    findings.append(("note", f"{rule_name}: {nd['label']} reports {rep:%d %b %H:%M} the day after {du['label']} — guideline restricts assignments before 23:00 (check with crew control)."))

    def require_days_off(arr, n_days, why):
        for k in range(1, n_days + 1):
            d = arr.date() + timedelta(days=k)
            for e in duty_day_map.get(d, []):
                findings.append(("violation", f"{why}: {arr.date():%d %b} arrival entitles arrival day + {n_days} day(s) off — but {e['label']} is rostered on {d:%d %b}."))

    for du in duties:
        arr = du["chocks_on"]
        nxt = [e for e in events if e["dt"] > arr]

        # R1 — 17h30 minimum rest at base
        if du["dest"] == "CMB" and nxt:
            rest = (nxt[0]["dt"] - arr).total_seconds() / 3600
            if rest < MIN_BASE_REST_H:
                findings.append(("violation", f"Min base rest: only {rest:.1f}h between {du['label']} chocks-on ({arr:%d %b %H:%M}) and next report ({nxt[0]['dt']:%d %b %H:%M}, {nxt[0]['label']}) — minimum is 17h30m."))

        if du["is_turnaround"]:
            hit_sa = set(du["stations"]) & SOUTHASIA_TA
            # R3 — 24h rest after DEL/BOM/KHI arriving 00:01–11:59
            if hit_sa and dtime(0, 1) <= arr.time() <= dtime(11, 59) and nxt:
                rest = (nxt[0]["dt"] - arr).total_seconds() / 3600
                if rest < 24:
                    findings.append(("violation", f"24h rest rule: {du['label']} ({'/'.join(hit_sa)}) arrived {arr:%d %b %H:%M} (00:01–11:59 window) — needs 24h to next report, got {rest:.1f}h ({nxt[0]['label']} at {nxt[0]['dt']:%d %b %H:%M})."))
            # R2 — DEL/BOM/KHI arriving 12:00–23:59 → next-day restrictions
            if hit_sa and arr.time() >= dtime(12, 0):
                check_next_day_restrictions(du, arr, "DEL/BOM/KHI rule", layover_strict=True)
            # R5 — DXB/AUH/MCT turnarounds
            if set(du["stations"]) & MIDEAST_TA:
                check_next_day_restrictions(du, arr, "DXB/AUH/MCT rule", layover_ok_after=dtime(16, 0))
            # UL231/232 special: next day only SBY2 / duty within 06:00–18:00
            if du["numbers"] & {"UL231", "UL232"}:
                d1 = arr.date() + timedelta(days=1)
                for e in duty_day_map.get(d1, []):
                    if not (dtime(6, 0) <= e["dt"].time() <= dtime(18, 0)):
                        findings.append(("violation", f"UL231/232 rule: following-day duty must fall within 06:00–18:00 (SBY2 window) — {e['label']} reports {e['dt']:%d %b %H:%M}."))
            # R8 — DOH/BAH/DMM turnaround: arrival day + 1 day off
            hit_1off = set(du["stations"]) & ONE_OFF_TURNAROUND
            if hit_1off:
                require_days_off(arr, 1, f"{'/'.join(hit_1off)} turnaround")
            # JED turnaround: arrival day + 2 days off
            hit_jed = set(du["stations"]) & TWO_OFF_TURNAROUND
            if hit_jed:
                require_days_off(arr, 2, "JED turnaround")
            # R9 — SIN/KUL/CGK morning turnaround → next-day flights after 18:00 only
            hit_sea = set(du["stations"]) & SEASIA_MORNING_TA
            if hit_sea and du["report"].time() <= dtime(12, 0):
                d1 = arr.date() + timedelta(days=1)
                for e in duty_day_map.get(d1, []):
                    if e["kind"] == "FLIGHT" and e["dt"].time() < dtime(18, 0):
                        findings.append(("violation", f"{'/'.join(hit_sea)} morning turnaround rule: next-day flights may only report after 18:00 — {e['label']} reports {e['dt']:%d %b %H:%M}."))
        # R4 — four-sector days arriving before 17:30
        if du["n"] >= 4 and arr.time() <= dtime(17, 30):
            check_next_day_restrictions(du, arr, "Four-sector day rule", layover_ok_after=dtime(18, 0))

    # R7 — long-haul layover stations: arrival day + 2 days off after return
    for i, du in enumerate(duties):
        hit = set(du["stations"]) & LONGHAUL_2OFF_LAYOVER
        if hit and du["dest"] == "CMB":
            # this duty RETURNS from a long-haul station (e.g. SYD ➔ CMB)
            origins = {s["o"] for s in du["sectors"]}
            if origins & LONGHAUL_2OFF_LAYOVER:
                require_days_off(du["chocks_on"], 2, f"{'/'.join(origins & LONGHAUL_2OFF_LAYOVER)} layover")
        elif du["dest"] == "CMB":
            origins = {s["o"] for s in du["sectors"] if s["o"]}
            lh = origins & LONGHAUL_2OFF_LAYOVER
            if lh:
                require_days_off(du["chocks_on"], 2, f"{'/'.join(lh)} layover")

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
        if r["Type"] == "FLIGHT" and r["Departure"] != "-" and r["Arrival"] != "-":
            m = _mins_between(r["Departure"], r["Arrival"])
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

def build_calendar_html(rows, span=None):
    valid = [r for r in rows if r["DateObj"] is not None]
    if not valid:
        return "<div style='color:#7e8ba0;font-size:14px;'>No dated duties parsed.</div>"
    # map each activity onto EVERY day it covers (multi-day HTL / SB / OFF)
    rmap = {}
    for r in rows:
        if not r["DateObj"]:
            continue
        d0 = r["DateObj"].date()
        d1 = r["EndDateObj"].date() if r.get("EndDateObj") else d0
        d = d0
        while d <= d1:
            rmap.setdefault(d, []).append(r)
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
        chips = []
        for act in rmap.get(d, []):
            if act["Type"] == "FLIGHT":
                dep = act["Departure"] if act["Departure"] != "-" else ""
                chips.append(f"<div class='chip chip-flt'>✈ <b>{act['Flight / Code']}</b><br><span>{act['Route']} {dep}</span></div>")
            elif act["Type"] == "LAYOVER":
                stn = act["Route"] if act["Route"] != "-" else "Layover"
                chips.append(f"<div class='chip chip-lay'>🏨 {stn}</div>")
            elif act["Type"] == "DAY OFF":
                chips.append("<div class='chip chip-off'>🟢 OFF</div>")
            elif act["Type"] == "STANDBY":
                chips.append("<div class='chip chip-sby'>⏱ SB</div>")
        if in_period and (rmin <= d <= rmax) and not chips:
            chips.append("<div class='chip chip-none'>No duty</div>")
        cells.append(f"<div class='{cls}'><div class='cal-date'>{d.day} {d.strftime('%b') if d.day == 1 or d == start else ''}</div>{''.join(chips)}</div>")
        d += timedelta(days=1)
    cells.append("</div>")
    return "".join(cells)

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
    .chip-none { background:transparent; color:#3b4a5e; }
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
            f"<div class='muted'>{analytics['redeyes']} red-eye dep · {analytics['max_streak']} consecutive duty days</div></div>",
            unsafe_allow_html=True)
        rows_html = "".join(f"<div class='bidrow'><span>{s} × {n} night(s)</span><span>${t}</span></div>"
                            for s, n, r_, t in analytics['allowance_rows']) or "<div class='muted'>No layovers parsed.</div>"
        st.markdown(
            f"<div class='card'><h5>Estimated Allowances</h5>"
            f"<div style='font-size:26px;font-weight:800;color:#4caf50;'>${analytics['allowance_total']:,} USD</div>"
            f"<div class='muted' style='margin-bottom:6px;'>Total calculated per diem</div>{rows_html}</div>",
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
            wx_html = (f"{wx['icon']} {wx['temp']}°C · {wx['desc']}" if wx and wx["temp"] is not None else "n/a")
            lt_html = f"{wx['local_time']} ({wx['gmt']})" if wx else "-"
            gt_html = f"{lv['ground_hrs']} hrs" if lv["ground_hrs"] else "-"
            spots_html = "".join(f"<span class='spot'>{s}</span>" for s in spots)
            st.markdown(
                f"<div class='card' style='border-color:#00bcd4;'>"
                f"<h5>🏨 Layover Intel: {city} ({lv['station']})" + (f" — {lv['date'].strftime('%d %b')}" if lv['date'] else "") + "</h5>"
                f"<div style='display:flex;gap:28px;font-size:13px;margin-bottom:10px;'>"
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
- **DEL / BOM / KHI turnarounds** arriving 12:00–23:59 → next day: no turnaround before 23:00 report; 23:00–05:59 regional (<4h sector) only; anything after 06:00 on day 2.
- **DEL / BOM / KHI turnarounds** arriving 00:01–11:59 → **24h rest** chocks-on to next report.
- **Four-sector days** (arrival before 17:30) → next day: 1-sector layover after 18:00 only; same turnaround limits as above; SBY4 insertable.
- **DXB / AUH / MCT turnarounds** → next day: 1-sector layover after 16:00 only; same turnaround limits; SBY4 insertable.
- **UL231/232 (DXB)** → next day only SBY2 (06:00–18:00) or a flight within that window.
- **LHR / FRA / CDG / FCO / MXP / NRT / SYD / MEL layovers & JED turnaround** → arrival day + **2 days off**.
- **DOH / BAH / DMM turnarounds** → arrival day + **1 day off**.
- **SIN / KUL / CGK morning turnarounds** → next-day flights report after 18:00, or SBY4.
- **Standby insertion if your flight cancels:** morning T/A (report after 06:00) → SBY2 · morning T/A (report before 06:00) → SBY1 · Middle-East flight reporting before 18:00 → SBY3 · midnight flight reporting before midnight → SBY4 · midnight flight reporting after midnight → SBY1.
- Duty leave counts as a duty day.
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
                    flight_check_results.append({"flight": flight["flight_no"], "route": flight["route"],
                                                 "date": flight["date_obj"].strftime("%d %b %Y"),
                                                 "delayed": telemetry["is_delayed"],
                                                 "severity": telemetry.get("severity", "ok"),
                                                 "status": telemetry["status_message"],
                                                 "inbound_note": telemetry.get("inbound_note"),
                                                 "inbound_risk": telemetry.get("inbound_risk", False),
                                                 "rest_note": rest_note})
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
