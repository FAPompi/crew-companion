import streamlit as st
import sqlite3
import hashlib
import re
import json
import requests
import pandas as pd
try:
    import plotly.graph_objects as go
except Exception:            # test harnesses may stub plotly; analytics falls back gracefully
    go = None
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
    c.execute('''
        CREATE TABLE IF NOT EXISTS salary_history (
            username TEXT,
            month TEXT,
            roster_text TEXT,
            saved_at TEXT,
            PRIMARY KEY (username, month)
        )
    ''')
    # Migration: 'excluded' flag — 1 when the user removed an auto-pulled month
    # from the salary tab (kept as a marker so the month isn't silently re-pulled).
    try:
        c.execute("ALTER TABLE salary_history ADD COLUMN excluded INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass   # column already present
    c.execute('''
        CREATE TABLE IF NOT EXISTS roster_history (
            username TEXT,
            period_start TEXT,
            published_text TEXT,
            performed_text TEXT,
            finalized INTEGER DEFAULT 0,
            saved_at TEXT,
            PRIMARY KEY (username, period_start)
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


# --- salary history (per-calendar-month performed rosters, manual or pulled) ---

def _month_key(ym):
    """(year, month) -> 'YYYY-MM'."""
    return f"{ym[0]:04d}-{ym[1]:02d}"


def _month_of_roster(valid_dates):
    """Calendar (year, month) containing the MEDIAN date of a pasted roster —
    the month the paste belongs to, robust to a day or two of adjacent-month
    rows at either end."""
    if not valid_dates:
        return None
    s = sorted(valid_dates)
    d = s[len(s) // 2]
    return (d.year, d.month)


def _prev_months(ym, n):
    """The n most recent (year, month) pairs ending at ym (inclusive), oldest last."""
    y, m = ym
    out = []
    for _ in range(n):
        out.append((y, m))
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    return out


def _months_from_2026(upto_ym):
    """Every (year, month) from January 2026 through upto_ym, oldest first."""
    y, m = upto_ym
    out = []
    cy, cm = 2026, 1
    while (cy, cm) <= (y, m):
        out.append((cy, cm))
        cm += 1
        if cm == 13:
            cm, cy = 1, cy + 1
    return out


def save_salary_history(username, month_key, text):
    conn = sqlite3.connect('crew_companion.db')
    c = conn.cursor()
    c.execute('''INSERT INTO salary_history (username, month, roster_text, saved_at, excluded)
                 VALUES (?, ?, ?, ?, 0)
                 ON CONFLICT(username, month) DO UPDATE SET
                   roster_text = excluded.roster_text, saved_at = excluded.saved_at,
                   excluded = 0''',
              (username, month_key, text, datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
    conn.commit()
    conn.close()


def exclude_salary_month(username, month_key):
    """Stop auto-pulling this month from finalized Roster History (a manual
    entry, if any, is kept). Stored as a marker row with empty text."""
    conn = sqlite3.connect('crew_companion.db')
    c = conn.cursor()
    c.execute('''INSERT INTO salary_history (username, month, roster_text, saved_at, excluded)
                 VALUES (?, ?, '', ?, 1)
                 ON CONFLICT(username, month) DO UPDATE SET excluded = 1''',
              (username, month_key, datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
    conn.commit()
    conn.close()


def restore_salary_month(username, month_key):
    """Undo an exclusion: drop the marker row (or clear the flag if a manual
    entry exists under the same month)."""
    conn = sqlite3.connect('crew_companion.db')
    c = conn.cursor()
    c.execute('''SELECT roster_text FROM salary_history WHERE username = ? AND month = ?''',
              (username, month_key))
    row = c.fetchone()
    if row is None:
        conn.close()
        return
    if (row[0] or '').strip():
        c.execute('''UPDATE salary_history SET excluded = 0 WHERE username = ? AND month = ?''',
                  (username, month_key))
    else:
        c.execute('''DELETE FROM salary_history WHERE username = ? AND month = ?''',
                  (username, month_key))
    conn.commit()
    conn.close()


def delete_salary_history(username, month_key):
    conn = sqlite3.connect('crew_companion.db')
    c = conn.cursor()
    c.execute('DELETE FROM salary_history WHERE username = ? AND month = ?', (username, month_key))
    conn.commit()
    conn.close()


def load_salary_history(username):
    """{month_key: {'text': ..., 'saved_at': ..., 'excluded': bool}} for the user."""
    conn = sqlite3.connect('crew_companion.db')
    c = conn.cursor()
    c.execute('SELECT month, roster_text, saved_at, excluded FROM salary_history WHERE username = ?', (username,))
    out = {mk: {"text": txt or "", "saved_at": sa, "excluded": bool(ex)}
           for mk, txt, sa, ex in c.fetchall()}
    conn.close()
    return out


def _clip_rows_to_month(rows, ym):
    """Rows whose DateObj falls within the calendar month (year, month)."""
    y, m = ym
    lo = datetime(y, m, 1)
    hi = datetime(y, m + 1, 1) if m < 12 else datetime(y + 1, 1, 1)
    return [r for r in rows if r.get("DateObj") and lo <= r["DateObj"] < hi]


def _full_month_days(y, m):
    """Every date in the calendar month (year, month)."""
    lo = datetime(y, m, 1).date()
    hi = (datetime(y, m + 1, 1) if m < 12 else datetime(y + 1, 1, 1)).date()
    return {lo + timedelta(days=i) for i in range((hi - lo).days)}


def _finalized_performed_rows(username, current_rows):
    """Finalized performed rows from PAST roster_history periods, clipped to
    their 28-day periods and date-sorted. The live/current period is excluded —
    it stays on the published roster until it ends, so it must never feed a
    salary month."""
    cur_start = None
    if current_rows:
        dates = [r["DateObj"].date() for r in current_rows if r.get("DateObj")]
        if dates:
            cur_start = roster_period_of_roster(dates)
    out = []
    for h in load_roster_history(username):
        if not h["finalized"] or not h["performed_text"] or h["period_start"] is None:
            continue
        if cur_start is not None and h["period_start"] == cur_start:
            continue   # current period stays live until finalized
        out += _rows_in_period(parse_roster_text(h["performed_text"]), h["period_start"])
    out.sort(key=lambda r: r["DateObj"] or datetime.min)
    return out


def _month_full_performed_coverage(username, current_rows, y, m):
    """True when finalized roster periods collectively cover every day of the
    calendar month by their 28-day spans (an off day with no row still counts
    as covered by the period it sits inside). The live/current period is
    excluded, so a month that would need the still-live roster never counts."""
    cur_start = None
    if current_rows:
        dates = [r["DateObj"].date() for r in current_rows if r.get("DateObj")]
        if dates:
            cur_start = roster_period_of_roster(dates)
    lo = datetime(y, m, 1).date()
    hi = (datetime(y, m + 1, 1) if m < 12 else datetime(y + 1, 1, 1)).date()
    spans = []
    for h in load_roster_history(username):
        if not h["finalized"] or not h["performed_text"] or h["period_start"] is None:
            continue
        if cur_start is not None and h["period_start"] == cur_start:
            continue   # current period stays live until finalized
        spans.append((h["period_start"], h["period_start"] + timedelta(days=ROSTER_PERIOD_DAYS - 1)))
    if not spans:
        return False
    day = lo
    while day < hi:
        if not any(s <= day <= e for s, e in spans):
            return False
        day += timedelta(days=1)
    return True


def salary_month_rows(username, ym, current_rows):
    """Rows to compute salary for a calendar month — ONLY when the month has
    ENDED and a FULL performed month (1st through the last day) is available.
    By precedence:
    1) a manual salary_history entry for that exact month (trusted — pinned
       per-month by the user as a whole month);
    2) finalized roster_history performed rows, but only if the finalized
       periods fully cover the calendar month (no merge with the live/current
       roster — a half-month or in-progress month is excluded);
    3) the salary tab's performed-roster slot, only if it covers the full
       month day-for-day.
    An in-progress (or future) month returns ([], "ongoing") so the UI shows
    "still in progress" instead of silently computing from a partial roster.
    Returns (rows, source) where source ∈ {manual, history, slot, none, ongoing}."""
    y, m = ym
    now = datetime.now()
    if (y, m) >= (now.year, now.month):
        return [], "ongoing"       # month still in progress — no full performed month exists yet
    key = _month_key(ym)
    sh = load_salary_history(username)
    if key in sh and sh[key]["text"].strip():
        return _clip_rows_to_month(parse_roster_text(sh[key]["text"]), ym), "manual"
    if not sh.get(key, {}).get("excluded") and _month_full_performed_coverage(username, current_rows, y, m):
        hist = _clip_rows_to_month(_finalized_performed_rows(username, current_rows), ym)
        if any(r["Type"] == "FLIGHT" for r in hist):
            return hist, "history"
    need = _full_month_days(y, m)
    slot = _clip_rows_to_month(parse_roster_text(load_performed_roster(username)), ym)
    if need <= {r["DateObj"].date() for r in slot if r.get("DateObj")} \
            and any(r["Type"] == "FLIGHT" for r in slot):
        return slot, "slot"
    return [], "none"


def salary_available_months(username, current_rows):
    """Months the salary calculator can actually compute: manual saves plus any
    calendar month fully covered by finalized performed rosters or a full-month
    performed-slot paste. Partial months are NOT offered."""
    months = set()
    for key, entry in load_salary_history(username).items():
        if not entry.get("excluded") and entry.get("text", "").strip():
            try:
                months.add((int(key[:4]), int(key[5:7])))
            except ValueError:
                pass
    y, m = datetime.now().year, datetime.now().month
    for ym in _months_from_2026((y, m)):
        rows, _ = salary_month_rows(username, ym, current_rows)
        if any(r["Type"] == "FLIGHT" for r in rows):
            months.add(ym)
    return sorted(months)


def _dt_fmt(dt_):
    """Portal-style datetime stamp: 'DDMMMYY HH:MM'."""
    if not isinstance(dt_, datetime):
        return ""
    return dt_.strftime("%d%b%y").upper() + " " + dt_.strftime("%H:%M")


def _rows_to_roster_text(rows):
    """Rebuild a tab-separated roster text from parsed rows (Activity | Checkin |
    Start | Dep | Arr | End | Checkout | AcType), so an auto-pulled month can be
    loaded into the paste box for editing. The parser is content-based, so this
    round-trips cleanly (check-in cells stay on the first sector of each duty)."""
    lines = ["Checkin\tActivity\tStart\tDep\tArr\tEnd\tCheckout\tAcType"]
    for r in rows:
        act = (r.get("Code") or r.get("Flight / Code") or r["Type"]).replace(" ", "")
        if r["Type"] == "FLIGHT":
            o, d = _route_od(r.get("Route"))
            cells = [
                _dt_fmt(r.get("CIdt")), act, _dt_fmt(r.get("DEPdt")),
                o or "CMB", d or "CMB", _dt_fmt(r.get("ARRdt")), _dt_fmt(r.get("COdt")),
                r.get("Aircraft") or "-",
            ]
        elif r["Type"] == "LAYOVER":
            stn = (r.get("Route") or "CMB").strip()
            cells = ["", act, _dt_fmt(r.get("DEPdt")), stn, stn, _dt_fmt(r.get("ARRdt")), "", "-"]
        elif r["Type"] in ("STANDBY", "DUTY"):
            cells = [
                _dt_fmt(r.get("CIdt")), act, _dt_fmt(r.get("DEPdt")),
                "CMB", "CMB", _dt_fmt(r.get("ARRdt")), _dt_fmt(r.get("COdt")), "-",
            ]
        else:  # DAY OFF / LEAVE / TIMEOFF
            cells = ["", act, _dt_fmt(r.get("DEPdt")), "CMB", "CMB", _dt_fmt(r.get("ARRdt")), "", "-"]
        lines.append("\t".join(cells))
    return "\n".join(lines)


def pulled_salary_months(username, current_rows):
    """Months the salary calculator currently AUTO-PULLS from finalized Roster
    History (source 'history' — no manual pin). Returns [(ym, rows), ...]."""
    out = []
    y, m = datetime.now().year, datetime.now().month
    for ym in _months_from_2026((y, m)):
        rows, src = salary_month_rows(username, ym, current_rows)
        if src == "history":
            out.append((ym, rows))
    return out


# --- roster periods ---
# 28-day roster periods. User-confirmed starts (2026-09-08):
#   13 Jul 2026, 10 Aug 2026, 07 Sep 2026 (current), 05 Oct 2026, ...
# A label like "070926-041026" runs 07 Sep – 04 Oct inclusive (28 days), and the
# NEXT period starts the following day (05 Oct).
ROSTER_ANCHOR = datetime(2026, 7, 13).date()
ROSTER_PERIOD_DAYS = 28


# --- roster history (period-based archive: published plan + performed reality) ---

def save_roster_history(username, period_start, published_text=None, performed_text=None, finalized=None):
    """UPSERT one 28-day period into the history archive. period_start is a
    datetime.date (stored as 'YYYY-MM-DD')."""
    conn = sqlite3.connect('crew_companion.db')
    c = conn.cursor()
    pkey = period_start.strftime('%Y-%m-%d') if isinstance(period_start, (datetime,)) else str(period_start)
    c.execute('''INSERT INTO roster_history (username, period_start, published_text, performed_text, finalized, saved_at)
                 VALUES (?, ?, ?, ?, ?, ?)
                 ON CONFLICT(username, period_start) DO UPDATE SET
                   published_text = COALESCE(excluded.published_text, roster_history.published_text),
                   performed_text = COALESCE(excluded.performed_text, roster_history.performed_text),
                   finalized = COALESCE(excluded.finalized, roster_history.finalized),
                   saved_at = excluded.saved_at''',
              (username, pkey, published_text, performed_text,
               (1 if finalized else 0) if finalized is not None else None,
               datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
    conn.commit()
    conn.close()


def load_roster_history(username):
    """All archived periods for a user, oldest first. Each row: period_start
    (datetime.date), published_text, performed_text, finalized (bool), saved_at."""
    conn = sqlite3.connect('crew_companion.db')
    c = conn.cursor()
    c.execute('''SELECT period_start, published_text, performed_text, finalized, saved_at
                 FROM roster_history WHERE username = ? ORDER BY period_start''', (username,))
    out = []
    for ps, pub, perf, fin, saved in c.fetchall():
        try:
            d = datetime.strptime(ps, '%Y-%m-%d').date()
        except ValueError:
            d = None
        out.append({"period_start": d, "published_text": pub or "",
                    "performed_text": perf or "", "finalized": bool(fin),
                    "saved_at": saved})
    conn.close()
    return out


def delete_roster_history(username, period_start):
    """Remove one archived 28-day period (its published/performed text goes too).
    Used to clean up a stray finalized period (e.g. an accidental paste)."""
    pkey = period_start.strftime('%Y-%m-%d') if hasattr(period_start, 'strftime') else str(period_start)
    conn = sqlite3.connect('crew_companion.db')
    c = conn.cursor()
    c.execute('DELETE FROM roster_history WHERE username = ? AND period_start = ?', (username, pkey))
    conn.commit()
    conn.close()


def purge_pre2026_history():
    """Startup cleanup: the app only supports 2026-onwards rosters and salary
    months, so any roster period or salary month saved from an older test
    roster is removed automatically. Idempotent — runs on every launch, so a
    stray pre-2026 paste can never persist across an app update/reload."""
    conn = sqlite3.connect('crew_companion.db')
    c = conn.cursor()
    c.execute("DELETE FROM roster_history WHERE period_start < '2026-01-01' OR period_start = ''")
    c.execute("DELETE FROM salary_history WHERE month < '2026-01'")
    conn.commit()
    conn.close()


def _rows_in_period(rows, start, days=ROSTER_PERIOD_DAYS):
    """Rows whose DateObj falls within [start, start + days)."""
    lo, hi = start, start + timedelta(days=days)
    return [r for r in rows if r.get("DateObj") and lo <= r["DateObj"].date() < hi]


def roster_periods_off_days(username, current_rows):
    """Off-day count per 28-day period, from finalized history (performed) plus
    the current roster (published). The current period uses its finalized
    performed text if one exists, else the live published text. Returns a list
    of {start, off, src} sorted by period start."""
    hist = load_roster_history(username)
    cur_start = None
    if current_rows:
        dates = [r["DateObj"].date() for r in current_rows if r.get("DateObj")]
        if dates:
            cur_start = roster_period_of_roster(dates)
    out, included = [], set()
    for h in hist:
        if not h["finalized"] or not h["performed_text"] or h["period_start"] is None:
            continue
        if cur_start is not None and h["period_start"] == cur_start:
            continue   # current period stays as the live published roster
        rows = parse_roster_text(h["performed_text"])
        clipped = _rows_in_period(rows, h["period_start"])
        ds = _day_status_map(clipped)
        if ds:
            out.append({"start": h["period_start"],
                        "off": sum(1 for d, s in ds.items() if "off" in s), "src": "performed"})
            included.add(h["period_start"])
    if cur_start and cur_start not in included and current_rows:
        ds = _day_status_map(_rows_in_period(current_rows, cur_start))
        if ds:
            out.append({"start": cur_start,
                        "off": sum(1 for d, s in ds.items() if "off" in s), "src": "live"})
    out.sort(key=lambda x: x["start"])
    return out


def days_off_average_8_2_17_d(username, current_rows, upto=None):
    """8.2.17(d): off days over the last 3 finalized 4-week periods must reach
    24 (8 per period), and every period must carry its own minimum of 7. When
    `upto` (a period start date) is given, only periods up to and including it
    are considered — used when browsing a past period so the rolling check
    reflects history as of that period. Returns {n_periods, avg, ok, per,
    total} — avg/ok/total are None unless ≥ 3 periods; ok = total ≥ 24."""
    per = roster_periods_off_days(username, current_rows)
    if upto is not None:
        per = [p for p in per if p["start"] <= upto]
    if len(per) < 3:
        return {"n_periods": len(per), "avg": None, "ok": None, "per": per, "total": None}
    last3 = per[-3:]
    total = sum(p["off"] for p in last3)
    avg = round(total / 3.0, 1)
    return {"n_periods": len(per), "avg": avg, "ok": total >= 24, "per": per, "total": total}


def merged_history_rows(username, current_rows):
    """Finalized performed rows (clipped to their periods) + the current roster
    rows, merged and date-sorted — for cross-period rolling checks."""
    hist = load_roster_history(username)
    out = []
    included = set()
    cur_start = None
    if current_rows:
        dates = [r["DateObj"].date() for r in current_rows if r.get("DateObj")]
        if dates:
            cur_start = roster_period_of_roster(dates)
    for h in hist:
        if not h["finalized"] or not h["performed_text"] or h["period_start"] is None:
            continue
        if cur_start is not None and h["period_start"] == cur_start:
            continue   # current period stays as the live published roster
        out += _rows_in_period(parse_roster_text(h["performed_text"]), h["period_start"])
        included.add(h["period_start"])
    if current_rows and cur_start and cur_start not in included:
        out += _rows_in_period(current_rows, cur_start)
    out.sort(key=lambda r: r["DateObj"] or datetime.min)
    return out


def calendar_rows(username, current_rows):
    """Rows for the CALENDAR view: the current published roster as-is (unclipped,
    so an adjacent-period tail day still shows) plus each FINALIZED past period's
    performed rows (clipped to its period). The current period always comes from
    the live roster, never from a finalized duplicate. Monitoring & Intel stay
    on the current roster only — this is purely for calendar display."""
    out = []
    cur_start = None
    if current_rows:
        dates = [r["DateObj"].date() for r in current_rows if r.get("DateObj")]
        if dates:
            cur_start = roster_period_of_roster(dates)
    hist = load_roster_history(username)
    for h in hist:
        if not h["finalized"] or not h["performed_text"] or h["period_start"] is None:
            continue
        if cur_start is not None and h["period_start"] == cur_start:
            continue   # current period is shown live from the published roster
        out += _rows_in_period(parse_roster_text(h["performed_text"]), h["period_start"])
    out += list(current_rows)
    out.sort(key=lambda r: r["DateObj"] or datetime.min)
    # The live published roster can carry a day or two of the PREVIOUS period
    # (its tail), which a finalized history period also provides — collapse
    # identical (date, type, code) rows so those days aren't double-chipped.
    seen, dedup = set(), []
    for r in out:
        key = (r["DateObj"].date() if r.get("DateObj") else None, r["Type"], r.get("Code"))
        if key in seen:
            continue
        seen.add(key)
        dedup.append(r)
    return dedup


def period_rows_for_display(username, current_rows, period_start):
    """Rows for a SPECIFIC period, for the analytics/fatigue/FDP panels when the
    calendar is browsing that period. The current period returns the live
    published rows; past periods return their finalized performed rows (clipped
    to the period). Empty list if the period has no finalized roster."""
    if current_rows:
        dates = [r["DateObj"].date() for r in current_rows if r.get("DateObj")]
        cur_start = roster_period_of_roster(dates) if dates else None
        if cur_start == period_start:
            return _rows_in_period(current_rows, period_start)
    for h in load_roster_history(username):
        if h["period_start"] == period_start and h["finalized"] and h["performed_text"]:
            return _rows_in_period(parse_roster_text(h["performed_text"]), period_start)
    return []


def surrounding_rows(username, current_rows, period_start):
    """All finalized-history + current-roster rows that fall OUTSIDE the given
    28-day period — context for cross-period checks (e.g. the duty that ends
    before a day-off block at the very start of the period)."""
    lo, hi = period_start, period_start + timedelta(days=ROSTER_PERIOD_DAYS)
    out = []
    for r in merged_history_rows(username, current_rows):
        d = r["DateObj"].date() if r.get("DateObj") else None
        if d is not None and not (lo <= d < hi):
            out.append(r)
    return out


def _nav_go(delta):
    """‹ › ⟲ calendar-period navigation. Runs as a button `on_click` callback —
    i.e. BEFORE the script body of the rerun — so it can mutate the
    'cal_period_sel' key directly (the selectbox hasn't been instantiated yet in
    that run). `delta` is -1 (older), +1 (newer) or 'cur' (jump to the current
    period). Reads the period grid stashed by the Dashboard tab."""
    starts = st.session_state.get('_period_starts') or []
    t0 = st.session_state.get('_t0')
    cur = st.session_state.get('cal_period_sel')
    if not starts:
        return
    if delta == 'cur':
        if t0 in starts:
            st.session_state['cal_period_sel'] = t0
        return
    idx = starts.index(cur) if cur in starts else None
    if idx is None:
        st.session_state['cal_period_sel'] = (t0 if t0 in starts else starts[0])
        return
    j = idx + delta
    if 0 <= j < len(starts):
        st.session_state['cal_period_sel'] = starts[j]


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


def roster_period_bounds(d):
    """Return (start, end_exclusive) of the 28-day roster period containing date d.
    end_exclusive = start + 28 = the NEXT period's start. The period's LAST day is
    end_exclusive - 1 (e.g. 07 Sep – 04 Oct 2026; next starts 05 Oct)."""
    k = (d - ROSTER_ANCHOR).days // ROSTER_PERIOD_DAYS
    start = ROSTER_ANCHOR + timedelta(days=ROSTER_PERIOD_DAYS * k)
    return start, start + timedelta(days=ROSTER_PERIOD_DAYS)


def roster_period_of_roster(valid_dates):
    """The 28-day period containing the MEDIAN date of a roster — robust to a
    day or two of the adjacent period's rows at either end of the paste."""
    if not valid_dates:
        return None
    s = sorted(valid_dates)
    return roster_period_bounds(s[len(s) // 2])[0]

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
    _code_alt = "|".join(re.escape(c) for c in _GROUND_CODES_SORTED)
    t = re.sub(r'(\d{2}[A-Z]{3}\d{2}[ \t]*\d{2}:\d{2})[ \t]*(?=(?<![A-Z0-9])UL\s*\d|SB\d|' + _code_alt + r')', r'\n\1', t)
    return t

# --- Ground / activity codes (from the user's "Ground Code" reference) ---
# Every code the portal can emit is recognised so nothing is silently dropped.
# bucket: 'off' = legal/request day off (the ONLY codes that count as a day off
# for 8.2.17); 'layover'; 'standby'; 'sick' (leave bucket); 'leave'; 'neutral'
# (neither duty nor a day off); 'tof' (time-off window); 'duty' (training /
# office / meeting / positioning / standby call-out — counts as a duty day).
# 'UL…' flights are handled separately by the parsers.
GROUND_CODES = {
    # day off
    "OFF": ("off", "Legal off day"), "ROF": ("off", "Request off"),
    "HOT": ("off", "Outstation off"), "OVO": ("off", "Overseas off day"),
    # layover / time off
    "HTL": ("layover", "Hotel"),
    "TOF": ("tof", "Time off"), "HTO": ("tof", "Time off after FAU meeting"),
    # standby
    "SB1": ("standby", "Standby 1"), "SB2": ("standby", "Standby 2"),
    "SB3": ("standby", "Standby 3"), "SB4": ("standby", "Standby 4"),
    "SSY": ("standby", "Standby for SNY"), "LSB": ("standby", "London standby"),
    "ASB": ("standby", "Airport standby"),
    # sick — company counts sick towards off days (see _day_status_map)
    "S/L": ("sick", "Illness"), "FSL": ("sick", "Flexible sick"),
    "LMS": ("sick", "Last minute sick"), "SAR": ("sick", "Sick after ROFF"),
    "SBR": ("sick", "Sick before ROFF"), "SAC": ("sick", "Sick after casual leave"),
    "SBC": ("sick", "Sick before casual leave"), "SAA": ("sick", "Sick after annual leave"),
    "SBA": ("sick", "Sick before annual leave"), "SAS": ("sick", "Sick after standby"),
    "SCM": ("sick", "Sick combined w/ leave"), "SOD": ("sick", "Sick on duty swap"),
    "SSC": ("sick", "Sick set off from CLV"), "SSA": ("sick", "Sick set off from ALV"),
    "S/R": ("sick", "Sick leave request"),
    "EML": ("sick", "Emergency leave"), "ACL": ("sick", "Accident leave"),
    # leave
    "ALV": ("leave", "Annual leave"), "RLV": ("leave", "Annual leave (after publish)"),
    "ALP": ("leave", "Annual leave (planned)"), "CLV": ("leave", "Casual leave"),
    "SPL": ("leave", "Special leave"),
    "MTL": ("leave", "Maternity leave"), "MTP": ("leave", "Maternity leave (paid)"),
    "MTO": ("leave", "Maternity leave (office)"), "MTN": ("leave", "Maternity leave (no pay)"),
    # neutral (not duty, not a day off)
    "C/R": ("neutral", "Casual leave request"), "LWP": ("neutral", "No-pay leave"),
    "NAN": ("neutral", "Non-authorized no-pay"), "A/N": ("neutral", "Authorized no-pay leave"),
    "AWL": ("neutral", "Absent without leave"), "GRD": ("neutral", "Grounding (discipline)"),
    "GRW": ("neutral", "Grounding (weight)"), "GRC": ("neutral", "Grounding (cosmetic)"),
    "OTR": ("neutral", "Off the roster"), "CHO": ("neutral", "Company holiday"),
    "AB1": ("neutral", "Block before ALV"), "AB2": ("neutral", "Block after ALV"),
    "CNL": ("neutral", "Cancelled"), "NTS/QRN": ("neutral", "Quarantine"),
    "QRC": ("neutral", "Quarantine (first contact)"), "QRW": ("neutral", "Waiting for PCR results"),
    "PCO": ("neutral", "PCR on arrival"), "PCR/PCL": ("neutral", "PCR at hospital"),
    "YFV": ("neutral", "Yellow fever vaccine"),
    # duty (training / office / meeting / positioning / standby call-out)
    "DLV": ("duty", "Company assignment"), "OFG": ("duty", "Office duty"),
    "OFH": ("duty", "Office duty (half day)"), "MTG": ("duty", "Meeting"),
    "GND": ("duty", "Ground duty"), "PRG": ("duty", "Ground duty (pregnancy)"),
    "ADM": ("duty", "Admin"), "ENQ": ("duty", "Enquiry"), "SDN": ("duty", "Step down"),
    "MED": ("duty", "Medical check"), "GTP": ("duty", "Ground transport"),
    "OAL": ("duty", "Positioning (other carrier)"), "SCO": ("duty", "Called on standby"),
    "DTL": ("duty", "Duty leave"), "FAU": ("duty", "FAU meeting"),
    "IMM": ("duty", "Inflight mgmt meeting"), "SEP": ("duty", "SEP training"),
    "SEC": ("duty", "Security refresher"), "CRM": ("duty", "CRM"),
    "DGR": ("duty", "DGR refresher"), "F/A": ("duty", "First aid training"),
    "TTT": ("duty", "Train the trainer"), "OBT": ("duty", "Outbound training"),
    "SPC": ("duty", "Announcement training"), "SVC": ("duty", "Service training"),
    "GNT": ("duty", "General training"), "RST": ("duty", "Re-sit"),
    "CBT": ("duty", "Computer based training"), "CDE": ("duty", "Ditching evacuation"),
    "CSE": ("duty", "Slide evacuation"), "CFD": ("duty", "Fire drill"),
    "FSE": ("duty", "Slide evacuation FD"), "FDE": ("duty", "Ditching evacuation FD"),
    "FFD": ("duty", "Fire drill FD"), "CSS": ("duty", "CRM, security & safety"),
    "PEF": ("duty", "Performance training"), "SOP": ("duty", "SOP / FMGS training"),
    "EXM": ("duty", "Crew exam"), "ONE": ("duty", "One World training"),
    "PAX": ("duty", "Pax interaction"), "CSW": ("duty", "CS workshop"),
    "INT": ("duty", "Crew interview"), "TD1": ("duty", "Training D1 (DGR/SEC)"),
    "TD2": ("duty", "Training D2 (safety/fd/slevac)"), "TD3": ("duty", "Training D2 (CRM/ditching)"),
    "IBC": ("duty", "Initial BC training"), "TMD": ("duty", "Team dynamics"),
    "EMI": ("duty", "Emotional intelligence"), "WNA": ("duty", "Wine appreciation"),
    "LSW": ("duty", "Leadership workshop"), "BCT": ("duty", "BC training"),
    "321": ("duty", "A321 training"), "MLS": ("duty", "Meal service training"),
    "SPH": ("duty", "Special pax handling"), "EVA": ("duty", "On-board eval"),
    "ETQ": ("duty", "Etiquette training"), "PER": ("duty", "Personality development"),
    "GRN": ("duty", "Graduation"), "PDM": ("duty", "Personality development"),
    "F&B": ("duty", "Food & bev training"), "SER": ("duty", "Service recurrent"),
    "DFS": ("duty", "Duty free training"), "CSR": ("duty", "CSR"),
    "ICT": ("duty", "In-charge crew training"), "CMW": ("duty", "Cabin manager workshop"),
    "CSP": ("duty", "Cabin supervisor promotion"), "CMP": ("duty", "Cabin manager workshop"),
    "CSC": ("duty", "Security"), "DFT": ("duty", "Training/duty"),
}

_GROUND_CODES_SORTED = sorted(GROUND_CODES, key=len, reverse=True)
_GROUND_CODE_PAT = "|".join(re.escape(c) for c in _GROUND_CODES_SORTED)

# Annual-leave codes (any type of annual leave) count towards off-day totals and
# suppress the mandatory-off-day check — the crew has already taken their rest
# inside the annual-leave block.
ANNUAL_LEAVE_CODES = {"ALV", "RLV", "ALP"}


def ground_code_bucket(code):
    info = GROUND_CODES.get(code)
    return info[0] if info else None


def ground_code_label(code):
    info = GROUND_CODES.get(code)
    return info[1] if info else (code or "")


def _detect_code(line_str):
    """Longest-first ground-code token match with alnum/slash boundaries."""
    for c in _GROUND_CODES_SORTED:
        if re.search(r'(?<![A-Z0-9/])' + re.escape(c) + r'(?![A-Z0-9/])', line_str):
            return c
    return None


def _clamp_exclusive_end(dep_dt, arr_dt):
    """The portal's PERFORMED view writes all-day markers with an EXCLUSIVE end:
    'OFF 03AUG26 00:00 → 04AUG26 00:00' means off on 03 Aug only (the boundary
    is the next day's midnight). Collapse that to the day before so a single day
    off isn't double-counted. Real-times (23:59, 15:10, …) are left untouched."""
    if (isinstance(dep_dt, datetime) and isinstance(arr_dt, datetime)
            and arr_dt > dep_dt and arr_dt.time() == dtime(0, 0)):
        return dep_dt, arr_dt - timedelta(days=1)
    return dep_dt, arr_dt


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
        if re.match(r'^(UL\s*\d{1,4}|SB\d*|' + _GROUND_CODE_PAT + r')$', s):
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
    else:
        bucket = ground_code_bucket(act)
        if bucket == "layover":
            atype, code = "LAYOVER", act
            station = iatas[0][1] if iatas else "-"
            dep_iata = arr_iata = station
            if dts:
                dep_dt, arr_dt = dts[0][1], dts[-1][1]
            ci_dt, co_dt = dep_dt, arr_dt
        elif bucket == "off":
            atype, code = "DAY OFF", act
            if dts:
                dep_dt, arr_dt = _clamp_exclusive_end(dts[0][1], dts[-1][1])
        elif bucket == "tof":
            # Time OFF — a protected time-off window (NOT a full day off). No duty
            # may check in or check out within the window (user rule).
            atype, code = "TIMEOFF", act
            if dts:
                dep_dt, arr_dt = dts[0][1], dts[-1][1]
        elif bucket == "standby" or re.match(r'^SB\d*$', act):
            atype, code = "STANDBY", act
            if len(dts) >= 4:
                ci_dt, dep_dt, arr_dt, co_dt = dts[0][1], dts[1][1], dts[2][1], dts[3][1]
            elif len(dts) == 3:
                ci_dt = dep_dt = dts[0][1]
                arr_dt, co_dt = dts[1][1], dts[2][1]
            elif dts:
                ci_dt = dep_dt = dts[0][1]
                arr_dt = co_dt = dts[-1][1]
        elif bucket in ("sick", "leave", "neutral"):
            atype, code = "LEAVE", act
            if dts:
                dep_dt, arr_dt = _clamp_exclusive_end(dts[0][1], dts[-1][1])
        elif bucket == "duty":
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

    # aircraft type: a 3-char alnum token (e.g. 320, 32B, 333) that isn't an IATA code
    ac_type = "-"
    for f in fields[act_idx + 1:]:
        s = f.strip()
        if re.match(r'^[A-Z0-9]{3}$', s) and not re.match(r'^[A-Z]{3}$', s):
            ac_type = s
            break

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
        "Aircraft": ac_type,
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

        is_flight = re.search(r'(?<![A-Z0-9])UL\s*\d{1,4}', line_str) is not None
        # Flight detection takes priority so a route IATA code that collides
        # with a ground code (e.g. PER = Perth vs "Personality development")
        # can't misclassify a flight line as a ground duty.
        act_code = None if is_flight else _detect_code(line_str)
        if act_code is not None or is_flight:
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

            if is_flight:
                activity_type = "FLIGHT"
                # Bounded so 'UL60622SEP26' parses as UL606 + date, not UL60622
                m = re.search(r'(?<![A-Z0-9])UL\s*(\d{1,4}?)(?=\d{2}[A-Z]{3}\d{2}|\D|$)', line_str)
                if m:
                    flight_no = f"UL {m.group(1)}"
            else:
                bucket = ground_code_bucket(act_code)
                if bucket == "off":
                    activity_type = "DAY OFF"
                elif bucket == "tof":
                    activity_type = "TIMEOFF"
                elif bucket == "layover":
                    activity_type = "LAYOVER"
                elif bucket == "standby":
                    activity_type = "STANDBY"
                elif bucket in ("sick", "leave", "neutral"):
                    activity_type = "LEAVE"
                elif bucket == "duty":
                    activity_type = "DUTY"
                else:
                    activity_type = "OTHER"

            # Raw activity code — needed for chip labels & acting-duty marking
            if activity_type == "FLIGHT":
                code = flight_no.replace(" ", "")
            elif activity_type == "LAYOVER":
                code = "HTL"
            else:
                code = act_code or activity_type

            # Multi-day span support (layover/standby/off blocks with start+end stamps)
            if dt_stamps:
                start_dt, end_dt = min(dt_stamps), max(dt_stamps)
                if activity_type in ("DAY OFF", "LEAVE"):
                    # performed view: exclusive next-day 00:00 boundary → single day
                    start_dt, end_dt = _clamp_exclusive_end(start_dt, end_dt)
                if activity_type in ("LAYOVER", "STANDBY", "DAY OFF", "TIMEOFF", "LEAVE"):
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
                if (len(p) == 3 and p.isalnum() and re.search(r'\d', p)
                        and p not in ["FA", "J28", "CMB", "CAN", "BKK", "TRZ", "MAA", "MLE",
                                      "DMM", "BLR", "DXB", "RUH", "LHE", "ICN", "DEL", "PUR"]):
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
            elif activity_type in ("STANDBY", "LAYOVER", "DUTY") and dt_stamps:
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

def _roster_acclimatized(rows, report_dt, away_origin=None):
    """Best-effort acclimatization state just before `report_dt`, from roster
    history. A layover at a station > 2h off CMB de-acclimatizes; the crew
    re-acclimatizes after 3 consecutive local nights at CMB (counted from the
    return-leg arrival). A duty reporting from an outstation > 2h off CMB is
    also not acclimatized. Defaults to acclimatized when history is thin."""
    if away_origin:
        off = AIRPORT_OFFSET_H.get(away_origin, 5.5)
        if abs(off - 5.5) > 2:
            return False
    latest_return = None
    for i, r in enumerate(rows):
        if r["Type"] != "LAYOVER":
            continue
        stn = (r.get("Route") or "").strip()
        off = AIRPORT_OFFSET_H.get(stn, 5.5)
        if abs(off - 5.5) <= 2:
            continue  # within the 2h zone → stays acclimatized
        ret_arr = None
        for j in range(i + 1, len(rows)):
            nr = rows[j]
            if nr["Type"] == "FLIGHT" and isinstance(nr.get("ARRdt"), datetime):
                o, d = _route_od(nr.get("Route"))
                if d == "CMB":
                    ret_arr = nr["ARRdt"]
                break
        if isinstance(ret_arr, datetime) and ret_arr <= report_dt:
            latest_return = ret_arr if latest_return is None else max(latest_return, ret_arr)
    if latest_return is None:
        return True
    return _count_local_nights(latest_return, report_dt) >= 3


def _preceding_rest_h(rows, report_dt):
    """Rest (hours) before `report_dt`, from the last duty end in the roster."""
    prev_end = None
    for r in rows:
        if r["Type"] in ("FLIGHT", "STANDBY", "DUTY"):
            e = r.get("COdt") or r.get("ARRdt")
            if isinstance(e, datetime) and e <= report_dt:
                prev_end = e if prev_end is None else max(prev_end, e)
    if prev_end is None:
        return None
    return max(0.0, (report_dt - prev_end).total_seconds() / 3600)


def _next_report(rows, duties, after_dt):
    """Next report time (duty check-in or standby start) strictly after after_dt."""
    cands = [{"dt": du["report"], "label": du["label"]} for du in duties
             if isinstance(du.get("report"), datetime) and du["report"] > after_dt]
    for sb in rows:
        if sb["Type"] != "STANDBY":
            continue
        sdt = sb.get("CIdt")
        if not isinstance(sdt, datetime) and sb["DateObj"] is not None and sb["Departure"] != "-":
            try:
                sdt = datetime.combine(sb["DateObj"].date(), datetime.strptime(sb["Departure"], "%H:%M").time())
            except ValueError:
                sdt = None
        if isinstance(sdt, datetime) and sdt > after_dt:
            cands.append({"dt": sdt, "label": f"Standby {sb.get('Code') or ''}".strip()})
    return min(cands, key=lambda c: c["dt"]) if cands else None


def delay_impact_note(rows, flight_no, fdate, delay_mins):
    """FDP impact of a delay once the crew has reported (8.2.6 reported-then-
    delayed). The FDP clock runs from check-in to the duty's FINAL on-chock, so a
    delay on any sector (e.g. outbound UL404) pushes the return leg (UL405) later
    and can exceed the duty's maximum FDP. The 17h30m rest guideline is checked
    only AFTER the duty ends, against the next report."""
    if not delay_mins or delay_mins <= 0:
        return None
    duties = build_duties(rows)
    fkey = flight_no.replace(" ", "")
    duty = next((du for du in duties
                 if any(s["flight"].replace(" ", "") == fkey and isinstance(s.get("dep"), datetime)
                        and s["dep"].date() == fdate for s in du["sectors"])), None)
    if duty is None:
        return None
    report, end = duty["report"], duty["chocks_on"]
    if not isinstance(report, datetime) or not isinstance(end, datetime) or end <= report:
        return None
    n = len(duty["sectors"])
    first_dep = duty["sectors"][0].get("dep")
    band = fdp_band((first_dep - timedelta(hours=1)).time()) if isinstance(first_dep, datetime) else "0600-0759"
    acclim = _roster_acclimatized(rows, report, away_origin=duty.get("origin"))
    prec_rest = _preceding_rest_h(rows, report) if not acclim else None
    max_fdp = fdp_limit_min(acclim, band, n, prec_rest)
    delayed_end = end + timedelta(minutes=delay_mins)
    fdp_del = int((delayed_end - report).total_seconds() // 60)
    idx = next((i for i, s in enumerate(duty["sectors"])
                if s["flight"].replace(" ", "") == fkey), 0)
    rest_sectors = duty["sectors"][idx + 1:]
    remaining = "/".join(s["flight"].replace(" ", "") for s in rest_sectors)
    over = fdp_del - max_fdp
    lines = []
    if over > 0:
        if remaining:
            verb = (f"Operating <b>{remaining}</b> would exceed the duty FDP by {_fmt_hm(over)} "
                    "— do not operate; replan / crew replacement required.")
        else:
            verb = (f"the duty would exceed its maximum by {_fmt_hm(over)} "
                    "— do not depart; replan / crew replacement required.")
        lines.append(
            f"⚠️ <b>FDP impact:</b> a {delay_mins} min delay on {fkey} pushes this duty to "
            f"{_fmt_hm(fdp_del)} (check-in {report:%H:%M} → final on-chock {delayed_end:%H:%M}) "
            f"but the maximum is {_fmt_hm(max_fdp)} (Table {'A' if acclim else 'B'}, band {band}, "
            f"{n} sector(s)). {verb}")
    else:
        spare_txt = f"{remaining} can still be operated" if remaining else "the duty still ends within FDP"
        lines.append(
            f"✅ <b>FDP impact:</b> a {delay_mins} min delay extends this duty to "
            f"{_fmt_hm(fdp_del)} (final on-chock {delayed_end:%H:%M}) vs a {_fmt_hm(max_fdp)} maximum "
            f"(Table {'A' if acclim else 'B'}) — {_fmt_hm(-over)} to spare, {spare_txt}.")
    nxt = _next_report(rows, duties, delayed_end)
    if nxt is not None:
        rest = (nxt["dt"] - delayed_end).total_seconds() / 3600
        if rest < MIN_REST_HOURS:
            lines.append(
                f"🛏 After this duty, rest before <b>{nxt['label']}</b> drops to {rest:.1f}h — "
                f"BELOW the 17h30m FAU minimum.")
        else:
            lines.append(
                f"🛏 Rest after this duty before <b>{nxt['label']}</b>: {rest:.1f}h (min 17h30m) — OK.")
    lines.append(
        "ℹ️ If the delay was announced <b>before</b> you reported, it is delayed reporting (8.2.6): "
        "your report time shifts and the FDP clock starts at the new report time — this check does not apply.")
    return "<br>".join(lines)


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


# Extra crew-intel per station: currency + approximate rate, plug type, visa notes
# for partners/family, how public transport is paid (tap vs cash vs travel card),
# and a few crew tips. Hotel/laundry/breakfast details to be added later.
# Extra crew-intel per station: currency + approximate LKR rate, plug type, visa
# notes for partners/family travelling on a SRI LANKAN passport, how public
# transport is paid (tap vs cash vs travel card), and a few crew tips.
# Hotel/laundry/breakfast details to be added later.
VISA_DISCLAIMER = "Visas are for 🇱🇰 (Sri Lankan) passports · indicative — always verify before travel."

STATION_EXTRAS = {
    "CMB": {"cur": "LKR", "fx": "Home currency", "plug": "D / M / G",
            "visa_family": "Home base — no visa for the family.",
            "transit": "Buses: cash or a travel card · PickMe/Uber take cards in-app.",
            "tips": ["Drink bottled water", "Uber/PickMe are cheap & reliable", "Cards accepted city-wide"]},
    "DXB": {"cur": "AED", "fx": "1 AED ≈ LKR 82", "plug": "G",
            "visa_family": "eVisa required (apply in advance) — no visa-on-arrival for LK passports.",
            "transit": "Metro/bus/tram all tap contactless Visa/Mastercard (nol) · a silver nol card works if a foreign card is blocked.",
            "tips": ["nol card from any station", "Taxis take cards", "Free hotel shuttles common"]},
    "AUH": {"cur": "AED", "fx": "1 AED ≈ LKR 82", "plug": "G",
            "visa_family": "eVisa required (apply in advance).",
            "transit": "Hafilat card for buses (cash top-up) · taxis take cards.",
            "tips": ["Careem/Uber everywhere"]},
    "DOH": {"cur": "QAR", "fx": "1 QAR ≈ LKR 82", "plug": "G",
            "visa_family": "eVisa (Hayya) or a hotel-arranged visa on arrival.",
            "transit": "Metro taps contactless Visa/Mastercard · Karwa card also works.",
            "tips": ["Metro links the airport", "Cards accepted almost everywhere"]},
    "RUH": {"cur": "SAR", "fx": "1 SAR ≈ LKR 80", "plug": "G",
            "visa_family": "eVisa available (Saudi tourist eVisa).",
            "transit": "Riyadh Metro taps contactless · many buses cash-only · Careem/Uber in-app cards.",
            "tips": ["eVisa via official portal", "Malls & hotels take cards"]},
    "DMM": {"cur": "SAR", "fx": "1 SAR ≈ LKR 80", "plug": "G",
            "visa_family": "eVisa available (Saudi tourist eVisa).",
            "transit": "Buses cash or SPT card · Uber/Careem in-app cards.",
            "tips": []},
    "JED": {"cur": "SAR", "fx": "1 SAR ≈ LKR 80", "plug": "G",
            "visa_family": "eVisa available (note Hajj/Umrah-specific rules).",
            "transit": "Buses cash/card · Uber/Careem.",
            "tips": []},
    "KWI": {"cur": "KWD", "fx": "1 KWD ≈ LKR 970", "plug": "G",
            "visa_family": "eVisa required for LK passports.",
            "transit": "Buses mostly cash or PAM card · Careem/Uber.",
            "tips": []},
    "BAH": {"cur": "BHD", "fx": "1 BHD ≈ LKR 800", "plug": "G",
            "visa_family": "eVisa required for LK passports.",
            "transit": "GoCard for buses · taxis cash/card.",
            "tips": []},
    "MCT": {"cur": "OMR", "fx": "1 OMR ≈ LKR 780", "plug": "G",
            "visa_family": "eVisa available for LK passports.",
            "transit": "Mwasalat buses take contactless/cash · OTaxi app.",
            "tips": []},
    "BKK": {"cur": "THB", "fx": "1 THB ≈ LKR 8", "plug": "A / B / C",
            "visa_family": "Visa-free — 30 days for LK passports.",
            "transit": "BTS/MRT tap Visa/Mastercard on newer gates · Rabbit card for BTS · city buses cash only.",
            "tips": ["Grab/Bolt are cheap", "7-Eleven takes cards", "Keep small cash for street food"]},
    "SIN": {"cur": "SGD", "fx": "1 SGD ≈ LKR 222", "plug": "G",
            "visa_family": "Visa required for LK passports — apply in advance; SG Arrival Card for all.",
            "transit": "Tap any contactless Visa/Mastercard on MRT & buses (SimplyGo) — no separate card needed.",
            "tips": ["Hawker stalls often cash-only", "Grab/Gojek everywhere", "Tap water is safe"]},
    "KUL": {"cur": "MYR", "fx": "1 MYR ≈ LKR 68", "plug": "G",
            "visa_family": "Visa-free — 90 days for LK passports.",
            "transit": "Touch 'n Go card needed for rail/bus (cash top-up) · Grab everywhere (cards in-app).",
            "tips": ["Grab is the default", "Cards widely accepted"]},
    "CGK": {"cur": "IDR", "fx": "1,000 IDR ≈ LKR 19", "plug": "C / F",
            "visa_family": "Visa on arrival (paid) available for LK passports.",
            "transit": "e-money cards (e-Money/Flazz) for Commuter Line/MRT · contactless cards rare · Gojek/Grab in-app.",
            "tips": ["Gojek for short hops", "Carry some cash"]},
    "HKG": {"cur": "HKD", "fx": "1 HKD ≈ LKR 39", "plug": "G",
            "visa_family": "Visa required for LK passports — apply in advance.",
            "transit": "Octopus card (cash top-up) for MTR/bus — contactless credit cards NOT accepted · Airport Express takes cards.",
            "tips": ["Octopus at the airport", "Cards accepted city-wide"]},
    "CAN": {"cur": "CNY", "fx": "1 CNY ≈ LKR 42", "plug": "A / I",
            "visa_family": "Visa required · 144-hr transit-without-visa possible when transiting onward.",
            "transit": "Alipay/WeChat dominate · cash works · foreign contactless cards unreliable on buses · DiDi app.",
            "tips": ["Set up Alipay/WeChat if you can", "Carry cash"]},
    "PVG": {"cur": "CNY", "fx": "1 CNY ≈ LKR 42", "plug": "A / I",
            "visa_family": "Visa required · 144-hr transit-without-visa possible when transiting onward.",
            "transit": "Metro takes Alipay/WeChat & cash · foreign cards unreliable · Maglev takes cards.",
            "tips": []},
    "PEK": {"cur": "CNY", "fx": "1 CNY ≈ LKR 42", "plug": "A / I",
            "visa_family": "Visa required · 144-hr transit-without-visa possible when transiting onward.",
            "transit": "Metro cash/Alipay · foreign cards unreliable.",
            "tips": []},
    "ICN": {"cur": "KRW", "fx": "1,000 KRW ≈ LKR 226", "plug": "F / C",
            "visa_family": "Visa required for LK passports (K-ETA is only for visa-free nationalities).",
            "transit": "T-money card (cash top-up) for metro/bus · some airport buses take cards.",
            "tips": ["Get a T-money at the airport", "Cards accepted across Seoul"]},
    "NRT": {"cur": "JPY", "fx": "1 JPY ≈ LKR 2", "plug": "A / B",
            "visa_family": "Visa required for LK passports — embassy application.",
            "transit": "Suica/Pasmo (cash top-up) — contactless credit cards only on some lines · Welcome Suica at the airport.",
            "tips": ["Get a Suica at the airport", "Japan is still cash-friendly"]},
    "KIX": {"cur": "JPY", "fx": "1 JPY ≈ LKR 2", "plug": "A / B",
            "visa_family": "Visa required for LK passports — embassy application.",
            "transit": "ICOCA card for trains/buses.",
            "tips": []},
    "MLE": {"cur": "MVR / USD", "fx": "USD widely used · 1 USD ≈ LKR 300", "plug": "D / G",
            "visa_family": "Free visa on arrival (30 days) for all nationalities.",
            "transit": "Cash — USD widely accepted · resort speedboats arranged by the hotel.",
            "tips": ["USD accepted everywhere", "Resort transfers pre-arranged"]},
    "MAA": {"cur": "INR", "fx": "1 INR ≈ LKR 3.6", "plug": "C / D",
            "visa_family": "eVisa available for LK passports.",
            "transit": "Metro/bus cash & cards · UPI for locals · Ola/Uber cards in-app.",
            "tips": ["Ola/Uber cheap", "Carry small notes"]},
    "DEL": {"cur": "INR", "fx": "1 INR ≈ LKR 3.6", "plug": "C / D",
            "visa_family": "eVisa available for LK passports.",
            "transit": "Delhi Metro token/card (cash) · Ola/Uber cards · autos cash.",
            "tips": []},
    "BLR": {"cur": "INR", "fx": "1 INR ≈ LKR 3.6", "plug": "C / D",
            "visa_family": "eVisa available for LK passports.",
            "transit": "Namma Metro card/QR · buses cash · Uber/Ola.",
            "tips": []},
    "BOM": {"cur": "INR", "fx": "1 INR ≈ LKR 3.6", "plug": "C / D",
            "visa_family": "eVisa available for LK passports.",
            "transit": "Local trains UTS app/cash · Uber/Ola cards.",
            "tips": []},
    "HYD": {"cur": "INR", "fx": "1 INR ≈ LKR 3.6", "plug": "C / D",
            "visa_family": "eVisa available for LK passports.",
            "transit": "Metro cards · buses cash · Uber/Ola.",
            "tips": []},
    "CCU": {"cur": "INR", "fx": "1 INR ≈ LKR 3.6", "plug": "C / D",
            "visa_family": "eVisa available for LK passports.",
            "transit": "Metro cards · buses cash · Uber/Ola.",
            "tips": []},
    "COK": {"cur": "INR", "fx": "1 INR ≈ LKR 3.6", "plug": "C / D",
            "visa_family": "eVisa available for LK passports.",
            "transit": "Kochi Metro cards · buses cash.",
            "tips": []},
    "TRV": {"cur": "INR", "fx": "1 INR ≈ LKR 3.6", "plug": "C / D",
            "visa_family": "eVisa available for LK passports.",
            "transit": "Buses cash · autos cash.",
            "tips": []},
    "TRZ": {"cur": "INR", "fx": "1 INR ≈ LKR 3.6", "plug": "C / D",
            "visa_family": "eVisa available for LK passports.",
            "transit": "Buses cash · autos cash.",
            "tips": []},
    "DAC": {"cur": "BDT", "fx": "1 BDT ≈ LKR 2.7", "plug": "C / D / G",
            "visa_family": "Visa on arrival available for LK passports.",
            "transit": "Cash · Uber/Pathao cards in-app · metro takes cards.",
            "tips": []},
    "KHI": {"cur": "PKR", "fx": "1 PKR ≈ LKR 1.1", "plug": "C / D",
            "visa_family": "eVisa required in advance for LK passports.",
            "transit": "Cash · Careem/Uber cards in-app.",
            "tips": []},
    "LHE": {"cur": "PKR", "fx": "1 PKR ≈ LKR 1.1", "plug": "C / D",
            "visa_family": "eVisa required in advance for LK passports.",
            "transit": "Cash · Careem/Uber cards in-app.",
            "tips": []},
    "SEZ": {"cur": "SCR", "fx": "1 SCR ≈ LKR 21", "plug": "G",
            "visa_family": "Visa-free (visitor's permit on arrival) for all.",
            "transit": "Cash & cards · SPTC buses cash/card · taxis cards.",
            "tips": ["Free visitor's permit on arrival"]},
    "LHR": {"cur": "GBP", "fx": "1 GBP ≈ LKR 380", "plug": "G",
            "visa_family": "Standard Visitor visa required (the ETA is only for visa-free nationalities).",
            "transit": "Tap any contactless Visa/Mastercard on Tube & bus (no Oyster needed) · daily fare cap.",
            "tips": ["Contactless everywhere", "Heathrow Express takes cards"]},
    "CDG": {"cur": "EUR", "fx": "1 EUR ≈ LKR 326", "plug": "C / E",
            "visa_family": "Schengen visa required for LK passports.",
            "transit": "Navigo Easy card or contactless cards on RATP · some buses cash.",
            "tips": ["Contactless on metro", "Watch for pickpockets"]},
    "FRA": {"cur": "EUR", "fx": "1 EUR ≈ LKR 326", "plug": "C / F",
            "visa_family": "Schengen visa required for LK passports.",
            "transit": "RMV/U-Bahn take contactless cards · some ticket machines cash.",
            "tips": []},
    "ZRH": {"cur": "CHF", "fx": "1 CHF ≈ LKR 340", "plug": "J",
            "visa_family": "Schengen visa required for LK passports.",
            "transit": "Contactless cards everywhere (SBB) — no cash needed.",
            "tips": ["Cards everywhere", "Expensive — plan meals"]},
    "IST": {"cur": "TRY", "fx": "1 TRY ≈ LKR 9", "plug": "C / F",
            "visa_family": "eVisa available for LK passports.",
            "transit": "Istanbulkart (cash top-up) or contactless cards · taxis cash/app.",
            "tips": []},
    "SYD": {"cur": "AUD", "fx": "1 AUD ≈ LKR 200", "plug": "I",
            "visa_family": "Visitor visa (subclass 600) required — no ETA for LK passports.",
            "transit": "Opal: tap contactless Visa/Mastercard directly on trains/buses/ferries.",
            "tips": ["Tap your own card — no Opal needed", "Airport train adds a fee"]},
    "MEL": {"cur": "AUD", "fx": "1 AUD ≈ LKR 200", "plug": "I",
            "visa_family": "Visitor visa (subclass 600) required — no ETA for LK passports.",
            "transit": "Myki accepts contactless cards (or a Myki card).",
            "tips": []},
}

# Little monospace diagrams of what each plug/socket looks like, so nobody has
# to decode "Type G" from memory.
PLUG_SHAPES = {
    "A": ("‖", "two flat pins"),
    "B": ("‖\n ●", "two flat pins + ground"),
    "C": ("• •", "two round pins"),
    "D": ("• •\n •", "three round pins (triangle)"),
    "E": ("• •\n⌾", "two round pins + earth pin"),
    "F": ("• •", "two round pins + side clips"),
    "G": ("│\n▬ ▬", "three rectangular pins"),
    "I": ("\\ /\n │", "two slanted pins + ground"),
    "J": ("• • •", "three round pins in a row"),
    "M": ("● ●\n ●", "three large round pins"),
}


def plug_diagram_html(plug_letters):
    """Render plug types as little monospace diagrams instead of cryptic letters."""
    parts = []
    for letter in (plug_letters or "").split("/"):
        letter = letter.strip()
        sh = PLUG_SHAPES.get(letter)
        if not sh:
            continue
        art, cap = sh
        art_html = art.replace("\n", "<br>")
        parts.append(
            f"<span style='display:inline-block;font-family:ui-monospace,Menlo,Consolas,monospace;"
            f"font-size:13px;line-height:1.15;white-space:nowrap;text-align:center;'>{art_html}</span>"
            f"<span class='muted' style='font-size:10.5px;margin-left:4px;'>{cap}</span>")
    return " &nbsp;&nbsp; ".join(parts)


def station_extra(iata):
    """Extra crew-intel for a station (currency/plug/visa/transport/tips)."""
    return STATION_EXTRAS.get(iata, {})


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
    fat = compute_fatigue(rows)
    fatigue = fat["score"]
    fat_label = fat["label"]
    allow_rows = []
    for lv in layovers:
        stn = lv["station"] or "?"
        nights = lv.get("nights") or (max(1, int((lv["ground_hrs"] or 24) // 24)) if lv["ground_hrs"] else 1)
        rate = PER_DIEM.get(stn, PER_DIEM_DEFAULT)
        allow_rows.append((stn, nights, rate, nights * rate))
    return {"block_hrs": round(block_hrs, 1), "block_target": 85, "flights": n_flights,
            "redeyes": redeyes, "max_streak": max_streak, "fatigue": fatigue,
            "fatigue_label": fat_label, "fatigue_parts": fat["parts"], "daily_min": daily_min,
            "allowance_rows": allow_rows, "allowance_total": sum(a[3] for a in allow_rows),
            "layovers": layovers}


def compute_fatigue(rows):
    """Fatigue proxy (0–10) grounded in the FOM Part A Ch.08 fatigue drivers.
    This is a HEURISTIC — the FOM states the objectives and prescriptive limits
    but gives no scoring formula. Drivers, each mapped to its FOM source:
      · early/late/night duty load — §8.2.2 (report early / finish late over
        consecutive days → sleep deprivation) + night duties more arduous
      · longest consecutive run touching 0100–0659 — §8.2.2
      · day/night alternation — §8.5 (avoid alternating day/night duties)
      · 18–30h rest after a time-zone-crossing duty — §8.5 (undesirable)
      · cumulative duty load vs 210h/28d — §8.3.d
      · recovery deficit — §8.0 (≥2 consecutive nights of unrestricted sleep)
    Returns {score, label, parts:[{label, pts, detail}]}."""
    parts = []
    duties = build_duties(rows)
    for du in duties:
        start, end = du["report"], du["chocks_on"]
        if not isinstance(start, datetime) or not isinstance(end, datetime) or end <= start:
            end = (start if isinstance(start, datetime) else datetime.now()) + timedelta(minutes=1)
        du["start"], du["end"] = start, end
        c = duty_classify(start, end)
        du["early"], du["late"], du["night"] = c["early"], c["late"], c["night"]

    eln = sum(1 for d in duties if d["early"] or d["late"] or d["night"])
    touching = [d for d in duties if _spans_window(d["start"], d["end"], dtime(1, 0), dtime(6, 59))]
    run0106 = max((len(r) for r in _group_runs(touching, lambda d: True)), default=0)

    ordered = sorted(duties, key=lambda d: d["start"])
    bands = ["night" if (d["start"].time() < dtime(7, 0) or d["start"].time() >= dtime(18, 0))
             else "day" for d in ordered]
    swings = sum(1 for i in range(1, len(bands)) if bands[i] != bands[i - 1])

    bad_rest = 0
    for i, du in enumerate(ordered):
        # "long flights crossing MANY time zones" (§8.5) — only count a
        # crossing beyond 4h of offset difference (long-haul). NOTE: this
        # threshold is fatigue-only; acclimatization stays at 2h per the FOM.
        tz_cross = any(abs(AIRPORT_OFFSET_H.get(s["o"], 5.5) - AIRPORT_OFFSET_H.get(s["d"], 5.5)) > 4
                       for s in du["sectors"])
        if not tz_cross or i + 1 >= len(ordered):
            continue
        rest = (ordered[i + 1]["start"] - du["end"]).total_seconds() / 3600
        if 18 < rest <= 30:
            bad_rest += 1

    periods = [{"start": d["start"], "end": d["end"],
                "minutes": int((d["end"] - d["start"]).total_seconds() // 60)} for d in duties]
    for r in rows:
        if r["Type"] in ("STANDBY", "DUTY") and r.get("CIdt"):
            s = r["CIdt"]
            e = r.get("COdt") or s
            if not isinstance(e, datetime) or e <= s:
                e = s + timedelta(minutes=1)
            periods.append({"start": s, "end": e, "minutes": int((e - s).total_seconds() // 60)})
    periods.sort(key=lambda p: p["start"])
    cum28 = 0
    if periods:
        anchors = sorted({p["start"].date() for p in periods})
        for anchor in anchors:
            lo = anchor - timedelta(days=27)
            cum28 = max(cum28, sum(p["minutes"] for p in periods if lo <= p["start"].date() <= anchor))
    cum_ratio = cum28 / (210 * 60)

    ds = _day_status_map(rows)
    offs = sorted(d for d, s in ds.items() if "off" in s)
    recovery_ok = any((offs[i + 1] - offs[i]).days == 1 for i in range(len(offs) - 1))

    score = 0.6
    def add(label, pts, detail):
        parts.append({"label": label, "pts": round(pts, 1), "detail": detail})
    p = min(3.0, 0.4 * eln); add("Early / late / night duties", p, f"{eln} duty(ies)"); score += p
    p = min(2.0, 0.6 * max(0, run0106 - 1)); add("0100–0659 consecutive run", p, f"longest {run0106}"); score += p
    p = min(1.5, 0.5 * swings); add("Day/night alternation", p, f"{swings} swing(s)"); score += p
    p = min(1.5, 0.75 * bad_rest); add("18–30h rest after TZ flight", p, f"{bad_rest} occurrence(s)"); score += p
    p = min(2.0, 2.0 * cum_ratio); add("Cumulative duty load", p, f"{_fmt_hm(cum28)} of 210h/28d"); score += p
    p = 0.0 if recovery_ok else 0.6; add("Recovery deficit", p,
        "2-off block present" if recovery_ok else "no 2 consecutive off days"); score += p
    score = min(10.0, round(score, 1))
    label = "Low" if score < 4 else ("Moderate" if score < 7 else "High")
    return {"score": score, "label": label, "parts": parts, "eln": eln,
            "run0106": run0106, "swings": swings, "bad_rest": bad_rest,
            "cum28": cum28, "recovery_ok": recovery_ok}

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


def _gmt_str(off):
    """UTC offset hours -> 'GMT+5:30'."""
    sign = "+" if off >= 0 else "-"
    h = int(abs(off))
    m = int(round((abs(off) - h) * 60))
    return f"GMT{sign}{h}" + (f":{m:02d}" if m else "")


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
        bucket = ("between18_30" if preceding_rest_h is not None and 18 < preceding_rest_h <= 30
                  else "up18_or_over30")
        row = FDP_TABLE_B[bucket]
    return row[sectors - 1] + 60


def _count_local_nights(start_dt, end_dt):
    """Count consecutive local nights at base between start_dt (on the ground,
    e.g. arrival) and end_dt (next duty start, e.g. check-in). FOM definition:
    a local night is a period of 8 hours falling between 2200 and 0800 local, so
    a night counts when the crew is on the ground for ≥8h within that window."""
    if not isinstance(start_dt, datetime) or not isinstance(end_dt, datetime) or end_dt <= start_dt:
        return 0
    n = 0
    day = start_dt.date()
    last_day = end_dt.date() + timedelta(days=1)
    while day <= last_day:
        win_start = datetime.combine(day, dtime(22, 0))
        win_end = datetime.combine(day + timedelta(days=1), dtime(8, 0))
        if win_start > end_dt:
            break
        overlap = min(end_dt, win_end) - max(start_dt, win_start)
        if overlap >= timedelta(hours=8):
            n += 1
        day += timedelta(days=1)
    return n


def _spans_window(start_dt, end_dt, win_start_t, win_end_t):
    """True if [start_dt, end_dt] overlaps any daily [win_start_t → win_end_t] window
    (handles windows that cross midnight, e.g. 2200→0800)."""
    if not isinstance(start_dt, datetime) or not isinstance(end_dt, datetime) or end_dt <= start_dt:
        return False
    d = start_dt.date()
    for _ in range(4):
        ws = datetime.combine(d, win_start_t)
        we = datetime.combine(d, win_end_t)
        if we <= ws:
            we += timedelta(days=1)
        if start_dt < we and end_dt > ws:
            return True
        d += timedelta(days=1)
    return False


def duty_classify(ci_dt, arr_dt):
    """Classify a duty window (Colombo local) into early / late / night flags.
    Early Start = commences 0500–0659; Late Finish = finishes 0100–0159;
    Night Duty = any part falls within 0200–0459."""
    if not isinstance(ci_dt, datetime) or not isinstance(arr_dt, datetime):
        return {"early": False, "late": False, "night": False}
    if arr_dt <= ci_dt:
        arr_dt += timedelta(days=1)
    return {
        "early": dtime(5, 0) <= ci_dt.time() <= dtime(6, 59),
        "late": dtime(1, 0) <= arr_dt.time() <= dtime(1, 59),
        "night": _spans_window(ci_dt, arr_dt, dtime(2, 0), dtime(4, 59)),
    }


def apply_delay(acclimatized, sectors, preceding_rest_h, dep_dt, delay_min):
    """8.2.6 delayed reporting. Returns (band, note). delay_min < 240 → max FDP from
    the original report band; ≥ 240 → more limiting band of planned vs actual report."""
    band_orig = fdp_band((dep_dt - timedelta(hours=1)).time())
    if delay_min < 240:
        return band_orig, (f"Delay &lt; 4 h → max FDP from the ORIGINAL report band ({band_orig}); "
                           "the FDP clock starts at the actual (delayed) report time.")
    dep_act = dep_dt + timedelta(minutes=delay_min)
    band_act = fdp_band((dep_act - timedelta(hours=1)).time())
    v1 = fdp_limit_min(acclimatized, band_orig, sectors, preceding_rest_h)
    v2 = fdp_limit_min(acclimatized, band_act, sectors, preceding_rest_h)
    band = band_orig if v1 <= v2 else band_act
    return band, (f"Delay ≥ 4 h → more limiting band of planned ({band_orig}) vs actual ({band_act}) "
                  f"= <b>{band}</b>; the FDP clock starts 4 h after the original report time.")


def extension_minutes(split_rest_min=None, relief_rest_min=None, relief_type=None):
    """Returns (extra_minutes, cap_min_or_None, detail). Split duty (8.2.13): rest 3–10 h
    between sectors → extend by ½ of the rest. In-flight relief (8.2.12): needs ≥ 3 h rest;
    bunk → +½, seat → +⅓ of rest taken; caps 19 h cabin (bunk) / 16 h cabin (seat)."""
    extra = 0
    cap = None
    detail = []
    if split_rest_min and 180 <= split_rest_min <= 600:
        add = split_rest_min // 2
        extra += add
        detail.append(f"split-duty rest {_fmt_hm(split_rest_min)} → +{_fmt_hm(add)}")
    if relief_rest_min and relief_rest_min >= 180 and relief_type in ("Bunk", "Seat"):
        frac = 0.5 if relief_type == "Bunk" else (1 / 3)
        add = int(relief_rest_min * frac)
        extra += add
        cap = (19 * 60) if relief_type == "Bunk" else (16 * 60)
        detail.append(f"in-flight relief ({relief_type}) {_fmt_hm(relief_rest_min)} → +{_fmt_hm(add)}, "
                      f"capped at {_fmt_hm(cap)}")
    return extra, cap, " · ".join(detail)


CUMULATIVE_LIMITS = {"7d": 60 * 60, "14d": 105 * 60, "28d": 210 * 60}   # cabin crew 8.3.d
CUMULATIVE_7D_SOFT = 65 * 60                                            # unforeseen-delay allowance


def _duty_periods(rows):
    """Countable periods for cumulative totals: flight duties + standby/duty in
    full — exactly the source the dashboard's 7/14/28-day cumulative uses."""
    periods = []
    for du in build_duties(rows):
        start, end = du["report"], du["chocks_on"]
        if not isinstance(start, datetime) or not isinstance(end, datetime) or end <= start:
            end = start + timedelta(minutes=1)
        periods.append({"label": du["label"], "start": start, "end": end,
                        "minutes": int((end - start).total_seconds() // 60)})
    for r in rows:
        if r["Type"] in ("STANDBY", "DUTY") and r.get("CIdt"):
            s = r["CIdt"]
            e = r.get("COdt") or s
            if not isinstance(e, datetime) or e <= s:
                e = s + timedelta(minutes=1)
            periods.append({"label": (r.get("Code") or r["Type"]).strip(),
                            "start": s, "end": e,
                            "minutes": int((e - s).total_seconds() // 60)})
    periods.sort(key=lambda p: p["start"])
    return periods


def _cumulative_max(periods):
    """Max rolling 7/14/28-day duty minutes over the given periods."""
    if not periods:
        return {"7d": 0, "14d": 0, "28d": 0}
    anchors = sorted({p["start"].date() for p in periods})
    out = {}
    for wkey, days in (("7d", 7), ("14d", 14), ("28d", 28)):
        best = 0
        for anchor in anchors:
            lo = anchor - timedelta(days=days - 1)
            total = sum(p["minutes"] for p in periods if lo <= p["start"].date() <= anchor)
            if total > best:
                best = total
        out[wkey] = best
    return out


def standby_fdp_check(acclimatized, sectors, preceding_rest_h, band, sby_minutes, case_c):
    """8.2.8 — called out from standby into an FDP. Returns (allowed_total_min, case, detail).
    The combined standby + FDP may be 1 h longer than the flight-crew FDP (8.3.c)."""
    table_val = fdp_limit_min(acclimatized, band, sectors, preceding_rest_h) - 60   # flight-crew value
    if case_c:
        return (table_val + 60, "Case C",
                "Standby at home/suitable accommodation 2200\u20130800 with \u2264 2 h notice \u2192 normal FDP "
                "applies, no standby addition.")
    if sby_minutes < 6 * 60:
        return (sby_minutes + table_val + 60, "Case A",
                "Called out before 6 h of standby \u2192 total allowed = standby time + Table A/B FDP (+ 1 h cabin).")
    return (6 * 60 + table_val + 60, "Case B",
            "Called out after more than 6 h of standby \u2192 total allowed = 6 h + Table A/B FDP (+ 1 h cabin); "
            "standby beyond 6 h is deducted.")


def _group_runs(items, keyfn, break_h=34.0):
    """Group qualifying items into runs, where consecutive qualifying items are
    separated by less than break_h hours of free time (FOM 'consecutive' = not
    broken by \u2265 34 h free from such duties)."""
    runs, cur = [], []
    for it in items:
        if not keyfn(it):
            continue
        if cur and (it["start"] - cur[-1]["end"]).total_seconds() < break_h * 3600:
            cur.append(it)
        else:
            if cur:
                runs.append(cur)
            cur = [it]
    if cur:
        runs.append(cur)
    return runs


def _day_status_map(rows):
    """Calendar-date → set(status). FLIGHT/STANDBY/LAYOVER/DUTY → 'duty',
    DAY OFF (OFF/ROF/HOT/OVO) → 'off', sick-codes → 'off' (company counts sick
    towards days off), other leave/neutral → 'leave', TIMEOFF → 'tof'. Multi-day
    rows span every date they cover (8.2.17 counting: layover days count as
    duty; leave/neutral is not a day off and not duty)."""
    day_status = {}
    for r in rows:
        if not r.get("DateObj"):
            continue
        start = r["DateObj"].date()
        end = r["EndDateObj"].date() if r.get("EndDateObj") else start
        if r["Type"] in ("FLIGHT", "STANDBY", "LAYOVER", "DUTY"):
            key = "duty"
        elif r["Type"] == "DAY OFF":
            key = "off"
        elif r["Type"] == "LEAVE":
            # company rule: sick AND annual leave (ALV/RLV/ALP) count towards
            # off days; other leave doesn't
            key = "off" if (ground_code_bucket(r.get("Code")) == "sick"
                            or r.get("Code") in ANNUAL_LEAVE_CODES) else "leave"
        elif r["Type"] == "TIMEOFF":
            key = "tof"   # time-off block: not duty, not a day off
        else:
            continue
        d = start
        while d <= end:
            day_status.setdefault(d, set()).add(key)
            d += timedelta(days=1)
    return day_status


def _max_duty_run(day_status, lo, hi):
    """Longest run of consecutive duty days across [lo, hi] (8.2.17.a)."""
    run = best = 0
    d = lo
    while d <= hi:
        if "duty" in day_status.get(d, set()):
            run += 1
            best = max(best, run)
        else:
            run = 0
        d += timedelta(days=1)
    return best


def _no_pair_14_windows(day_status, lo, hi):
    """Set of 14-day window START dates (windows fully inside [lo, hi]) that
    contain no adjacent pair of off days (8.2.17.b)."""
    out = set()
    d = lo
    while d + timedelta(days=13) <= hi:
        if not any("off" in day_status.get(d + timedelta(days=k), set())
                   and "off" in day_status.get(d + timedelta(days=k + 1), set())
                   for k in range(13)):
            out.add(d)
        d += timedelta(days=1)
    return out


def _period_off_count(day_status, period_start):
    """Number of off days inside one aligned 28-day roster period."""
    return sum(1 for d, s in day_status.items()
               if "off" in s and period_start <= d < period_start + timedelta(days=ROSTER_PERIOD_DAYS))


def _base_rule_status(day_status, lo, hi):
    """Which 8.2.17(a/b/c) rules the base roster already breaches.
    (a) a duty run > 7 anywhere; (b) any 14-day window with no adjacent off pair;
    (c) any aligned 28-day period with fewer than 7 off days (the user-confirmed
    per-roster minimum)."""
    brk = set()
    if _max_duty_run(day_status, lo, hi) > 7:
        brk.add("a")
    if _no_pair_14_windows(day_status, lo, hi):
        brk.add("b")
    pmin = roster_period_bounds(lo)[0]
    pmax = roster_period_bounds(hi)[0]
    p = pmin
    while p <= pmax:
        if _period_off_count(day_status, p) < 7:
            brk.add("c")
            break
        p += timedelta(days=ROSTER_PERIOD_DAYS)
    return brk


def mandatory_off_days(rows):
    """DAY OFF dates that are legally required by 8.2.17. A date is mandatory
    only when the roster currently MEETS a rule but turning that date into a
    duty day would BREACH it (new-breach-vs-base), so an already-short roster
    doesn't mark every day. Rule (c) uses the user-confirmed per-roster reading:
    an aligned 28-day period with exactly 7 off days makes all 7 load-bearing.
    Only PLANNED off days (OFF/ROF/HOT/OVO) are ever flagged mandatory — sick
    days count towards the off-day totals but are never themselves 'mandatory'.
    Returns {date: [rule label, ...]}."""
    day_status = _day_status_map(rows)
    off_days = sorted(d for d, s in day_status.items() if "off" in s)
    planned = sorted({r["DateObj"].date() for r in rows
                      if r["Type"] == "DAY OFF" and r.get("DateObj")})
    if not day_status or not off_days:
        return {}
    lo, hi = min(day_status), max(day_status)
    labels = {"a": "8th-day off", "b": "2-off-in-14", "c": "7-off-in-28"}
    base = _base_rule_status(day_status, lo, hi)
    base_b = _no_pair_14_windows(day_status, lo, hi)

    mand = {}
    for o in planned:
        alt = {d: set(v) for d, v in day_status.items()}
        alt[o].discard("off")
        alt[o].add("duty")
        rules = []
        if "a" not in base and _max_duty_run(alt, lo, hi) > 7:
            rules.append("a")
        if _no_pair_14_windows(alt, lo, hi) - base_b:
            rules.append("b")
        if _period_off_count(day_status, roster_period_bounds(o)[0]) == 7:
            rules.append("c")
        if rules:
            mand[o] = [labels[r] for r in rules]
    return mand


def off_day_rest_check(rows, context_rows=None):
    """8.2.17 day-off definition: each day-off block must give \u2265 34 h free of
    duty AND 2 local nights (8 h within 2200\u20130800 local) for the first off day,
    plus one further local night per extra consecutive off day. Free time runs
    from the previous duty's check-out to the next duty's check-in (leave days
    are free of duty, so they extend the window). Times treated as Colombo
    local.

    Day-off blocks are taken from `rows` only (the viewed period); `context_rows`
    are neighbouring finalized/current rows used solely to locate the duty that
    ends before / starts after a block, so a block at the very start of a period
    is still verified against the previous roster instead of being flagged
    "cannot verify". Returns a list of (severity, message) findings."""
    findings = []
    day_status = _day_status_map(rows)
    off_days = sorted(d for d, s in day_status.items() if "off" in s)
    if not off_days:
        return findings

    duty_ivs = []
    for r in list(rows) + list(context_rows or []):
        if r["Type"] not in ("FLIGHT", "STANDBY", "LAYOVER", "DUTY"):
            continue
        s = r.get("CIdt") or r.get("DEPdt")
        e = r.get("COdt") or r.get("ARRdt")
        if isinstance(s, datetime) and isinstance(e, datetime):
            if e <= s:
                e = s + timedelta(minutes=1)
            duty_ivs.append((s, e))
    duty_ivs.sort(key=lambda x: x[0])

    blocks, b0, b1 = [], off_days[0], off_days[0]
    for o in off_days[1:]:
        if o == b1 + timedelta(days=1):
            b1 = o
        else:
            blocks.append((b0, b1))
            b0 = b1 = o
    blocks.append((b0, b1))

    for b0, b1 in blocks:
        n = (b1 - b0).days + 1
        block_start = datetime.combine(b0, dtime(0, 0))
        block_end = datetime.combine(b1 + timedelta(days=1), dtime(0, 0))
        next_start = None
        for s, _e in duty_ivs:
            if s >= block_start:
                next_start = s
                break
        free_end = next_start if next_start is not None else block_end
        prev_end = max((e for _s, e in duty_ivs if e <= free_end), default=None)
        free_start = prev_end if prev_end is not None else block_start
        edge = []
        if prev_end is None:
            edge.append("previous duty not in this roster")
        if next_start is None:
            edge.append("next duty not in this roster")
        hours = (free_end - free_start).total_seconds() / 3600
        nights = _count_local_nights(free_start, free_end)
        need_nights = n + 1
        lbl = b0.strftime("%d %b") if n == 1 else f"{b0.strftime('%d %b')}\u2013{b1.strftime('%d %b')}"
        problems = []
        if hours < 34:
            problems.append(f"only {hours:.1f}h free of duty (min 34h)")
        if nights < need_nights:
            problems.append(f"{nights} local night(s) vs {need_nights} required for {n} day(s) off")
        if problems:
            # A block that only fails because the previous/next duty lies outside
            # this roster can't be verified, so it's a note rather than a breach.
            sev = "note" if edge else "violation"
            msg = f"Day off {lbl}: {'; '.join(problems)}."
            if edge:
                msg += f" (\u26a0\ufe0f {'; '.join(edge)} \u2014 cannot fully verify from this roster)."
            findings.append((sev, msg))
        elif edge:
            findings.append(("note",
                f"Day off {lbl}: {hours:.1f}h free, {nights} local night(s) \u2014 OK within this roster "
                f"({'; '.join(edge)})."))
    return findings


def tof_conflict_check(rows):
    """TOF = time-off block (not a day off). User rule: no duty may check in or
    check out within a TOF window. Returns [(severity, message), ...]."""
    findings = []
    windows = []
    for r in rows:
        if r["Type"] != "TIMEOFF":
            continue
        s, e = r.get("DEPdt"), r.get("ARRdt")
        if isinstance(s, datetime) and isinstance(e, datetime):
            if e <= s:
                e = s + timedelta(minutes=1)
            windows.append((s, e))
    if not windows:
        return findings
    for r in rows:
        if r["Type"] != "FLIGHT":
            continue
        ci, co = r.get("CIdt"), r.get("COdt")
        flt = (r.get("Flight / Code") or "").replace(" ", "")
        for ws, we in windows:
            hits = []
            if isinstance(ci, datetime) and ws <= ci <= we:
                hits.append(f"check-in {ci:%H:%M}")
            if isinstance(co, datetime) and ws <= co <= we:
                hits.append(f"check-out {co:%H:%M}")
            if hits:
                findings.append(("violation",
                    f"Time-off block {ws:%d %b %H:%M}\u2013{we:%H:%M}: {flt} "
                    f"{' and '.join(hits)} falls inside the TOF window \u2014 "
                    "no duty may check in or check out during TOF."))
    return findings


def fdp_roster_audit(rows, context_rows=None):
    """Chapter 08 FTL checks across the whole parsed roster: early/late/night
    classification, the 0100\u20130659 run limits, and cumulative duty hours
    (60/105/210). `context_rows` are neighbouring finalized/current rows passed
    through to the day-off rest check so a period's edge blocks can still be
    verified against the previous/next roster. Returns {counts, cumulative,
    findings, days_off}."""
    duties = build_duties(rows)
    for du in duties:
        start, end = du["report"], du["chocks_on"]
        if not isinstance(start, datetime) or not isinstance(end, datetime) or end <= start:
            end = (start if isinstance(start, datetime) else datetime.now()) + timedelta(minutes=1)
        du["start"], du["end"] = start, end

    counts = {"early": 0, "late": 0, "night": 0}
    for du in duties:
        c = duty_classify(du["start"], du["end"])
        du["early"], du["late"], du["night"] = c["early"], c["late"], c["night"]
        if c["early"]:
            counts["early"] += 1
        if c["late"]:
            counts["late"] += 1
        if c["night"]:
            counts["night"] += 1

    findings = []

    # --- 0100\u20130659 touch limits: \u2264 3 consecutive, \u2264 4 in 7 days ---
    touching = [du for du in duties if _spans_window(du["start"], du["end"], dtime(1, 0), dtime(6, 59))]
    for run in _group_runs(touching, lambda d: True):
        if len(run) >= 4:
            findings.append(("violation",
                f"{len(run)} consecutive duties touch 0100\u20130659 (limit is 3) \u2014 "
                + " \u2192 ".join(d["label"] for d in run) + "."))
    for du in touching:
        lo = du["start"] - timedelta(days=6)
        cnt = sum(1 for t in touching if lo <= t["start"] <= du["start"])
        if cnt > 4:
            findings.append(("violation",
                f"{cnt} duties touch 0100\u20130659 within 7 days ending {du['start']:%d %b} (limit is 4)."))
            break

    # --- regular early-morning series (runs of 4\u20135, max 5, duty \u2264 9 h) ---
    for run in _group_runs(duties, lambda d: d["early"]):
        if len(run) >= 4:
            over = [d for d in run if (d["end"] - d["start"]).total_seconds() > 9 * 3600]
            sev, msg = "note", (f"{len(run)} consecutive early starts = a regular early-morning series "
                                "\u2014 each duty \u2264 9 h, \u2265 24 h rest before the series, \u2265 63 h free after.")
            if len(run) > 5:
                sev, msg = "violation", f"{len(run)} consecutive early starts exceed the 5-duty cap."
            elif over:
                sev, msg = "violation", ("Regular early-morning series: " + ", ".join(d["label"] for d in over)
                                         + " exceed(s) the 9 h duty cap.")
            findings.append((sev, msg + "  (" + " \u2192 ".join(d["label"] for d in run) + ")"))
    # --- regular night-duty series (runs of 4\u20135, max 5, duty \u2264 8 h) ---
    for run in _group_runs(duties, lambda d: d["night"]):
        if len(run) >= 4:
            over = [d for d in run if (d["end"] - d["start"]).total_seconds() > 8 * 3600]
            sev, msg = "note", (f"{len(run)} consecutive night duties = a regular night-duty series "
                                "\u2014 each duty \u2264 8 h, \u2265 24 h rest before, \u2265 54 h free after.")
            if len(run) > 5:
                sev, msg = "violation", f"{len(run)} consecutive night duties exceed the 5-duty cap."
            elif over:
                sev, msg = "violation", ("Regular night-duty series: " + ", ".join(d["label"] for d in over)
                                         + " exceed(s) the 8 h duty cap.")
            findings.append((sev, msg + "  (" + " \u2192 ".join(d["label"] for d in run) + ")"))
    # --- free by 21:00 before a night-duty block (runs of 2\u20133 nights) ---
    for run in _group_runs(duties, lambda d: d["night"]):
        if len(run) < 2:
            continue
        prev = None
        for other in duties:
            if other["end"] <= run[0]["start"]:
                prev = other
        if prev and prev["end"].time() > dtime(21, 0) and (run[0]["start"] - prev["end"]).total_seconds() < 34 * 3600:
            findings.append(("note",
                f"Night-duty block starting {run[0]['label']} ({run[0]['start']:%d %b}): previous duty "
                f"{prev['label']} ends {prev['end']:%H:%M}, so the crew is not free by 21:00 before the block."))

    # --- cumulative duty hours (rolling 7/14/28-day windows, standby in full) ---
    periods = [{"label": du["label"], "start": du["start"], "end": du["end"],
                "minutes": int((du["end"] - du["start"]).total_seconds() // 60)} for du in duties]
    for r in rows:
        if r["Type"] in ("STANDBY", "DUTY") and r.get("CIdt"):
            s = r["CIdt"]
            e = r.get("COdt") or s
            if not isinstance(e, datetime) or e <= s:
                e = s + timedelta(minutes=1)
            periods.append({"label": (r.get("Code") or r["Type"]).strip(), "start": s, "end": e,
                            "minutes": int((e - s).total_seconds() // 60)})
    periods.sort(key=lambda p: p["start"])
    cumulative = {w: {"max": 0, "date": None} for w in ("7d", "14d", "28d")}
    if periods:
        anchors = sorted({p["start"].date() for p in periods})
        for wkey, days in (("7d", 7), ("14d", 14), ("28d", 28)):
            best, best_date = 0, None
            for anchor in anchors:
                lo = anchor - timedelta(days=days - 1)
                total = sum(p["minutes"] for p in periods if lo <= p["start"].date() <= anchor)
                if total > best:
                    best, best_date = total, anchor
            cumulative[wkey] = {"max": best, "date": best_date}

    for wkey, limit, cap_txt, soft in (("7d", CUMULATIVE_LIMITS["7d"], "60 h (65 h with delays)", CUMULATIVE_7D_SOFT),
                                       ("14d", CUMULATIVE_LIMITS["14d"], "105 h", None),
                                       ("28d", CUMULATIVE_LIMITS["28d"], "210 h", None)):
        c = cumulative[wkey]
        if c["max"] > (soft or limit):
            findings.append(("violation",
                f"Cumulative: {_fmt_hm(c['max'])} in {wkey.replace('d', '')} days (ending {c['date']:%d %b}) exceeds the {cap_txt} cap."))
        elif soft and c["max"] > limit:
            findings.append(("note",
                f"Cumulative: {_fmt_hm(c['max'])} in 7 days (ending {c['date']:%d %b}) is over 60 h \u2014 "
                "only OK if caused by unforeseen delays (\u2264 65 h)."))

    # --- days-off rules (8.2.17): duty-day status per calendar date ---
    day_status = _day_status_map(rows)

    days_off = {"off_days": 0, "max_duty_run": 0}
    if day_status:
        alldays = sorted(day_status)
        lo, hi = alldays[0], alldays[-1]
        days_off["off_days"] = sum(1 for d in day_status if "off" in day_status[d])

        # (a) not more than 7 consecutive days on duty
        run_start = None
        run_len = 0
        reported = False
        d = lo
        while d <= hi:
            if "duty" in day_status.get(d, set()):
                if run_start is None:
                    run_start, reported = d, False
                run_len += 1
                days_off["max_duty_run"] = max(days_off["max_duty_run"], run_len)
                if run_len > 7 and not reported:
                    findings.append(("violation",
                        f"{run_len} consecutive days on duty from {run_start:%d %b} to {d:%d %b} \u2014 limit is 7 "
                        "(8.2.17.a: the 8th day must be a day off, or positioning to base followed by 2 days off)."))
                    reported = True
            else:
                run_start, run_len, reported = None, 0, False
            d += timedelta(days=1)

        # (b) 2 consecutive days off in any consecutive 14 days
        prev_viol = False
        d = lo
        while d + timedelta(days=13) <= hi:
            has_pair = any("off" in day_status.get(d + timedelta(days=k), set())
                           and "off" in day_status.get(d + timedelta(days=k + 1), set()) for k in range(13))
            if not has_pair:
                if not prev_viol:
                    findings.append(("violation",
                        f"No 2 consecutive days off in the 14 days from {d:%d %b} (8.2.17.b)."))
                prev_viol = True
            else:
                prev_viol = False
            d += timedelta(days=1)

        # (c) at least 7 days off in any consecutive 28 days
        prev_viol = False
        d = lo
        while d + timedelta(days=27) <= hi:
            off_count = sum(1 for k in range(28) if "off" in day_status.get(d + timedelta(days=k), set()))
            if off_count < 7:
                if not prev_viol:
                    findings.append(("violation",
                        f"Only {off_count} day(s) off in the 28 days from {d:%d %b} \u2014 minimum is 7 (8.2.17.c)."))
                prev_viol = True
            else:
                prev_viol = False
            d += timedelta(days=1)

    # --- each day off must satisfy the 34h / 2-local-night definition ---
    rest_findings = off_day_rest_check(rows, context_rows=context_rows)
    findings.extend(rest_findings)
    days_off["off_rest_bad"] = sum(1 for f in rest_findings if f[0] == "violation")
    days_off["off_rest_notes"] = sum(1 for f in rest_findings if f[0] == "note")

    # --- which off days are mandatory (required by 8.2.17 a/b/c). Annual leave
    # means the crew has already taken their rest, so the mandatory-off check is
    # not applicable (8.2.17 a/b/c quotas are met inside the leave block). ---
    has_annual = any(r["Type"] == "LEAVE" and r.get("Code") in ANNUAL_LEAVE_CODES
                     for r in rows)
    if has_annual:
        days_off["mandatory_annual"] = True
        mand = {}
    else:
        mand = mandatory_off_days(rows)
    days_off["mandatory_dates"] = sorted(mand)
    days_off["mandatory_count"] = len(mand)

    # --- TOF time-off blocks: no duty may check in/out within the window ---
    findings.extend(tof_conflict_check(rows))

    return {"counts": counts, "cumulative": cumulative, "days_off": days_off, "findings": findings}


def compute_cumulative_hours(rows):
    """Rolling 7/14/28-day cumulative duty hours (duty FDPs + standby & ground
    duty in full, same convention as the FDP audit). Returns
    {window: {max, date}}."""
    duties = build_duties(rows)
    for du in duties:
        start, end = du["report"], du["chocks_on"]
        if not isinstance(start, datetime) or not isinstance(end, datetime) or end <= start:
            end = (start if isinstance(start, datetime) else datetime.now()) + timedelta(minutes=1)
        du["start"], du["end"] = start, end
    periods = [{"label": du["label"], "start": du["start"], "end": du["end"],
                "minutes": int((du["end"] - du["start"]).total_seconds() // 60)} for du in duties]
    for r in rows:
        if r["Type"] in ("STANDBY", "DUTY") and r.get("CIdt"):
            s = r["CIdt"]
            e = r.get("COdt") or s
            if not isinstance(e, datetime) or e <= s:
                e = s + timedelta(minutes=1)
            periods.append({"label": (r.get("Code") or r["Type"]).strip(), "start": s, "end": e,
                            "minutes": int((e - s).total_seconds() // 60)})
    periods.sort(key=lambda p: p["start"])
    cumulative = {w: {"max": 0, "date": None} for w in ("7d", "14d", "28d")}
    if periods:
        anchors = sorted({p["start"].date() for p in periods})
        for wkey, days in (("7d", 7), ("14d", 14), ("28d", 28)):
            best, best_date = 0, None
            for anchor in anchors:
                lo = anchor - timedelta(days=days - 1)
                total = sum(p["minutes"] for p in periods if lo <= p["start"].date() <= anchor)
                if total > best:
                    best, best_date = total, anchor
            cumulative[wkey] = {"max": best, "date": best_date}
    return cumulative


def cross_period_cumulative(username, current_rows):
    """Rolling 60/105/210 cumulative duty hours computed across finalized past
    periods + the current roster — windows that straddle the 28-day period
    boundaries a single-period view would miss. Returns
    {cumulative, findings}."""
    merged = merged_history_rows(username, current_rows)
    empty = {w: {"max": 0, "date": None} for w in ("7d", "14d", "28d")}
    if not merged:
        return {"cumulative": empty, "findings": []}
    cumulative = compute_cumulative_hours(merged)
    findings = []
    for wkey, limit, cap_txt, soft in (("7d", CUMULATIVE_LIMITS["7d"], "60 h (65 h with delays)", CUMULATIVE_7D_SOFT),
                                       ("14d", CUMULATIVE_LIMITS["14d"], "105 h", None),
                                       ("28d", CUMULATIVE_LIMITS["28d"], "210 h", None)):
        c = cumulative[wkey]
        if c["max"] > (soft or limit):
            findings.append(f"Cross-period cumulative: {_fmt_hm(c['max'])} in {wkey.replace('d', '')} days "
                            f"(ending {c['date']:%d %b}) exceeds the {cap_txt} cap.")
        elif soft and c["max"] > limit:
            findings.append(f"Cross-period cumulative: {_fmt_hm(c['max'])} in 7 days (ending {c['date']:%d %b}) "
                            f"is over 60 h — only OK if caused by unforeseen delays (≤ 65 h).")
    return {"cumulative": cumulative, "findings": findings}


def _duty_chip(sectors):
    """One compact chip per duty: a turnaround collapses to 'UL404/5' with one
    line per sector — route + dep–arr times only (flying time, airport names and
    timezones live in the Flight Intel panel). Midnight crossings get a tiny
    '▸ starts / ↳ lands' marker on the adjacent day instead."""
    title = _compact_flight_label([s["flight"].replace(" ", "") for s in sectors])
    lines = []
    for s in sectors:
        dep_t = s["dep"].strftime("%H:%M") if isinstance(s["dep"], datetime) else ""
        arr_t = s["arr"].strftime("%H:%M") if isinstance(s["arr"], datetime) else ""
        lines.append(f"<span>{s['o']}→{s['d']} {dep_t}–{arr_t}</span>")
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


def _current_only_note(label):
    """Muted placeholder shown in place of a current-roster panel's details when
    the calendar is browsing a PAST (archived) period — crew shouldn't mistake
    current-roster data for the archived period on screen."""
    return (f"<div class='card' style='border-color:#607d8b;color:#9fb3c8;font-size:12.5px;'>"
            f"ℹ️ <b>{label}</b> reflects the <b>current</b> roster only — switch the calendar "
            f"back to the current period to view it.</div>")


def _off_chip(mand_rules, code="OFF"):
    """DAY OFF chip — the portal code is kept (OFF/ROF/HOT/OVO); mandatory days
    (required by 8.2.17) are highlighted red."""
    lbl = code if code in ("OFF", "ROF", "HOT", "OVO") else "OFF"
    if mand_rules:
        return (f"<div class='chip chip-off-mand' title='Mandatory — {', '.join(mand_rules)}'>"
                f"🔴 {lbl} · MAND</div>")
    return f"<div class='chip chip-off'>🟢 {lbl}</div>"


def _duty_sectors_html(du, rows):
    """The per-sector route/times/flying-time rows for one duty (shared by the
    flight-intel card and the merged layover card)."""
    ac_by = {}
    for r in rows:
        if r["Type"] == "FLIGHT":
            ac_by[str(r["Flight / Code"]).replace(" ", "")] = r.get("Aircraft") or "-"

    sec_rows = []
    for s in du["sectors"]:
        fno = s["flight"].replace(" ", "")
        dep_t = s["dep"].strftime("%H:%M") if isinstance(s.get("dep"), datetime) else "-"
        arr_t = s["arr"].strftime("%H:%M") if isinstance(s.get("arr"), datetime) else "-"
        block = _fmt_hm(int(round(s["block_h"] * 60))) if s.get("block_h") else "-"
        sec_rows.append((fno, s["o"], s["d"], dep_t, arr_t, block, ac_by.get(fno, "-")))

    rows_html = ""
    for fno, o, d, dep_t, arr_t, block, ac in sec_rows:
        on = AIRPORT_NAME.get(o, "")
        dn = AIRPORT_NAME.get(d, "")
        ac_txt = f" · A/C {ac}" if ac not in ("-", "") else ""
        rows_html += (
            f"<div style='border:1px solid #1f2b3a;border-radius:8px;padding:8px 10px;margin-bottom:8px;'>"
            f"<div style='display:flex;justify-content:space-between;font-size:12.5px;'>"
            f"<b style='color:#4dd0e1;'>{fno}</b>"
            f"<span style='color:#9fb3c8;'>{o}→{d} · {dep_t}–{arr_t}</span>"
            f"<span style='color:#e8eef7;font-weight:600;'>{block}</span></div>"
            f"<div class='muted' style='margin-top:3px;'>{on} ({o}, {_gmt_str(AIRPORT_OFFSET_H.get(o, 5.5))}) → {dn} ({d}, {_gmt_str(AIRPORT_OFFSET_H.get(d, 5.5))}){ac_txt}</div></div>"
        )
    return rows_html


def layover_flight_duties(rows, station, lv_date, duties=None):
    """Inbound duty (last sector arrives at `station` on the layover start date)
    and outbound duty (departs `station` after the layover) — merged into the
    layover card so you don't have to click the flights separately."""
    if duties is None:
        duties = build_duties(rows)
    inbound = outbound = None
    for du in duties:
        if (du["dest"] == station and isinstance(du["chocks_on"], datetime)
                and du["chocks_on"].date() == lv_date):
            inbound = du
    for du in duties:
        if (du["origin"] == station and isinstance(du["report"], datetime)
                and du["report"].date() > lv_date):
            outbound = du
            break
    return inbound, outbound


def flight_intel_card(du, rows):
    """HTML card for one duty: per-sector route + times + flying time, aircraft,
    airport names and timezones, check-in/on-chock, and the duty's FDP vs its
    Table A/B maximum. (Flying time/airport detail lives here, not on the
    calendar chip.)"""
    sectors = du["sectors"]
    first = sectors[0]
    report, chocks = du.get("report"), du.get("chocks_on")

    n = len(sectors)
    band = fdp_band((first["dep"] - timedelta(hours=1)).time()) if isinstance(first.get("dep"), datetime) else "0600-0759"
    acclim = _roster_acclimatized(rows, report, away_origin=du.get("origin"))
    prec = _preceding_rest_h(rows, report) if not acclim else None
    max_fdp = fdp_limit_min(acclim, band, n, prec)
    fdp_actual = (int((chocks - report).total_seconds() // 60)
                  if isinstance(report, datetime) and isinstance(chocks, datetime) and chocks > report else None)

    d0 = first["dep"].date().strftime("%d %b") if isinstance(first.get("dep"), datetime) else ""
    rows_html = _duty_sectors_html(du, rows)
    fdp_html = ""
    if fdp_actual is not None:
        fdp_html = (f"<div class='bidrow'><span>Duty FDP (check-in → on-chock)</span>"
                    f"<span>{_fmt_hm(fdp_actual)} / max {_fmt_hm(max_fdp)} · Table {'A' if acclim else 'B'}</span></div>")
    head = f"✈ Flight Intel: {du['label']}" + (f" — {d0}" if d0 else "")
    sub = (f"<div class='muted' style='margin-bottom:8px;'>Check-in {report:%H:%M} · On-chock {chocks:%H:%M} · {n} sector(s)</div>"
           if isinstance(report, datetime) and isinstance(chocks, datetime) else "")
    return f"<div class='card' style='border-color:#00bcd4;'><h5>{head}</h5>{sub}{rows_html}{fdp_html}</div>"


def _day_chips_map(rows):
    """date → [chip HTML] for every calendar day a duty touches, plus (rmin, rmax).
    Ground blocks map onto every day they cover (multi-day HTL / SB / OFF);
    flights are grouped into DUTIES so a turnaround shows as ONE chip with
    both legs (e.g. 'UL404/5') instead of only the return leg."""
    mand_map = mandatory_off_days(rows)
    rmap = {}
    for r in rows:
        if not r["DateObj"] or r["Type"] == "FLIGHT":
            continue
        if r["Type"] == "LAYOVER":
            stn = r["Route"] if r["Route"] != "-" else "Layover"
            chip = f"<div class='chip chip-lay'>🏨 {stn}</div>"
        elif r["Type"] == "DAY OFF":
            chip = ("off", r.get("Code") or "OFF")   # per-day; keeps the OFF/ROF code
        elif r["Type"] == "STANDBY":
            code = r.get("Code") or "SB"
            s0 = r.get("CIdt") or r.get("DEPdt")
            s1 = r.get("COdt") or r.get("ARRdt")
            t = ""
            if isinstance(s0, datetime) and isinstance(s1, datetime):
                t = f"{s0.strftime('%H:%M')}–{s1.strftime('%H:%M')}"
                if s1.date() != s0.date():
                    t = f"{s0.day} {s0.strftime('%H:%M')}–{s1.day} {s1.strftime('%H:%M')}"
            chip = (f"<div class='chip chip-sby' title='{ground_code_label(code)}'>"
                    f"⏱ <b>{code}</b>{(' · ' + t) if t else ''}</div>")
        elif r["Type"] == "LEAVE":
            _code = r.get("Code") or "LEAVE"
            _bucket = ground_code_bucket(_code) or "leave"
            _lbl = ground_code_label(_code)
            if _bucket == "sick":
                chip = f"<div class='chip chip-lay' title='{_lbl}'>🤒 {_code}</div>"
            elif _bucket == "neutral":
                if _code == "CNL":
                    _em = "🚫"
                elif _code in ("NTS/QRN", "QRC", "QRW", "PCO", "PCR/PCL", "YFV"):
                    _em = "😷"
                elif _code == "CHO":
                    _em = "🎉"
                elif _code in ("HOT", "OVO"):
                    _em = "🏖"
                else:
                    _em = "▪"
                chip = f"<div class='chip chip-lay' title='{_lbl}'>{_em} {_code}</div>"
            else:
                chip = f"<div class='chip chip-lay' title='{_lbl}'>🌴 {_code}</div>"
        elif r["Type"] == "TIMEOFF":
            s0 = r.get("DEPdt") or r.get("CIdt")
            s1 = r.get("ARRdt") or r.get("COdt")
            t = ""
            if isinstance(s0, datetime) and isinstance(s1, datetime):
                t = f" {s0.strftime('%H:%M')}–{s1.strftime('%H:%M')}"
            _tcode = r.get("Code") or "TOF"
            chip = f"<div class='chip chip-tof' title='{ground_code_label(_tcode)}'>🕓 {_tcode}{t}</div>"
        elif r["Type"] == "DUTY":
            _dcode = r.get('Code') or 'Duty'
            chip = f"<div class='chip chip-duty' title='{ground_code_label(_dcode)}'>📚 {_dcode}</div>"
        else:
            continue
        d0 = r["DateObj"].date()
        d1 = r["EndDateObj"].date() if r.get("EndDateObj") else d0
        d = d0
        while d <= d1:
            if isinstance(chip, tuple) and chip[0] == "off":
                rmap.setdefault(d, []).append(_off_chip(mand_map.get(d), chip[1]))
            else:
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
    valid = [r for r in rows if r["DateObj"] is not None]
    rmin = min(r["DateObj"] for r in valid).date()
    rmax = max((r["EndDateObj"] or r["DateObj"]) for r in valid).date()
    return rmap, rmin, rmax


def build_calendar_html(rows, span=None):
    valid = [r for r in rows if r["DateObj"] is not None]
    if not valid:
        return "<div style='color:#7e8ba0;font-size:14px;'>No dated duties parsed.</div>"
    # Chips per day come from _day_chips_map (shared with the edit-mode grid).
    rmap, rmin, rmax = _day_chips_map(rows)
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
    legend = (
        "<div style='margin-top:8px;font-size:11px;color:#7e8ba0;'>"
        "<span class='chip chip-off-mand' style='display:inline-block;margin:0 4px 0 0;'>🔴 OFF · MAND</span>"
        "= required by §8.2.17 (7-day duty cap / 2-off-in-14 / 7-off-in-28) &nbsp;·&nbsp; "
        "<span class='chip chip-off' style='display:inline-block;margin:0 4px 0 0;'>🟢 OFF</span>"
        "= discretionary &nbsp;·&nbsp; "
        "<span class='chip chip-off' style='display:inline-block;margin:0 4px 0 0;'>🟢 ROF</span>"
        "= rest-off &nbsp;·&nbsp; "
        "<span class='chip chip-off' style='display:inline-block;margin:0 4px 0 0;'>🟢 HOT/OVO</span>"
        "= outstation/overseas off (counts as a day off) &nbsp;·&nbsp; "
        "<span class='chip chip-lay' style='display:inline-block;margin:0 4px 0 0;'>🌴/🤒/😷 code</span>"
        "= leave / sick (sick counts as a day off) / other ground codes &nbsp;·&nbsp; "
        "<span class='chip chip-duty' style='display:inline-block;margin:0 4px 0 0;'>📚 code</span>"
        "= training / ground duty &nbsp;·&nbsp; "
        "<span class='chip chip-tof' style='display:inline-block;margin:0 4px 0 0;'>🕓 TOF</span>"
        "= time off (no check-in/check-out allowed in the window)</div>")
    return "".join(cells) + legend


# --- CALENDAR EDITING (current roster only) ---
# The live roster is stored as text; edits operate on parsed rows, which are
# re-serialized (round-trips losslessly — see _rows_to_roster_text) and saved
# back. Editing is gated to the CURRENT 28-day period only.

_ADD_OFF_CODES = ["OFF", "ROF", "HOT", "OVO"]
_ADD_LEAVE_CODES = ["ALV", "RLV", "ALP", "CLV", "SPL", "MTL", "S/L", "FSL", "LMS"]
_ADD_TOF_CODES = ["TOF", "HTO"]
_ADD_DUTY_CODES = ["GND", "MTG", "FAU", "OFG", "DLV", "ADM", "MED", "CRM", "SEP", "SEC", "DGR", "CBT"]
_ADD_SBY_CODES = ["SB1", "SB2", "SB3", "SB4", "ASB", "LSB", "SSY"]


# Standby windows (start, end) — SB4 runs overnight into the next morning.
_SBY_WINDOWS = {
    "SB1": ("00:01", "11:59"),
    "SB2": ("06:00", "17:59"),
    "SB3": ("12:00", "23:59"),
    "SB4": ("18:00", "05:59"),
}

# Layover trips: outbound flight -> return flight, nights away (counted from
# the day the outbound ARRIVES), and optional per-weekday overrides (0=Mon..
# 6=Sun) for routes where the layover length depends on the departure day.
# Nights derived from the SriLankan timetable (flightsfrom.com, Sep 2026) and
# the crew's own pasted rosters; schedules shift seasonally and the number of
# nights is always editable in the add-flight form (a pasted roster's actual
# hotel block always wins when present).
LAYOVER_TRIPS = {
    "UL253": {"ret": "UL254", "nights": 1, "dow": {}},          # Dammam — daily, 1 night
    "UL265": {"ret": "UL266", "nights": 1, "dow": {}},          # Riyadh — 1 night
    "UL470": {"ret": "UL471", "nights": 2, "dow": {1: 5}},      # Seoul — Sun=2, Tue=5 nights
    "UL503": {"ret": "UL504", "nights": 1, "dow": {}},          # London — daily; 1 night (winter up to 2)
    "UL563": {"ret": "UL564", "nights": 1, "dow": {}},          # Paris — 1 night
    "UL557": {"ret": "UL558", "nights": 1, "dow": {}},          # Frankfurt — 1 night (resumes winter)
    "UL604": {"ret": "UL605", "nights": 1, "dow": {}},          # Melbourne — daily, 1 night
    "UL606": {"ret": "UL607", "nights": 1, "dow": {}},          # Sydney — 1 night (varies — use the stepper)
    "UL880": {"ret": "UL885", "nights": 3, "dow": {}},          # Guangzhou (Tue) — 3 nights
    "UL884": {"ret": "UL881", "nights": 1, "dow": {3: 5, 5: 3}},  # Guangzhou Mon/Thu/Sat — 1/5/3 nights
}


def _normalize_flt(s):
    """'404' or 'ul404 ' -> 'UL404'; anything else passed through uppercased."""
    s = (s or '').replace(' ', '').upper()
    if re.fullmatch(r'\d{1,4}', s):
        return 'UL' + s
    if re.fullmatch(r'UL\d{1,4}', s):
        return s
    return s


def _hm_shift(hhmm, delta_min):
    """Shift an HH:MM string by delta minutes (negative = earlier); '' if invalid."""
    t = _parse_hm(hhmm)
    if t is None:
        return ''
    return (datetime(2000, 1, 1) + timedelta(hours=t.hour, minutes=t.minute)
            + timedelta(minutes=delta_min)).strftime('%H:%M')


def _lay_nights(trip, day):
    """Nights for a layover trip, honouring any per-weekday override."""
    return trip.get('dow', {}).get(day.weekday(), trip['nights'])


def _row_coverage(r):
    """(start, end) dates a row occupies on the calendar, or None."""
    s = r.get('CIdt') or r.get('DEPdt')
    e = r.get('COdt') or r.get('ARRdt')
    if isinstance(s, datetime) and isinstance(e, datetime):
        return s.date(), e.date()
    if isinstance(r.get('DateObj'), datetime):
        d0 = r['DateObj'].date()
        d1 = (r['EndDateObj'].date() if isinstance(r.get('EndDateObj'), datetime) else d0)
        return d0, d1
    return None


def _remove_days(rows, lo, hi):
    """Split rows into (kept, removed) — removed = any row touching [lo, hi]."""
    kept, removed = [], []
    for r in rows:
        cov = _row_coverage(r)
        if cov and not (cov[1] < lo or cov[0] > hi):
            removed.append(r)
        else:
            kept.append(r)
    return kept, removed


def _time_span(r):
    """(start, end) datetimes a row actually occupies, or None. Uses the same
    fields as _row_coverage but keeps the times, so overlap checks can tell a
    05:59 standby handover from an 08:00 duty apart."""
    s = r.get('CIdt') or r.get('DEPdt')
    e = r.get('COdt') or r.get('ARRdt')
    if isinstance(s, datetime) and isinstance(e, datetime):
        return s, e
    d0 = r.get('DateObj')
    if isinstance(d0, datetime):
        d1 = r.get('EndDateObj')
        if isinstance(d1, datetime) and d1.date() > d0.date():
            return d0, d1
        return d0, d0 + timedelta(days=1)
    return None


def _remove_conflicts(rows, new_row):
    """Split rows into (kept, removed) — removed = any row whose time span
    overlaps new_row's. Time-aware, so an overnight SB4 (18:00→05:59) clears a
    full-day OFF or an early-morning duty on the next day, but leaves an
    after-06:00 duty untouched."""
    ns = _time_span(new_row)
    if not ns:
        return list(rows), []
    kept, removed = [], []
    for r in rows:
        rs = _time_span(r)
        if rs and not (rs[1] <= ns[0] or rs[0] >= ns[1]):
            removed.append(r)
        else:
            kept.append(r)
    return kept, removed


def _cedit_flt_on_change():
    """Light callback: flag the main body to autofill (st.success is unsafe in callbacks)."""
    st.session_state['_autofill_flt'] = True


def _sby_code_changed():
    """Prefill standby times when the code changes."""
    code = st.session_state.get('cedit_sb_code', '')
    w = _SBY_WINDOWS.get(code)
    if w:
        st.session_state['cedit_sb_s0'] = w[0]
        st.session_state['cedit_sb_s1'] = w[1]


# SriLankan Airlines DIRECT flights from Colombo — autofill reference database.
# Keyed by flight number. Times are typical SCHEDULED LOCAL times at each end
# (dep = origin local, arr = destination local), matching how the portal logs
# them; they are a convenience prefill and always editable. 'ret' = the paired
# return flight (None on return legs), 'ret_day' = days after the outbound that
# the return departs (0 = same-day turnaround, 1 = next-day).
UL_DIRECT = {
    # Times are TYPICAL scheduled local times (summer 2026 season), refreshed
    # from live flight trackers 2026-09. Schedules shift seasonally, and the
    # app ALWAYS prefers your own pasted roster's times when it has them — see
    # _fill_flight_details. 'ret' = paired return, 'ret_day' = days after the
    # outbound that the return departs (0 = same-day turnaround, 1 = next day).
    # --- Bangkok (BKK) ---
    "UL402": {"o": "CMB", "d": "BKK", "dep": "01:10", "arr": "06:15", "ac": "32B", "ret": "UL403", "ret_day": 0},
    "UL403": {"o": "BKK", "d": "CMB", "dep": "07:30", "arr": "09:05", "ac": "32B"},
    "UL404": {"o": "CMB", "d": "BKK", "dep": "07:35", "arr": "12:45", "ac": "32B", "ret": "UL405", "ret_day": 0},
    "UL405": {"o": "BKK", "d": "CMB", "dep": "13:55", "arr": "15:45", "co": "16:15", "ac": "32B"},
    # --- Maldives (MLE) ---
    "UL101": {"o": "CMB", "d": "MLE", "dep": "07:20", "arr": "08:15", "ac": "333", "ret": "UL102", "ret_day": 0},
    "UL102": {"o": "MLE", "d": "CMB", "dep": "09:15", "arr": "10:15", "ac": "333"},
    "UL115": {"o": "CMB", "d": "MLE", "dep": "13:30", "arr": "14:25", "ac": "320", "ret": "UL116", "ret_day": 0},
    "UL116": {"o": "MLE", "d": "CMB", "dep": "15:25", "arr": "16:25", "ac": "320"},
    "UL103": {"o": "CMB", "d": "MLE", "dep": "18:55", "arr": "19:50", "ac": "320", "ret": "UL104", "ret_day": 0},
    "UL104": {"o": "MLE", "d": "CMB", "dep": "20:50", "arr": "21:50", "ac": "320"},
    # --- India: Chennai (MAA) ---
    "UL121": {"o": "CMB", "d": "MAA", "dep": "07:25", "arr": "08:50", "ac": "333", "ret": "UL122", "ret_day": 0},
    "UL122": {"o": "MAA", "d": "CMB", "dep": "10:50", "arr": "12:15", "ac": "333"},
    "UL123": {"o": "CMB", "d": "MAA", "dep": "18:45", "arr": "20:10", "ac": "320", "ret": "UL124", "ret_day": 0},
    "UL124": {"o": "MAA", "d": "CMB", "dep": "22:10", "arr": "23:35", "ac": "320"},
    "UL125": {"o": "CMB", "d": "MAA", "dep": "00:45", "arr": "02:10", "ac": "320", "ret": "UL126", "ret_day": 0},
    "UL126": {"o": "MAA", "d": "CMB", "dep": "03:10", "arr": "04:35", "ac": "320"},
    "UL127": {"o": "CMB", "d": "MAA", "dep": "13:40", "arr": "15:05", "ac": "320", "ret": "UL128", "ret_day": 0},
    "UL128": {"o": "MAA", "d": "CMB", "dep": "16:55", "arr": "18:20", "ac": "320"},
    # --- India: Mumbai (BOM) ---
    "UL141": {"o": "CMB", "d": "BOM", "dep": "23:40", "arr": "02:10", "ac": "320", "ret": "UL142", "ret_day": 1},
    "UL142": {"o": "BOM", "d": "CMB", "dep": "03:10", "arr": "05:40", "ac": "320"},
    "UL143": {"o": "CMB", "d": "BOM", "dep": "17:20", "arr": "19:50", "ac": "320", "ret": "UL144", "ret_day": 0},
    "UL144": {"o": "BOM", "d": "CMB", "dep": "20:40", "arr": "23:10", "ac": "320"},
    # --- India: New Delhi (DEL) ---
    "UL191": {"o": "CMB", "d": "DEL", "dep": "00:40", "arr": "04:15", "ac": "320", "ret": "UL192", "ret_day": 0},
    "UL192": {"o": "DEL", "d": "CMB", "dep": "05:15", "arr": "08:50", "co": "09:20", "ac": "320"},
    "UL195": {"o": "CMB", "d": "DEL", "dep": "14:10", "arr": "17:45", "ac": "320", "ret": "UL196", "ret_day": 0},
    "UL196": {"o": "DEL", "d": "CMB", "dep": "18:45", "arr": "22:20", "ac": "320"},
    # --- India: Bengaluru (BLR) ---
    "UL171": {"o": "CMB", "d": "BLR", "dep": "18:55", "arr": "20:20", "ac": "320", "ret": "UL172", "ret_day": 0},
    "UL172": {"o": "BLR", "d": "CMB", "dep": "21:20", "arr": "22:45", "co": "23:15", "ac": "320"},
    # --- India: Tiruchirappalli (TRZ) ---
    "UL131": {"o": "CMB", "d": "TRZ", "dep": "08:15", "arr": "09:15", "ac": "320", "ret": "UL132", "ret_day": 0},
    "UL132": {"o": "TRZ", "d": "CMB", "dep": "10:15", "arr": "11:15", "ac": "320"},
    "UL133": {"o": "CMB", "d": "TRZ", "dep": "13:40", "arr": "14:40", "ac": "320", "ret": "UL134", "ret_day": 0},
    "UL134": {"o": "TRZ", "d": "CMB", "dep": "15:40", "arr": "16:40", "ac": "320"},
    # --- Middle East ---
    "UL253": {"o": "CMB", "d": "DMM", "dep": "18:25", "arr": "21:00", "co": "21:30", "ac": "32B", "ret": "UL254", "ret_day": 0},
    "UL254": {"o": "DMM", "d": "CMB", "dep": "22:15", "arr": "05:55", "co": "06:25", "ac": "32B"},
    "UL265": {"o": "CMB", "d": "RUH", "dep": "18:15", "arr": "21:20", "co": "21:50", "ac": "332", "ret": "UL266", "ret_day": 0},
    "UL266": {"o": "RUH", "d": "CMB", "dep": "22:30", "arr": "06:20", "co": "06:50", "ac": "333"},
    "UL225": {"o": "CMB", "d": "DXB", "dep": "18:30", "arr": "21:40", "ac": "332", "ret": "UL226", "ret_day": 0},
    "UL226": {"o": "DXB", "d": "CMB", "dep": "23:00", "arr": "05:00", "ac": "332"},
    "UL229": {"o": "CMB", "d": "KWI", "dep": "18:15", "arr": "21:15", "ac": "32B", "ret": "UL230", "ret_day": 0},
    "UL230": {"o": "KWI", "d": "CMB", "dep": "22:15", "arr": "04:45", "ac": "32B"},
    "UL217": {"o": "CMB", "d": "DOH", "dep": "18:40", "arr": "21:15", "ac": "321", "ret": "UL218", "ret_day": 0},
    "UL218": {"o": "DOH", "d": "CMB", "dep": "22:15", "arr": "05:00", "ac": "321"},
    "UL215": {"o": "CMB", "d": "BAH", "dep": "18:50", "arr": "21:40", "ac": "320", "ret": "UL216", "ret_day": 0},
    "UL216": {"o": "BAH", "d": "CMB", "dep": "22:40", "arr": "05:20", "ac": "320"},
    "UL205": {"o": "CMB", "d": "MCT", "dep": "18:40", "arr": "21:40", "ac": "320", "ret": "UL206", "ret_day": 0},
    "UL206": {"o": "MCT", "d": "CMB", "dep": "22:40", "arr": "05:00", "ac": "320"},
    "UL281": {"o": "CMB", "d": "JED", "dep": "19:30", "arr": "23:10", "ac": "332", "ret": "UL282", "ret_day": 0},
    "UL282": {"o": "JED", "d": "CMB", "dep": "00:10", "arr": "08:10", "ac": "332"},
    # --- East Asia ---
    "UL470": {"o": "CMB", "d": "ICN", "dep": "19:50", "arr": "07:35", "co": "08:05", "ac": "332", "ret": "UL471", "ret_day": 1},
    "UL471": {"o": "ICN", "d": "CMB", "dep": "12:20", "arr": "17:00", "co": "17:30", "ac": "333"},
    "UL454": {"o": "CMB", "d": "NRT", "dep": "19:50", "arr": "08:10", "ac": "333", "ret": "UL455", "ret_day": 1},
    "UL455": {"o": "NRT", "d": "CMB", "dep": "11:30", "arr": "16:30", "ac": "333"},
    # --- China ---
    "UL891": {"o": "CMB", "d": "HKG", "dep": "18:10", "arr": "02:20", "ac": "332", "ret": "UL892", "ret_day": 1},
    "UL892": {"o": "HKG", "d": "CMB", "dep": "03:20", "arr": "05:50", "ac": "332"},
    "UL880": {"o": "CMB", "d": "CAN", "dep": "14:00", "arr": "22:30", "ac": "32N", "ret": "UL885", "ret_day": 3},
    "UL885": {"o": "CAN", "d": "CMB", "dep": "03:15", "arr": "06:05", "ac": "32A"},
    "UL884": {"o": "CMB", "d": "CAN", "dep": "17:35", "arr": "02:26", "ac": "333", "ret": "UL881", "ret_day": 1},
    "UL881": {"o": "CAN", "d": "CMB", "dep": "01:50", "arr": "04:35", "ac": "32A"},
    "UL866": {"o": "CMB", "d": "PVG", "dep": "14:15", "arr": "23:50", "ac": "332", "ret": "UL867", "ret_day": 1},
    "UL867": {"o": "PVG", "d": "CMB", "dep": "01:20", "arr": "05:40", "ac": "332"},
    "UL868": {"o": "CMB", "d": "PEK", "dep": "19:00", "arr": "05:10", "ac": "332", "ret": "UL869", "ret_day": 1},
    "UL869": {"o": "PEK", "d": "CMB", "dep": "06:10", "arr": "10:30", "ac": "332"},
    # --- Southeast Asia ---
    "UL306": {"o": "CMB", "d": "SIN", "dep": "01:50", "arr": "08:30", "ac": "320", "ret": "UL307", "ret_day": 0},
    "UL307": {"o": "SIN", "d": "CMB", "dep": "09:45", "arr": "11:15", "ac": "320"},
    "UL314": {"o": "CMB", "d": "KUL", "dep": "07:40", "arr": "13:55", "ac": "320", "ret": "UL315", "ret_day": 0},
    "UL315": {"o": "KUL", "d": "CMB", "dep": "15:00", "arr": "16:00", "ac": "320"},
    "UL318": {"o": "CMB", "d": "KUL", "dep": "19:35", "arr": "23:20", "ac": "320", "ret": "UL319", "ret_day": 1},
    "UL319": {"o": "KUL", "d": "CMB", "dep": "09:15", "arr": "10:45", "ac": "320"},
    "UL364": {"o": "CMB", "d": "CGK", "dep": "07:20", "arr": "13:30", "ac": "321", "ret": "UL365", "ret_day": 0},
    "UL365": {"o": "CGK", "d": "CMB", "dep": "14:25", "arr": "17:30", "ac": "320"},
    # --- South Asia ---
    "UL189": {"o": "CMB", "d": "DAC", "dep": "07:45", "arr": "11:30", "ac": "320", "ret": "UL190", "ret_day": 0},
    "UL190": {"o": "DAC", "d": "CMB", "dep": "12:55", "arr": "15:40", "ac": "332"},
    "UL181": {"o": "CMB", "d": "KTM", "dep": "07:30", "arr": "11:15", "ac": "320", "ret": "UL182", "ret_day": 0},
    "UL182": {"o": "KTM", "d": "CMB", "dep": "13:15", "arr": "17:05", "ac": "320"},
    "UL185": {"o": "CMB", "d": "LHE", "dep": "13:50", "arr": "17:15", "ac": "320", "ret": "UL186", "ret_day": 0},
    "UL186": {"o": "LHE", "d": "CMB", "dep": "18:25", "arr": "22:50", "co": "23:20", "ac": "320"},
    # --- Europe / Oceania ---
    "UL503": {"o": "CMB", "d": "LHR", "dep": "13:00", "arr": "19:45", "ac": "333", "ret": "UL504", "ret_day": 1},
    "UL504": {"o": "LHR", "d": "CMB", "dep": "21:30", "arr": "13:20", "ac": "333"},
    "UL563": {"o": "CMB", "d": "CDG", "dep": "00:05", "arr": "07:00", "ac": "333", "ret": "UL564", "ret_day": 1},
    "UL564": {"o": "CDG", "d": "CMB", "dep": "10:00", "arr": "23:30", "ac": "333"},
    "UL553": {"o": "CMB", "d": "FRA", "dep": "00:10", "arr": "07:05", "ac": "333", "ret": "UL554", "ret_day": 1},
    "UL554": {"o": "FRA", "d": "CMB", "dep": "09:00", "arr": "22:30", "ac": "333"},
    "UL557": {"o": "CMB", "d": "FRA", "dep": "00:40", "arr": "07:05", "ac": "333", "ret": "UL558", "ret_day": 1},
    "UL558": {"o": "FRA", "d": "CMB", "dep": "15:05", "arr": "05:34", "co": "06:04", "ac": "333"},
    "UL604": {"o": "CMB", "d": "MEL", "dep": "00:20", "arr": "14:40", "ac": "333", "ret": "UL605", "ret_day": 1},
    "UL605": {"o": "MEL", "d": "CMB", "dep": "16:10", "arr": "22:00", "ac": "333"},
    "UL606": {"o": "CMB", "d": "SYD", "dep": "00:05", "arr": "14:40", "ac": "333", "ret": "UL607", "ret_day": 1},
    "UL607": {"o": "SYD", "d": "CMB", "dep": "17:30", "arr": "23:30", "ac": "333"},
}



def _parse_hm(s):
    """'HH:MM' -> datetime.time; blank/invalid -> None."""
    if not isinstance(s, str):
        return None
    m = re.match(r'^\s*(\d{1,2}):(\d{2})\s*$', s)
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    if h > 23 or mi > 59:
        return None
    return dtime(h, mi)


def _at(day, t):
    """Combine a date with a time (or None)."""
    if t is None:
        return None
    return datetime.combine(day, t)


def _prefill(dt_):
    """datetime -> 'HH:MM' for a text field, else ''."""
    return dt_.strftime("%H:%M") if isinstance(dt_, datetime) else ""


def duty_rows_on_day(rows, day):
    """Indices of rows anchored to `day`: flights on their departure date, ground
    blocks on every date they cover (start..end)."""
    out = []
    for i, r in enumerate(rows):
        d0 = r.get("DateObj")
        if not isinstance(d0, datetime):
            continue
        d1 = r.get("EndDateObj") if isinstance(r.get("EndDateObj"), datetime) else d0
        if d0.date() <= day <= d1.date():
            out.append(i)
    return out


def build_duty_row(day, rtype, code, ci=None, dep=None, arr=None, co=None,
                   origin="CMB", dest="CMB", ac="-", end_day=None):
    """Construct a parsed-row dict for a new/edited duty anchored on `day`.
    Times are datetime.time objects (or None); `end_day` > day spans a ground
    block across multiple days. Overnight rollover follows the parser's rules."""
    ci_dt = _at(day, ci)
    dep_dt = _at(day, dep)
    arr_dt = _at(day, arr)
    co_dt = _at(day, co)

    if rtype in ("FLIGHT", "STANDBY", "DUTY"):
        if dep_dt and arr_dt and arr_dt < dep_dt:
            arr_dt += timedelta(days=1)      # overnight arrival
        if arr_dt and co_dt and co_dt < arr_dt:
            co_dt += timedelta(days=1)
        if ci_dt and dep_dt and ci_dt > dep_dt:
            ci_dt -= timedelta(days=1)       # check-in the evening before

    if rtype == "FLIGHT":
        dep_dt = dep_dt or datetime.combine(day, dtime(7, 0))
        arr_dt = arr_dt or (dep_dt + timedelta(hours=2))
        anchor = dep_dt
        route = f"{origin} ➔ {dest}"
        end_dt_obj = None
    elif rtype == "LAYOVER":
        dep_dt = dep_dt or datetime.combine(day, dtime(0, 0))
        if end_day and end_day > day:
            arr_dt = _at(end_day, arr) or datetime.combine(end_day, dtime(23, 59))
        arr_dt = arr_dt or dep_dt
        anchor = dep_dt
        route = origin
        ci_dt, co_dt = dep_dt, arr_dt
        end_dt_obj = (datetime.combine(arr_dt.date(), dtime.min)
                      if arr_dt.date() > dep_dt.date() else None)
    elif rtype in ("STANDBY", "DUTY"):
        dep_dt = dep_dt or datetime.combine(day, dtime(6, 0))
        ci_dt = ci_dt or dep_dt
        if end_day and end_day > day:
            arr_dt = co_dt = datetime.combine(end_day, dtime(23, 59))
        co_dt = co_dt or arr_dt or (dep_dt + timedelta(hours=12))
        arr_dt = arr_dt or co_dt
        anchor = dep_dt
        route = "-"
        end_dt_obj = (datetime.combine(co_dt.date(), dtime.min)
                      if co_dt.date() > dep_dt.date() else None)
    else:  # DAY OFF / LEAVE / TIMEOFF
        dep_dt = dep_dt or datetime.combine(day, dtime(0, 0))
        if end_day and end_day > day:
            arr_dt = datetime.combine(end_day, dtime(23, 59))
        arr_dt = arr_dt or datetime.combine(day, dtime(23, 59))
        ci_dt = co_dt = None
        anchor = dep_dt
        route = "-"
        end_dt_obj = (datetime.combine(arr_dt.date(), dtime.min)
                      if arr_dt.date() > dep_dt.date() else None)

    def hm(dt_):
        return dt_.strftime("%H:%M") if dt_ else "-"

    code_clean = re.sub(r"\s+", "", (code or "")).upper()
    if rtype == "FLIGHT":
        if not code_clean.startswith("UL"):
            code_clean = "UL" + code_clean
        fcode = f"UL {code_clean[2:]}"
    else:
        fcode = rtype

    return {
        "Date": anchor.strftime("%d%b%y").upper(),
        "DateObj": datetime.combine(anchor.date(), dtime.min),
        "EndDateObj": end_dt_obj,
        "Type": rtype,
        "Code": code_clean,
        "Flight / Code": fcode,
        "Check-In": hm(ci_dt) if rtype in ("FLIGHT", "STANDBY", "DUTY") else "-",
        "Departure": hm(dep_dt),
        "Route": route,
        "Arrival": hm(arr_dt),
        "Checkout": hm(co_dt) if rtype in ("FLIGHT", "STANDBY", "DUTY") else "-",
        "Aircraft": ac or "-",
        "CIdt": ci_dt, "DEPdt": dep_dt, "ARRdt": arr_dt, "COdt": co_dt,
        "CIdt_u": None, "DEPdt_u": None, "ARRdt_u": None, "COdt_u": None,
    }


def _sort_rows(rows):
    """Chronological order so the re-serialized roster stays consistent."""
    def key(r):
        t = r.get("DEPdt") or r.get("CIdt") or r.get("DateObj")
        if isinstance(t, datetime):
            return t
        return datetime(2099, 1, 1)
    return sorted(rows, key=key)


def save_rows_as_roster(username, rows):
    """Serialize parsed rows back to roster text, persist, return the text."""
    text = _rows_to_roster_text(_sort_rows(rows))
    save_roster_to_db(username, text)
    return text


def _row_summary(r):
    """One-line human summary of a duty row for the editor list."""
    t = r["Type"]
    code = r.get("Code") or ""
    if t == "FLIGHT":
        o, d = _route_od(r.get("Route"))
        s = f"✈ {code} {o}➔{d}"
        dep, arr = r.get("DEPdt"), r.get("ARRdt")
        if isinstance(dep, datetime):
            s += f" · dep {dep:%H:%M}"
        if isinstance(arr, datetime):
            s += f" – arr {arr:%H:%M}"
        return s
    if t == "STANDBY":
        s0 = r.get("CIdt") or r.get("DEPdt")
        s1 = r.get("COdt") or r.get("ARRdt")
        s = f"⏱ {code}"
        if isinstance(s0, datetime) and isinstance(s1, datetime):
            s += f" · {s0:%H:%M}–{s1:%H:%M}"
        return s
    if t == "LAYOVER":
        return f"🏨 {code} {r.get('Route') or ''}".strip()
    if t == "DAY OFF":
        return f"🟢 {code}"
    if t == "LEAVE":
        return f"🌴 {code}"
    if t == "TIMEOFF":
        s0, s1 = r.get("DEPdt"), r.get("ARRdt")
        s = f"🕓 {code}"
        if isinstance(s0, datetime) and isinstance(s1, datetime):
            s += f" · {s0:%H:%M}–{s1:%H:%M}"
        return s
    if t == "DUTY":
        return f"📚 {code}"
    return f"{t} {code}"


def _day_summary_labels(rows, day):
    """Compact one-line labels for the edit-mode calendar cell on `day`."""
    labels = []
    for r in rows:
        if r["Type"] == "FLIGHT" or not isinstance(r.get("DateObj"), datetime):
            continue
        d0 = r["DateObj"].date()
        d1 = (r["EndDateObj"].date() if isinstance(r.get("EndDateObj"), datetime) else d0)
        if not (d0 <= day <= d1):
            continue
        code = r.get("Code") or ""
        t = r["Type"]
        if t == "DAY OFF":
            labels.append("🟢 " + code)
        elif t == "LAYOVER":
            labels.append("🏨 " + (r["Route"] if r["Route"] != "-" else "Layover"))
        elif t == "STANDBY":
            labels.append("⏱ " + code)
        elif t == "LEAVE":
            labels.append("🌴 " + code)
        elif t == "TIMEOFF":
            labels.append("🕓 " + code)
        elif t == "DUTY":
            labels.append("📚 " + code)
    for grp in _calendar_duties(rows):
        first, last = grp[0], grp[-1]
        dep_dt = first.get("dep")
        if not isinstance(dep_dt, datetime):
            continue
        start = first.get("ci") or dep_dt
        end = last.get("co") or last.get("arr")
        if not isinstance(end, datetime):
            end = start
        if not (start.date() <= day <= end.date()):
            continue
        nums = "/".join(dict.fromkeys(s["flight"].replace(" ", "") for s in grp))
        if day == dep_dt.date():
            labels.append("✈ " + nums)
        elif day < dep_dt.date():
            labels.append("⟳ " + nums + " starts")
        else:
            labels.append("🛬 " + nums + " lands")
    return labels


def _cedit_open(day):
    """on_click: open the per-day editor."""
    st.session_state['caledit_day'] = day
    st.session_state['caledit_target'] = None


def _snapshot_for_undo():
    """Remember the pre-edit roster so an accidental change can be reverted."""
    st.session_state.setdefault('_undo_stack', [])
    st.session_state['_undo_stack'].append(st.session_state.get('current_roster', ''))
    if len(st.session_state['_undo_stack']) > 8:
        st.session_state['_undo_stack'] = st.session_state['_undo_stack'][-8:]


def _undo_last():
    """Revert the most recent calendar edit."""
    stack = st.session_state.get('_undo_stack', [])
    if not stack:
        st.warning("Nothing to undo — no calendar edits made this session.")
        return
    prev = stack.pop()
    st.session_state['current_roster'] = prev
    save_roster_to_db(st.session_state['username'], prev)
    st.session_state.pop('caledit_target', None)
    st.success("Reverted to the previous roster.")
    st.rerun()


def _roster_flight_times():
    """Actual times per flight number from the CURRENT roster — authoritative
    over the static database (your published roster is always right)."""
    out = {}
    text = st.session_state.get('current_roster', '')
    if not text:
        return out
    try:
        for r in parse_roster_text(text):
            code = r.get('Code')
            if r.get('Type') != 'FLIGHT' or not re.fullmatch(r'UL\d{1,4}', code or ''):
                continue
            o, d = _route_od(r.get('Route'))
            entry = {'o': o or 'CMB', 'd': d or ''}
            for k, src in (('dep', 'DEPdt'), ('arr', 'ARRdt'), ('co', 'COdt')):
                v = r.get(src)
                entry[k] = v.strftime('%H:%M') if isinstance(v, datetime) else ''
            ac = r.get('Aircraft')
            entry['ac'] = ac if ac not in (None, '-', '') else ''
            out.setdefault(code, entry)   # first occurrence = outbound leg
    except Exception:
        pass
    return out


def _flight_info(fl):
    """DB entry for `fl` with the current roster's actual times overlaid."""
    if not re.fullmatch(r'UL\d{1,4}', fl):
        return None
    info = UL_DIRECT.get(fl)
    r = _roster_flight_times().get(fl)
    if not info and not r:
        return None
    m = dict(info) if info else {}
    if r:
        for k in ('o', 'd', 'dep', 'arr', 'co', 'ac'):
            if r.get(k):
                m[k] = r[k]
    if not m:
        return None
    m.setdefault('o', 'CMB')
    m.setdefault('d', '')
    return m


def _fill_flight_details(day=None):
    """Autofill the add-flight form: times from the direct-flights DB (current
    roster's actual times win). Check-in = dep − 1h20; check-out = arr + 30m
    (turnarounds get NO check-in/check-out at the outstation). Layover trips
    also set the return date from the number of nights away."""
    fl = _normalize_flt(st.session_state.get('cedit_a_flt'))
    if not re.fullmatch(r'UL\d{1,4}', fl):
        st.warning("Type a UL flight number first (e.g. UL404).")
        return
    info = _flight_info(fl)
    if not info:
        st.warning(f"{fl} isn't in the flight database — fill the details manually.")
        for k in ('cedit_a_ret', 'cedit_a_rdep', 'cedit_a_rarr', 'cedit_a_rco', 'cedit_a_rci'):
            st.session_state[k] = ''
        st.session_state['cedit_a_turn'] = False
        st.session_state['cedit_a_layover'] = False
        return
    lay = LAYOVER_TRIPS.get(fl)
    from_roster = fl in _roster_flight_times()
    dep, arr = info.get('dep', ''), info.get('arr', '')
    st.session_state['cedit_a_o'] = info.get('o', 'CMB')
    st.session_state['cedit_a_d'] = info.get('d', '')
    st.session_state['cedit_a_ci'] = _hm_shift(dep, -80)
    st.session_state['cedit_a_dep'] = dep
    st.session_state['cedit_a_arr'] = arr
    st.session_state['cedit_a_co'] = _hm_shift(arr, 30) if lay else ''
    st.session_state['cedit_a_ac'] = info.get('ac', '')
    ret = (lay or {}).get('ret') or info.get('ret')
    if ret and ret in UL_DIRECT:
        ri = _flight_info(ret) if ret in _roster_flight_times() else UL_DIRECT[ret]
        st.session_state['cedit_a_ret'] = ret
        st.session_state['cedit_a_rdep'] = ri.get('dep', '')
        st.session_state['cedit_a_rarr'] = ri.get('arr', '')
        st.session_state['cedit_a_rco'] = _hm_shift(ri.get('arr', ''), 30)
        st.session_state['cedit_a_rci'] = _hm_shift(ri.get('dep', ''), -60) if lay else ''
        src = "your roster" if from_roster else "flight database"
        if lay:
            dep_t, arr_t = _parse_hm(dep), _parse_hm(arr)
            overnight = bool(dep_t and arr_t and arr_t < dep_t)
            nights = _lay_nights(lay, day) if day else lay['nights']
            st.session_state['cedit_a_retday'] = (1 if overnight else 0) + nights
            st.session_state['cedit_a_nights'] = nights
            st.session_state['cedit_a_layover'] = True
            if day:
                st.session_state['cedit_a_retdate'] = day + timedelta(days=st.session_state['cedit_a_retday'])
            retdate = (day + timedelta(days=st.session_state['cedit_a_retday'])) if day else None
            st.success(f"Filled {fl}: {info['o']}→{info['d']} {dep}–{arr} · layover {nights} night(s)"
                       f" · returns {ret} {retdate.strftime('%d %b') if retdate else ''} · from {src}")
        else:
            st.session_state['cedit_a_retday'] = int(info.get('ret_day', 0) or 0)
            st.session_state['cedit_a_layover'] = False
            st.success(f"Filled {fl}: {info['o']}→{info['d']} {dep}–{arr} · return {ret}"
                       f" {'next day' if info.get('ret_day') else 'same day'} · from {src}")
        st.session_state['cedit_a_turn'] = True
    else:
        for k in ('cedit_a_ret', 'cedit_a_rdep', 'cedit_a_rarr', 'cedit_a_rco', 'cedit_a_rci'):
            st.session_state[k] = ''
        st.session_state['cedit_a_turn'] = False
        st.session_state['cedit_a_layover'] = False
        src = "your roster" if from_roster else "flight database"
        st.success(f"Filled {fl}: {info['o']}→{info['d']} {dep}–{arr} · from {src}")


def _commit_roster_change(new_rows, note=None):
    """Persist edited rows back to the current roster and reload. `note` (if
    any) is shown as a warning banner on the next render."""
    _snapshot_for_undo()
    st.session_state['current_roster'] = save_rows_as_roster(st.session_state['username'], new_rows)
    st.session_state.pop('caledit_target', None)
    if note:
        st.session_state['_cedit_note'] = note
    st.success("Current roster updated — dashboard & intel recalculated.")
    st.rerun()


def _time_field(label, key, default=""):
    kwargs = {"placeholder": "HH:MM or blank", "key": key}
    if key not in st.session_state:
        kwargs["value"] = default
    return st.text_input(label, **kwargs)


def _render_duty_edit_form(rows, idx):
    """Form to edit an existing duty row (type-preserving)."""
    r = rows[idx]
    t = r["Type"]
    anchor = r["DateObj"].date() if isinstance(r.get("DateObj"), datetime) else datetime.now().date()
    with st.form(key="cedit_edit_form"):
        if t == "FLIGHT":
            fl = st.text_input("Flight number", value=r.get("Code") or "UL", key="cedit_e_flt").replace(" ", "")
            o, d = _route_od(r.get("Route"))
            c1, c2 = st.columns(2)
            with c1:
                origin = st.text_input("From (IATA)", value=o or "CMB", key="cedit_e_o").strip().upper()
            with c2:
                dest = st.text_input("To (IATA)", value=d or "", key="cedit_e_d").strip().upper()
            c3, c4, c5, c6 = st.columns(4)
            with c3:
                ci = _time_field("Check-in", "cedit_e_ci", _prefill(r.get("CIdt")))
            with c4:
                dep = _time_field("Departure", "cedit_e_dep", _prefill(r.get("DEPdt")))
            with c5:
                arr = _time_field("Arrival", "cedit_e_arr", _prefill(r.get("ARRdt")))
            with c6:
                co = _time_field("Check-out", "cedit_e_co", _prefill(r.get("COdt")))
            ac = st.text_input("Aircraft (optional)", value=r.get("Aircraft") or "", key="cedit_e_ac").strip().upper()
            ok, msg = True, ""
            if not re.fullmatch(r'UL\d{1,4}', fl.upper()):
                ok, msg = False, "Flight number must look like UL404."
            if not re.fullmatch(r'[A-Z]{3}', origin) or not re.fullmatch(r'[A-Z]{3}', dest):
                ok, msg = False, "From/To must be 3-letter IATA codes."
            if _parse_hm(dep) is None or _parse_hm(arr) is None:
                ok, msg = False, "Departure & Arrival need HH:MM times."
            if st.form_submit_button("💾 Save changes", key="cedit_edit_submit"):
                if ok:
                    new_rows = list(rows)
                    new_rows[idx] = build_duty_row(anchor, "FLIGHT", fl.upper(), ci=_parse_hm(ci), dep=_parse_hm(dep),
                                                   arr=_parse_hm(arr), co=_parse_hm(co), origin=origin, dest=dest, ac=ac or "-")
                    _commit_roster_change(_sort_rows(new_rows))
                else:
                    st.error(msg)
        elif t == "STANDBY":
            cur = r.get("Code") or "SB2"
            code = st.selectbox("Standby code", _ADD_SBY_CODES,
                                index=(_ADD_SBY_CODES.index(cur) if cur in _ADD_SBY_CODES else 0), key="cedit_e_sb")
            c1, c2 = st.columns(2)
            with c1:
                start = _time_field("Start", "cedit_e_s0", _prefill(r.get("CIdt") or r.get("DEPdt")))
            with c2:
                end = _time_field("End", "cedit_e_s1", _prefill(r.get("COdt") or r.get("ARRdt")))
            if st.form_submit_button("💾 Save changes", key="cedit_edit_submit"):
                if _parse_hm(start) is None or _parse_hm(end) is None:
                    st.error("Start & End need HH:MM times.")
                else:
                    new_rows = list(rows)
                    new_rows[idx] = build_duty_row(anchor, "STANDBY", code, ci=_parse_hm(start), dep=_parse_hm(start),
                                                   arr=_parse_hm(end), co=_parse_hm(end))
                    _commit_roster_change(_sort_rows(new_rows))
        elif t in ("DAY OFF", "LEAVE", "TIMEOFF", "DUTY", "LAYOVER"):
            code = r.get("Code") or ""
            pool = {"DAY OFF": _ADD_OFF_CODES, "LEAVE": _ADD_LEAVE_CODES, "TIMEOFF": _ADD_TOF_CODES,
                    "DUTY": _ADD_DUTY_CODES, "LAYOVER": ["HTL"]}[t]
            if code not in pool:
                pool = [code] + pool
            ccode = st.selectbox("Code", pool, index=pool.index(code), key="cedit_e_code")
            d1 = r.get("EndDateObj") if isinstance(r.get("EndDateObj"), datetime) else r.get("DateObj")
            if t == "LAYOVER":
                stn = st.text_input("Station (IATA)", value=(r.get("Route") or ""), key="cedit_e_stn").strip().upper()
                end = st.date_input("Until", value=(d1.date() if isinstance(d1, datetime) else anchor), key="cedit_e_end")
                if st.form_submit_button("💾 Save changes", key="cedit_edit_submit"):
                    if not re.fullmatch(r'[A-Z]{3}', stn):
                        st.error("Station must be a 3-letter IATA code.")
                    else:
                        new_rows = list(rows)
                        new_rows[idx] = build_duty_row(anchor, "LAYOVER", "HTL", origin=stn,
                                                       end_day=(end if end > anchor else None))
                        _commit_roster_change(_sort_rows(new_rows))
            elif t == "TIMEOFF":
                c1, c2 = st.columns(2)
                with c1:
                    s0 = _time_field("From", "cedit_e_t0", _prefill(r.get("DEPdt")))
                with c2:
                    s1 = _time_field("To", "cedit_e_t1", _prefill(r.get("ARRdt")))
                if st.form_submit_button("💾 Save changes", key="cedit_edit_submit"):
                    if _parse_hm(s0) is None or _parse_hm(s1) is None:
                        st.error("From & To need HH:MM times.")
                    else:
                        new_rows = list(rows)
                        new_rows[idx] = build_duty_row(anchor, "TIMEOFF", ccode, dep=_parse_hm(s0), arr=_parse_hm(s1))
                        _commit_roster_change(_sort_rows(new_rows))
            elif t == "DUTY":
                c1, c2 = st.columns(2)
                with c1:
                    s0 = _time_field("Start", "cedit_e_d0", _prefill(r.get("CIdt") or r.get("DEPdt")))
                with c2:
                    s1 = _time_field("End", "cedit_e_d1", _prefill(r.get("COdt") or r.get("ARRdt")))
                if st.form_submit_button("💾 Save changes", key="cedit_edit_submit"):
                    if _parse_hm(s0) is None or _parse_hm(s1) is None:
                        st.error("Start & End need HH:MM times.")
                    else:
                        new_rows = list(rows)
                        new_rows[idx] = build_duty_row(anchor, "DUTY", ccode, ci=_parse_hm(s0), dep=_parse_hm(s0),
                                                       arr=_parse_hm(s1), co=_parse_hm(s1))
                        _commit_roster_change(_sort_rows(new_rows))
            else:  # DAY OFF / LEAVE — full days
                end = st.date_input("Until", value=(d1.date() if isinstance(d1, datetime) else anchor), key="cedit_e_end")
                if st.form_submit_button("💾 Save changes", key="cedit_edit_submit"):
                    new_rows = list(rows)
                    new_rows[idx] = build_duty_row(anchor, t, ccode, end_day=(end if end > anchor else None))
                    _commit_roster_change(_sort_rows(new_rows))
        else:
            st.markdown("This duty type can't be edited here — use the paste box.")


def _render_duty_add_form(rows, day):
    """Form to add a new duty on `day`. Type-specific widgets that must react
    instantly (flight number, Fill, turnaround/layover tick, standby code) live
    OUTSIDE the form; the fields + submit button sit inside it."""
    ftype = st.selectbox("Add duty type", ["Flight", "Standby", "Day off", "Leave / sick",
                                           "Time off", "Training / ground duty", "Layover"],
                         key="cedit_add_type")

    _lay = None
    if ftype == "Flight":
        fl_default = "UL" if "cedit_a_flt" not in st.session_state else None
        _fl_raw = st.text_input("Flight number (e.g. UL404)", key="cedit_a_flt",
                                value=fl_default, placeholder="UL404",
                                on_change=_cedit_flt_on_change)
        if st.session_state.pop('_autofill_flt', False):
            _fill_flight_details(day)
        _fl = _normalize_flt(_fl_raw)
        _lay = LAYOVER_TRIPS.get(_fl)
        _f1, _f2 = st.columns([2.6, 1])
        with _f2:
            if st.button("🔍 Fill from DB", key="cedit_fill_btn", use_container_width=True,
                         help="Autofill origin/destination/times/check-in/check-out from the flight database"):
                _fill_flight_details(day)
        with _f1:
            _info = _flight_info(_fl)
            _from_roster = _fl in _roster_flight_times()
            if _lay and _info:
                _dep_t = _parse_hm(_info.get('dep', ''))
                _arr_t = _parse_hm(_info.get('arr', ''))
                _overnight = bool(_dep_t and _arr_t and _arr_t < _dep_t)
                _n = _lay_nights(_lay, day)
                _retdate = day + timedelta(days=(1 if _overnight else 0) + _n)
                st.caption("🏨 Layover: " + _fl + " → " + _info.get("d", "") + " · " + str(_n)
                           + " night(s) · returns " + _lay["ret"] + " " + _retdate.strftime('%d %b')
                           + (" · your roster" if _from_roster else " · flight database"))
            elif _info:
                st.caption("✓ " + _fl + " · " + _info.get("o", "") + "→" + _info.get("d", "")
                           + " · " + _info.get("dep", "—") + "–" + _info.get("arr", "—")
                           + (" · return " + _info["ret"] + (" (next day)" if _info.get("ret_day") else "") if _info.get("ret") else "")
                           + (" · " + ("your roster" if _from_roster else "flight database")))
            elif _fl:
                st.caption("Not in the database — enter details manually below.")
            else:
                st.caption("Type a flight number, then press Enter or 🔍 Fill from DB — or enter details manually.")
        if _lay:
            turn = st.checkbox("🏨 Add full layover trip (outbound + hotel + return · clears those days)",
                               key="cedit_a_turn")
        else:
            turn = st.checkbox("🔄 Turnaround — also add the return leg", key="cedit_a_turn")

        # Live nights picker for layovers — lets you correct seasonal lengths
        # (e.g. some winter Londons run longer) before saving.
        if _lay and turn:
            _li = _flight_info(_fl) or {}
            _ldt = _parse_hm(_li.get('dep', '')); _lat = _parse_hm(_li.get('arr', ''))
            _lovn = bool(_ldt and _lat and _lat < _ldt)
            if 'cedit_a_nights' not in st.session_state:
                st.session_state['cedit_a_nights'] = _lay_nights(_lay, day)
            _n_pick = st.number_input("Nights away", min_value=0, max_value=21, step=1,
                                      key="cedit_a_nights",
                                      help="Auto-filled from the schedule — adjust for seasonal changes.")
            st.session_state['cedit_a_retday'] = (1 if _lovn else 0) + int(_n_pick)
            st.session_state['cedit_a_retdate'] = day + timedelta(days=st.session_state['cedit_a_retday'])

    elif ftype == "Standby":
        _sby_code = st.selectbox("Standby code", _ADD_SBY_CODES, key="cedit_sb_code",
                                 on_change=_sby_code_changed)

    with st.form(key="cedit_add_form"):
        if ftype == "Flight":
            c1, c2 = st.columns(2)
            with c1:
                origin = st.text_input("From (IATA)", key="cedit_a_o").strip().upper()
            with c2:
                dest = st.text_input("To (IATA)", key="cedit_a_d").strip().upper()
            c3, c4, c5, c6 = st.columns(4)
            with c3:
                ci = _time_field("Check-in", "cedit_a_ci")
            with c4:
                dep = _time_field("Departure", "cedit_a_dep", "07:00")
            with c5:
                arr = _time_field("Arrival", "cedit_a_arr", "09:00")
            with c6:
                co = _time_field("Check-out", "cedit_a_co")
            ac = st.text_input("Aircraft (optional)", key="cedit_a_ac").strip().upper()
            if turn:
                if _lay:
                    _rd = int(st.session_state.get('cedit_a_retday', 0) or 0)
                    _nn = int(st.session_state.get('cedit_a_nights', _lay_nights(_lay, day)) or 0)
                    st.caption("Return departs **" + (day + timedelta(days=_rd)).strftime('%d %b')
                               + "** · hotel " + str(_nn) + " night(s)"
                               + " · From/To swap automatically.")
                    r1, r2, r3, r4, r5 = st.columns(5)
                    with r1:
                        rfl = st.text_input("Return flight", key="cedit_a_ret").replace(" ", "").upper()
                    with r2:
                        rci = _time_field("Return CI", "cedit_a_rci")
                    with r3:
                        rdep = _time_field("Return dep", "cedit_a_rdep")
                    with r4:
                        rarr = _time_field("Return arr", "cedit_a_rarr")
                    with r5:
                        rco = _time_field("Return co", "cedit_a_rco")
                else:
                    _rd = int(st.session_state.get('cedit_a_retday', 0) or 0)
                    st.caption("Return departs the **" + ("next day" if _rd else "same day")
                               + "** · From/To swap automatically.")
                    r1, r2, r3, r4 = st.columns(4)
                    with r1:
                        rfl = st.text_input("Return flight no.", key="cedit_a_ret").replace(" ", "").upper()
                    with r2:
                        rdep = _time_field("Return dep", "cedit_a_rdep")
                    with r3:
                        rarr = _time_field("Return arr", "cedit_a_rarr")
                    with r4:
                        rco = _time_field("Return co", "cedit_a_rco")
            ok, msg = True, ""
            if not re.fullmatch(r'UL\d{1,4}', _fl):
                ok, msg = False, "Flight number must look like UL404."
            if not re.fullmatch(r'[A-Z]{3}', origin) or not re.fullmatch(r'[A-Z]{3}', dest):
                ok, msg = False, "From/To must be 3-letter IATA codes."
            if _parse_hm(dep) is None or _parse_hm(arr) is None:
                ok, msg = False, "Departure & Arrival need HH:MM times."
            if turn:
                rfl_v = (st.session_state.get('cedit_a_ret') or '').replace(' ', '').upper()
                if not re.fullmatch(r'UL\d{1,4}', rfl_v):
                    ok, msg = False, "Return flight number must look like UL405."
                elif _parse_hm(st.session_state.get('cedit_a_rdep', '')) is None \
                        or _parse_hm(st.session_state.get('cedit_a_rarr', '')) is None:
                    ok, msg = False, "Return leg needs Departure & Arrival HH:MM times."
            if st.form_submit_button(f"➕ Add flight to {day.strftime('%d %b')}", key="cedit_add_submit"):
                if not ok:
                    st.error(msg)
                else:
                    row_out = build_duty_row(day, "FLIGHT", _fl, ci=_parse_hm(ci), dep=_parse_hm(dep),
                                             arr=_parse_hm(arr), co=_parse_hm(co), origin=origin, dest=dest, ac=ac or "-")
                    note = None
                    if not turn:
                        _commit_roster_change(_sort_rows(list(rows) + [row_out]))
                    elif _lay:
                        rfl_v = (st.session_state.get('cedit_a_ret') or '').replace(' ', '').upper()
                        _rd = int(st.session_state.get('cedit_a_retday', 0) or 0)
                        ret_day = day + timedelta(days=_rd)
                        arr_dt = row_out.get('ARRdt')
                        htl_start = arr_dt.date() if isinstance(arr_dt, datetime) else day
                        htl_co_t = _parse_hm(st.session_state.get('cedit_a_co', ''))
                        rci_t = (_parse_hm(st.session_state.get('cedit_a_rci', ''))
                                 or _hm_shift(st.session_state.get('cedit_a_rdep', ''), -60))
                        rdep_t = _parse_hm(st.session_state.get('cedit_a_rdep', ''))
                        rarr_t = _parse_hm(st.session_state.get('cedit_a_rarr', ''))
                        rco_t = _parse_hm(st.session_state.get('cedit_a_rco', ''))
                        rac = UL_DIRECT.get(rfl_v, {}).get('ac') or (ac or '-')
                        htl = build_duty_row(htl_start, "LAYOVER", "HTL", origin=dest,
                                             dep=htl_co_t, arr=rci_t, end_day=ret_day)
                        row_ret = build_duty_row(ret_day, "FLIGHT", rfl_v, ci=rci_t, dep=rdep_t,
                                                 arr=rarr_t, co=rco_t, origin=dest, dest=origin, ac=rac)
                        kept, removed = _remove_days(rows, day, ret_day)
                        if removed:
                            note = ("🏨 Layover trip added (" + _fl + " → " + dest + "). "
                                    "Removed to make room:\n"
                                    + "\n".join("• " + _row_summary(r) for r in removed))
                        _commit_roster_change(_sort_rows(kept + [row_out, htl, row_ret]), note=note)
                    else:
                        rfl_v = (st.session_state.get('cedit_a_ret') or '').replace(' ', '').upper()
                        _rd = int(st.session_state.get('cedit_a_retday', 0) or 0)
                        rdep_t = _parse_hm(st.session_state.get('cedit_a_rdep', ''))
                        rarr_t = _parse_hm(st.session_state.get('cedit_a_rarr', ''))
                        rco_t = _parse_hm(st.session_state.get('cedit_a_rco', ''))
                        rac = UL_DIRECT.get(rfl_v, {}).get('ac') or (ac or '-')
                        row_ret = build_duty_row(day + timedelta(days=_rd), "FLIGHT", rfl_v,
                                                 dep=rdep_t, arr=rarr_t, co=rco_t,
                                                 origin=dest, dest=origin, ac=rac)
                        _commit_roster_change(_sort_rows(list(rows) + [row_out, row_ret]))
        elif ftype == "Standby":
            w = _SBY_WINDOWS.get(_sby_code, ("06:00", "18:00"))
            c1, c2 = st.columns(2)
            with c1:
                s0 = _time_field("Start", "cedit_sb_s0", w[0])
            with c2:
                s1 = _time_field("End", "cedit_sb_s1", w[1])
            if _sby_code == "SB4":
                st.caption("SB4 runs overnight — ends 05:59 the next morning.")
            if st.form_submit_button(f"➕ Add standby to {day.strftime('%d %b')}", key="cedit_add_submit"):
                if _parse_hm(s0) is None or _parse_hm(s1) is None:
                    st.error("Start & End need HH:MM times.")
                else:
                    row = build_duty_row(day, "STANDBY", _sby_code, ci=_parse_hm(s0), dep=_parse_hm(s0),
                                         arr=_parse_hm(s1), co=_parse_hm(s1))
                    kept, removed = _remove_conflicts(rows, row)
                    note = None
                    if removed:
                        note = ("⏱ " + _sby_code + " added (" + row['Departure'] + "–"
                                + row['Checkout'] + "). Removed to make room:\n"
                                + "\n".join("• " + _row_summary(r) for r in removed))
                    _commit_roster_change(_sort_rows(kept + [row]), note=note)
        elif ftype == "Day off":
            code = st.selectbox("Off code", _ADD_OFF_CODES, key="cedit_off_code")
            end = st.date_input("Until (leave = today for a single day)", value=day, key="cedit_off_end")
            if st.form_submit_button(f"➕ Add day(s) off from {day.strftime('%d %b')}", key="cedit_add_submit"):
                row = build_duty_row(day, "DAY OFF", code, end_day=(end if end > day else None))
                _commit_roster_change(_sort_rows(list(rows) + [row]))
        elif ftype == "Leave / sick":
            code = st.selectbox("Leave code", _ADD_LEAVE_CODES, key="cedit_lv_code")
            end = st.date_input("Until (leave = today for a single day)", value=day, key="cedit_lv_end")
            if st.form_submit_button(f"➕ Add leave from {day.strftime('%d %b')}", key="cedit_add_submit"):
                row = build_duty_row(day, "LEAVE", code, end_day=(end if end > day else None))
                _commit_roster_change(_sort_rows(list(rows) + [row]))
        elif ftype == "Time off":
            code = st.selectbox("Time-off code", _ADD_TOF_CODES, key="cedit_tof_code")
            c1, c2 = st.columns(2)
            with c1:
                s0 = _time_field("From", "cedit_tof_s0", "09:00")
            with c2:
                s1 = _time_field("To", "cedit_tof_s1", "17:00")
            if st.form_submit_button(f"➕ Add time-off to {day.strftime('%d %b')}", key="cedit_add_submit"):
                if _parse_hm(s0) is None or _parse_hm(s1) is None:
                    st.error("From & To need HH:MM times.")
                else:
                    row = build_duty_row(day, "TIMEOFF", code, dep=_parse_hm(s0), arr=_parse_hm(s1))
                    _commit_roster_change(_sort_rows(list(rows) + [row]))
        elif ftype == "Training / ground duty":
            code = st.selectbox("Duty code", _ADD_DUTY_CODES, key="cedit_du_code")
            c1, c2 = st.columns(2)
            with c1:
                s0 = _time_field("Start", "cedit_du_s0", "09:00")
            with c2:
                s1 = _time_field("End", "cedit_du_s1", "17:00")
            if st.form_submit_button(f"➕ Add duty to {day.strftime('%d %b')}", key="cedit_add_submit"):
                if _parse_hm(s0) is None or _parse_hm(s1) is None:
                    st.error("Start & End need HH:MM times.")
                else:
                    row = build_duty_row(day, "DUTY", code, ci=_parse_hm(s0), dep=_parse_hm(s0),
                                         arr=_parse_hm(s1), co=_parse_hm(s1))
                    _commit_roster_change(_sort_rows(list(rows) + [row]))
        else:  # Layover
            stn = st.text_input("Station (IATA)", key="cedit_lay_stn").strip().upper()
            end = st.date_input("Until (leave = today for a single day)", value=day, key="cedit_lay_end")
            if st.form_submit_button(f"➕ Add layover from {day.strftime('%d %b')}", key="cedit_add_submit"):
                if not re.fullmatch(r'[A-Z]{3}', stn):
                    st.error("Station must be a 3-letter IATA code.")
                else:
                    row = build_duty_row(day, "LAYOVER", "HTL", origin=stn, end_day=(end if end > day else None))
                    _commit_roster_change(_sort_rows(list(rows) + [row]))


def _render_calendar_editor(rows, sel_span):
    """Edit-mode calendar for the CURRENT period: a day grid with an ✎ per day,
    plus a per-day editor that rewrites the live roster."""
    username = st.session_state['username']
    start, last = sel_span

    note = st.session_state.pop('_cedit_note', None)
    if note:
        st.warning(note)

    # pending delete (flag set by a 🗑 button, applied on the next run)
    del_idx = st.session_state.pop('_cedit_del', None)
    if del_idx is not None and 0 <= del_idx < len(rows):
        new_rows = [r for j, r in enumerate(rows) if j != del_idx]
        _snapshot_for_undo()
        st.session_state['current_roster'] = save_rows_as_roster(username, new_rows)
        st.session_state.pop('caledit_target', None)
        st.success("Duty removed from the current roster.")
        st.rerun()

    today = datetime.now().date()
    grid_start = start - timedelta(days=start.weekday())
    days = [grid_start + timedelta(days=i) for i in range(28)]

    hd = st.columns(7)
    for i, wd in enumerate(["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]):
        with hd[i]:
            st.markdown(f"<div class='cal-hd'>{wd}</div>", unsafe_allow_html=True)

    for wk in range(4):
        cols = st.columns(7)
        for i in range(7):
            d = days[wk * 7 + i]
            with cols[i]:
                in_period = start <= d <= last
                labels = _day_summary_labels(rows, d) if in_period else []
                hl = "#00bcd4" if d == today else "#1f2b3a"
                body = "".join(f"<div class='chip' style='margin-bottom:2px;font-size:11px;'>{lbl}</div>"
                               for lbl in labels)
                if in_period and not labels:
                    body = "<div class='chip chip-none' style='font-size:11px;'>No duty</div>"
                st.markdown(
                    f"<div style='background:#0f1926;border:1px solid {hl};border-radius:8px;"
                    f"min-height:58px;padding:4px 6px;{'opacity:.35;' if not in_period else ''}'>"
                    f"<div class='cal-date' style='font-size:12px;margin-bottom:2px;'>{d.day}"
                    f"{' ' + d.strftime('%b') if d.day == 1 else ''}</div>{body}</div>",
                    unsafe_allow_html=True)
                if in_period:
                    st.button("✎", key=f"cedit_{d.isoformat()}",
                              use_container_width=True,
                              help=f"Add or edit a duty on {d.strftime('%d %b')}",
                              on_click=_cedit_open, args=(d,))

    ed_day = st.session_state.get('caledit_day')
    if ed_day is None:
        return
    if not (start <= ed_day <= last):
        st.session_state.pop('caledit_day', None)
        return

    st.divider()
    idxs = duty_rows_on_day(rows, ed_day)
    ehead, eundo, eclose = st.columns([5.4, 0.8, 0.8])
    with ehead:
        st.markdown(f"**✏️ {ed_day.strftime('%A %d %b %Y')}** — {len(idxs)} duty(s) on this day")
    with eundo:
        if st.button("↩", key="cedit_undo", use_container_width=True,
                     help="Undo the last calendar edit",
                     disabled=not st.session_state.get('_undo_stack')):
            _undo_last()
    with eclose:
        if st.button("✕", key="cedit_close", use_container_width=True, help="Close the editor"):
            st.session_state.pop('caledit_day', None)
            st.session_state.pop('caledit_target', None)
            st.rerun()

    for j in idxs:
        r = rows[j]
        c1, c2, c3 = st.columns([8, 0.8, 0.8])
        with c1:
            st.markdown(f"<div style='padding:6px 2px;'>{_row_summary(r)}</div>", unsafe_allow_html=True)
        with c2:
            if st.button("✏️", key=f"cedit_edit_{j}", help="Edit this duty"):
                st.session_state['caledit_target'] = j
                st.rerun()
        with c3:
            if st.button("🗑", key=f"cedit_del_{j}", help="Delete this duty"):
                st.session_state['_cedit_del'] = j
                st.rerun()

    target = st.session_state.get('caledit_target')
    if target is not None and target not in idxs:
        st.session_state.pop('caledit_target', None)
        target = None

    if target is not None:
        _render_duty_edit_form(rows, target)
    else:
        _render_duty_add_form(rows, ed_day)


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

def _sheet_roster_cols(rows):
    """Build the Google Sheet 'Your Roster' column model consumed by the salary
    formulas. Column layout (as read by the sheet's MAP(...) formulas):
        A Activity · B Checkin · C Start(dep) · E Arr(IATA) · F End(arr) · G Checkout
    The portal only writes a Checkin on a duty's FIRST sector, so duty
    boundaries are walked by Checkin. For an HTL row the sheet leaves
    Checkin/Checkout BLANK and carries the hotel span in Start/End — LAYOVER
    rows therefore get checkin=None / checkout=None here (the parser's CIdt /
    COdt convenience copies are ignored on purpose)."""
    cols = []
    for r in rows:
        t = r["Type"]
        raw_act = (r.get("Code") or r.get("Flight / Code") or "")
        act = re.sub(r"\s+", "", str(raw_act)).upper()
        if t == "FLIGHT":
            o, d = _route_od(r.get("Route"))
            dep_iata, arr_iata = (o or "CMB"), (d or "CMB")
            checkin, start, end, checkout = (r.get("CIdt"), r.get("DEPdt"),
                                             r.get("ARRdt"), r.get("COdt"))
            sched_min = UL_SCHED_MIN.get(act)
            blk = None
            if r.get("ARRdt") and r.get("DEPdt"):
                a = r.get("ARRdt_u") or r.get("ARRdt")
                b = r.get("DEPdt_u") or r.get("DEPdt")
                if a and b:
                    blk = max(0, int((a - b).total_seconds() // 60))
        elif t == "LAYOVER":
            stn = (r.get("Route") or "").strip()
            dep_iata = arr_iata = stn if (len(stn) == 3 and stn.isalpha()) else "CMB"
            checkin, start, end, checkout = None, r.get("DEPdt"), r.get("ARRdt"), None
            sched_min, blk = None, None
        else:
            dep_iata = arr_iata = "CMB"
            checkin, start, end, checkout = (r.get("CIdt"), r.get("DEPdt"),
                                             r.get("ARRdt"), r.get("COdt"))
            sched_min, blk = None, None
        cols.append({"act": act, "checkin": checkin, "start": start, "end": end,
                     "checkout": checkout, "dep_iata": dep_iata, "arr_iata": arr_iata,
                     "sched_min": sched_min, "block_min": blk})
    return cols


def _sheet_duty_start(cols, rn):
    """Sheet d_start_rn: the most recent row at/above rn carrying a Checkin (a
    duty begins at its first sector's check-in)."""
    for j in range(rn, -1, -1):
        if cols[j]["checkin"] is not None:
            return j
    return rn


def _sheet_duty_end(cols, rn):
    """Sheet d_end_rn: the next row strictly below rn carrying a Checkin (start
    of the NEXT duty); the last row when none follows."""
    for j in range(rn + 1, len(cols)):
        if cols[j]["checkin"] is not None:
            return j
    return len(cols) - 1


def _sheet_meal_obopma(cols):
    """Port of the 'Meal Allowances' (col P) and 'OBOPMA' (col Q) formulas.
    P (entitled meals) = hotel rows' stay meals + on-board meals of every
    layover-trip flight. Q (OBOPMA) = the on-board part only (deducted from
    salary). A flight belongs to a layover trip when its DUTY (bounded by
    check-ins) contains an HTL ("forward"), or when an HTL sits within 3 rows
    above it and its start is within 14 h of the hotel's end ("backward").
    Returns (P, Q) lists aligned with `cols`, None = blank cell."""
    n = len(cols)
    P = [None] * n
    Q = [None] * n
    for rn in range(n):
        c = cols[rn]
        act = c["act"]
        if not act:
            continue
        s_time = c["checkin"] if c["checkin"] is not None else c["start"]
        e_time = c["checkout"] if c["checkout"] is not None else c["end"]
        if act == "HTL":
            pf = next((j for j in range(rn - 1, -1, -1)
                       if cols[j]["act"].startswith("UL")), None)
            pf_end = None
            if pf is not None:
                pf_end = (cols[pf]["checkout"] if cols[pf]["checkout"] is not None
                          else cols[pf]["end"])
            start_m = max(pf_end, s_time) if (pf_end is not None and s_time is not None) \
                else (pf_end if pf_end is not None else s_time)
            if start_m is not None and c["end"] is not None:
                P[rn] = _meal_counts(start_m, c["end"])
            continue
        if not act.startswith("UL"):
            continue
        ds, de = _sheet_duty_start(cols, rn), _sheet_duty_end(cols, rn)
        lay_fwd = any(cols[j]["act"] == "HTL" for j in range(ds, de + 1))
        last_htl = next((j for j in range(rn - 1, -1, -1) if cols[j]["act"] == "HTL"), None)
        lay_bwd = False
        if last_htl is not None and (rn - last_htl) <= 3:
            htl_end = cols[last_htl]["end"]
            if s_time is not None and htl_end is not None \
                    and (s_time - htl_end) < timedelta(hours=14):
                lay_bwd = True
        if (lay_fwd or lay_bwd) and s_time is not None and e_time is not None:
            P[rn] = _meal_counts(s_time, e_time)
            Q[rn] = P[rn]
    return P, Q


def _sheet_return_hours(cols, rn):
    """Duration (hours) of the station→CMB return leg for the inferred L-O/N
    edge case (the row just below an HTL has no activity). The sheet reads a
    'Named Ranges' route table; we use the UL# schedule table when the flight
    number is known, else the sector's block time."""
    stn = cols[rn]["arr_iata"]
    for j in range(rn + 1, len(cols)):
        c = cols[j]
        if not c["act"].startswith("UL"):
            continue
        if c["dep_iata"] != stn or c["arr_iata"] != "CMB":
            continue
        if c["sched_min"]:
            return c["sched_min"] / 60.0
        if c["block_min"]:
            return c["block_min"] / 60.0
        return 0.0
    return 0.0


def _sheet_l_overnight(cols):
    """L Overnight (Hours & ONights col I): nights away from base, one value per
    HTL row = INT(next activity time) − INT(prev activity time), plus an
    inferred night when the return flight's duration crosses midnight past the
    hotel end. Returns (total, nights_by_row)."""
    n = len(cols)
    nights_by_row = [0] * n
    total = 0
    for rn in range(n):
        c = cols[rn]
        if c["act"] != "HTL":
            continue
        if rn >= 1 and cols[rn - 1]["arr_iata"] == "CMB":
            prev_time = cols[rn - 1]["checkout"]
        elif rn >= 1 and cols[rn - 1]["checkin"] is None:
            prev_time = cols[rn - 3]["checkin"] if rn - 3 >= 0 else None
        else:
            prev_time = cols[rn - 1]["checkin"] if rn >= 1 else None
        n1 = cols[rn + 1]["checkout"] if rn + 1 < n else None
        n2 = cols[rn + 2]["checkout"] if rn + 2 < n else None
        if n1 is None and n2 is None:
            next_time = c["end"]
        elif n1 is None:
            next_time = n2
        else:
            next_time = n1
        raw = 0
        if prev_time is not None and next_time is not None and next_time > prev_time:
            raw = (next_time.date() - prev_time.date()).days
        inferred = 0
        if rn + 1 < n and not cols[rn + 1]["act"] and c["end"] is not None \
                and c["arr_iata"] != "CMB":
            dur_h = _sheet_return_hours(cols, rn)
            if dur_h > 0:
                inferred = int((c["end"] + timedelta(hours=dur_h)).date() - c["end"].date())
        nights_by_row[rn] = raw + inferred
        total += raw + inferred
    return total, nights_by_row


def _sheet_t_overnight(cols):
    """T Overnight (Hours & ONights col J): turnaround nights. A return leg
    (blank check-in, has check-out, previous row has a check-in) counts
    check-out day − previous check-in day; a standard crossing counts
    check-out day − check-in day. Rows adjacent to an HTL (layover flights)
    are blank, and a return leg whose previous-previous row is HTL (the second
    sector of a layover return) is blank too."""
    n = len(cols)
    total = 0
    for rn in range(n):
        act = cols[rn]["act"]
        if not act or act == "OFF" or act == "SICK" or act.startswith("SB"):
            continue
        next_act = cols[rn + 1]["act"] if rn + 1 < n else ""
        prev_act = cols[rn - 1]["act"] if rn >= 1 else ""
        prev_checkin = cols[rn - 1]["checkin"] if rn >= 1 else None
        prev_prev_act = cols[rn - 2]["act"] if rn >= 2 else ""
        if next_act == "HTL" or prev_act == "HTL":
            continue                                   # layover-adjacent → blank
        is_ret = (cols[rn]["checkin"] is None and cols[rn]["checkout"] is not None
                  and prev_checkin is not None)
        if is_ret:
            if prev_prev_act == "HTL":
                continue                               # layover return 2nd sector
            d = (cols[rn]["checkout"].date() - prev_checkin.date()).days
            if d > 0:
                total += d
        elif cols[rn]["checkin"] is not None and cols[rn]["checkout"] is not None \
                and cols[rn]["checkout"].date() > cols[rn]["checkin"].date():
            total += (cols[rn]["checkout"].date() - cols[rn]["checkin"].date()).days
    return total
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

    # --- allowance / OBOPMA / overnights — ported 1:1 from the Google Sheet's
    # 'Meal Allowances', 'OBOPMA', 'L Overnight' and 'T Overnight' formulas.
    # Duty boundaries are walked by CHECK-IN (the portal only writes a check-in
    # on a duty's first sector), so the combined flight before a layover
    # (UL133/UL134 → UL253 → HTL → UL254) chains naturally, while a separate
    # no-check-in red-eye (UL225/226) that merely precedes the layover duty is
    # cut off at the next duty's check-in — no heuristic guards needed. ---
    cols = _sheet_roster_cols(rows)
    P, Q = _sheet_meal_obopma(cols)

    ent = [0, 0, 0]      # entitled meals B/L/D (sheet col P)
    ob = [0, 0, 0]       # on-board meals (sheet col Q — deducted from salary)
    detail = []
    for i, r in enumerate(rows):
        if r["Type"] == "FLIGHT" and Q[i] is not None and sum(Q[i]) > 0:
            b, l, d = Q[i]
            for k, v in enumerate((b, l, d)):
                ent[k] += v
                ob[k] += v
            detail.append((f"✈ {r['Flight / Code']} ({r['Route']})", f"{b}B {l}L {d}D", "on board"))
        elif r["Type"] == "LAYOVER" and P[i] is not None and sum(P[i]) > 0:
            b, l, d = P[i]
            for k, v in enumerate((b, l, d)):
                ent[k] += v
            detail.append((f"🏨 Layover {r['Route']}", f"{b}B {l}L {d}D", "hotel"))

    l_nights, _lrow = _sheet_l_overnight(cols)
    t_on = _sheet_t_overnight(cols)

    # Per-layover allowance breakdown for the dashboard card: hotel meals (P)
    # + the on-board meals (Q) of this layover's outbound/inbound sectors.
    layover_allow = []
    claimed = set()
    for i, r in enumerate(rows):
        if r["Type"] != "LAYOVER":
            continue
        stn = cols[i]["arr_iata"]
        meals = sum(P[i]) if P[i] else 0
        htl_end = cols[i]["end"]
        for j in range(_sheet_duty_start(cols, i), i):          # outbound chain
            if j in claimed or rows[j]["Type"] != "FLIGHT" or Q[j] is None:
                continue
            meals += sum(Q[j])
            claimed.add(j)
        for j in range(i + 1, min(i + 4, len(rows))):            # inbound chain
            if j in claimed or rows[j]["Type"] != "FLIGHT" or Q[j] is None:
                continue
            s_t = cols[j]["checkin"] if cols[j]["checkin"] is not None else cols[j]["start"]
            if htl_end is not None and s_t is not None and (s_t - htl_end) < timedelta(hours=14):
                meals += sum(Q[j])
                claimed.add(j)
        layover_allow.append({
            "station": stn if len(stn) == 3 else (r.get("Route") or "LAY"),
            "meals": meals, "nights": _lrow[i],
            "usd": meals * MEAL_RATE_USD + _lrow[i] * OVERNIGHT_RATE_USD[cat],
        })

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
purge_pre2026_history()

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
    .chip-off-mand { background:#3a1220; color:#ff8a8a; border:1px solid #ff5252; }
    .chip-tof { background:#1a2233; color:#90a4c8; border:1px dashed #3d4d6b; }
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

    page_dash, page_salary, page_analytics, page_fdp = st.tabs(["📋 Dashboard", "💰 Salary Calculator", "📊 Salary Analytics", "⏱ FDP Calculator"])

    with page_dash:
        _cur_period = roster_period_of_roster(valid_dates_all) if valid_dates_all else None

        # Resolve the period-navigation state HERE, before any panel reads it, so
        # every panel and the calendar agree on the selected period. The ‹ › ⟲
        # buttons change 'cal_period_sel' directly through the `_nav_go` on_click
        # callback (which runs before the script body), so there is nothing
        # deferred to apply here — we only clamp to a valid period and stash the
        # period grid for the callbacks to use.
        _cal_rows = calendar_rows(st.session_state['username'], parsed_rows)
        _cal_dates = [r["DateObj"].date() for r in _cal_rows if r["DateObj"] is not None]
        _period_starts = []
        _t0 = None
        if _cal_dates:
            _pmin = roster_period_bounds(min(_cal_dates))[0]
            _pmax = roster_period_bounds(max(_cal_dates))[0]
            _p = _pmin
            while _p <= _pmax:
                _period_starts.append(_p)
                _p += timedelta(days=ROSTER_PERIOD_DAYS)
            _t0 = roster_period_bounds(datetime.now().date())[0]
            if ('cal_period_sel' not in st.session_state
                    or st.session_state['cal_period_sel'] not in _period_starts):
                st.session_state['cal_period_sel'] = _t0 if _t0 in _period_starts else _period_starts[0]
        # stash for the ‹ › ⟲ on_click callbacks
        st.session_state['_period_starts'] = _period_starts
        st.session_state['_t0'] = _t0

        _view_sel = st.session_state.get('cal_period_sel')
        viewing_past = bool(_cur_period is not None and _view_sel is not None
                             and _view_sel != _cur_period)

        # Analytics / fatigue / FDP panels reflect the SELECTED period: the live
        # current roster normally, the finalized performed roster when browsing a
        # past period. (Intel / Guardian / live monitoring stay current-only.)
        if viewing_past and _view_sel is not None:
            view_rows = period_rows_for_display(st.session_state['username'], parsed_rows, _view_sel)
        else:
            view_rows = parsed_rows
        view_analytics = compute_analytics(view_rows)
        _view_label = (f"{_view_sel.strftime('%d %b')} – "
                       f"{(_view_sel + timedelta(days=ROSTER_PERIOD_DAYS - 1)).strftime('%d %b %Y')}"
                       if viewing_past and _view_sel is not None else None)

        left_col, main_col, right_col = st.columns([1, 2.3, 1.3])

        # ---------- LEFT: ANALYTICS & FATIGUE ----------
        with left_col:
            st.markdown("#### Analytics & Fatigue Tracker")
            if _view_label:
                st.markdown(
                    f"<div class='muted' style='font-size:12px;margin-bottom:8px;'>"
                    f"Showing <b>{_view_label}</b> (performed) \u2014 archived period.</div>",
                    unsafe_allow_html=True)
            pct = view_analytics["block_hrs"] / view_analytics["block_target"] if view_analytics["block_target"] else 0
            donut = donut_svg(pct, str(view_analytics["block_hrs"]), f"of {view_analytics['block_target']} hrs")
            st.markdown(
                f"<div class='card' style='text-align:center;'><h5>Cumulative Block Hours</h5>"
                f"{donut}"
                f"<div class='muted'>this {'period' if _view_label else 'roster'} ({pct*100:.0f}%) \u00b7 {view_analytics['flights']} sectors</div></div>",
                unsafe_allow_html=True)
            spark = sparkline_svg([view_analytics['daily_min'].get(d, 0) for d in sorted(view_analytics['daily_min'])] or [0])
            fat_color = "#4caf50" if view_analytics['fatigue'] < 4 else ("#ff9800" if view_analytics['fatigue'] < 7 else "#ff5252")
            fat_parts = sorted(view_analytics.get("fatigue_parts", []), key=lambda p: -p["pts"])
            parts_html = ""
            for p in fat_parts:
                if p["pts"] <= 0:
                    continue
                parts_html += (f"<div class='bidrow'><span>{p['label']}</span>"
                               f"<span>{p['detail']} \u00b7 +{p['pts']}</span></div>")
            st.markdown(
                f"<div class='card'><h5 style='text-align:center;'>Fatigue Score</h5>"
                f"<div style='text-align:center;'>{gauge_svg(view_analytics['fatigue'])}</div>"
                f"<div style='color:{fat_color};font-weight:700;font-size:14px;text-align:center;'>{view_analytics['fatigue_label']} ({view_analytics['fatigue']}/10)</div>"
                f"<div style='margin-top:6px;text-align:center;'>{spark}</div>"
                f"<div class='muted' style='text-align:center;'>{view_analytics['redeyes']} red-eye dep \u00b7 {view_analytics['max_streak']} consecutive duty days</div>"
                f"<div style='margin-top:8px;'>{parts_html}</div>"
                f"<div class='muted' style='margin-top:4px;'>heuristic from FOM Ch.08 fatigue drivers (early/late/night duties, 0100\u20130659 runs, day/night alternation, 18\u201330h rests after TZ flights, cumulative load, recovery) \u2014 not a regulatory limit.</div></div>",
                unsafe_allow_html=True)

            # ---------- LEFT: FDP COMPLIANCE (Chapter 08) ----------
            if view_rows:
                st.markdown("#### \u2696 FDP Compliance")
                _view_start = _view_sel if (_view_sel is not None) else _cur_period
                _ctx_rows = (surrounding_rows(st.session_state['username'], parsed_rows, _view_start)
                             if _view_start is not None else [])
                fdp = fdp_roster_audit(view_rows, context_rows=_ctx_rows)
                fviol = [f for f in fdp["findings"] if f[0] == "violation"]
                fnote = [f for f in fdp["findings"] if f[0] == "note"]
                cnt = fdp["counts"]
                cum = fdp["cumulative"]
                if not fdp["findings"]:
                    st.markdown(
                        "<div class='card' style='border-color:#4caf50;text-align:center;'>"
                        "<div style='color:#a5d6a7;font-weight:700;'>\u2705 FDP limits OK</div>"
                        "<div class='muted' style='font-size:11px;'>no early/late/night or cumulative breaches</div></div>",
                        unsafe_allow_html=True)
                else:
                    st.markdown(
                        f"<div class='card' style='border-color:#ff5252;text-align:center;'>"
                        f"<div style='color:#ff8a8a;font-weight:700;'>{len(fviol)} FDP breach(es)</div>"
                        + (f"<div class='muted' style='font-size:11px;'>{len(fnote)} note(s)</div>" if fnote else "") +
                        "</div>", unsafe_allow_html=True)
                do = fdp["days_off"]
                if do["off_rest_bad"]:
                    off_rest_txt = f"{do['off_rest_bad']} fail"
                elif do["off_rest_notes"]:
                    off_rest_txt = f"OK \u00b7 {do['off_rest_notes']} unverified"
                else:
                    off_rest_txt = "all OK"
                mand_dates = do.get("mandatory_dates", [])
                if do.get("mandatory_annual"):
                    mand_txt = "n/a · annual leave"
                    mand_line = "annual leave present — mandatory off days not checked"
                else:
                    mand_txt = f"{do['mandatory_count']} of {do['off_days']}"
                    mand_line = (", ".join(d.strftime("%d %b") for d in mand_dates)
                                 if mand_dates else "none individually required")
                davg = days_off_average_8_2_17_d(st.session_state['username'], parsed_rows,
                                                 upto=(_view_sel if viewing_past else None))
                if davg["avg"] is None:
                    davg_txt = f"N/A \u00b7 {davg['n_periods']}/3 periods"
                    davg_color = "#9fb3c8"
                    davg_foot = ""
                else:
                    last3 = davg["per"][-3:]
                    per_parts = " \u00b7 ".join(f"{p['off']}" for p in last3)
                    if any(p["off"] < 7 for p in last3):
                        per_parts = f"<span style='color:#ff8a8a;'>{per_parts}</span>"
                    davg_txt = f"{davg['total']} of 24 \u00b7 {'OK' if davg['ok'] else 'SHORT'}"
                    davg_color = "#a5d6a7" if davg["ok"] else "#ff8a8a"
                    davg_foot = (f"<div class='muted' style='font-size:11px;margin-top:2px;'>last 3 periods' off-days: "
                                 f"{per_parts} <span style='color:#8aa0b8;'>(min 7 each \u00b7 24 over 3)</span></div>")
                rows_html = (
                    f"<div class='bidrow'><span>Early / Late / Night</span><span>{cnt['early']} / {cnt['late']} / {cnt['night']}</span></div>"
                    f"<div class='bidrow'><span>7-day max (cap 60 h)</span><span>{_fmt_hm(cum['7d']['max'])}</span></div>"
                    f"<div class='bidrow'><span>14-day max (cap 105 h)</span><span>{_fmt_hm(cum['14d']['max'])}</span></div>"
                    f"<div class='bidrow'><span>28-day max (cap 210 h)</span><span>{_fmt_hm(cum['28d']['max'])}</span></div>"
                    f"<div class='bidrow'><span>Days off \u00b7 longest duty streak</span><span>{do['off_days']} \u00b7 {do['max_duty_run']}d</span></div>"
                    f"<div class='bidrow'><span>Off-day rest (\u226534h \u00b7 2 nights)</span><span>{off_rest_txt}</span></div>"
                    f"<div class='bidrow'><span>Mandatory days off (8.2.17)</span><span>{mand_txt}</span></div>"
                    f"<div class='bidrow'><span>Days off \u00b7 last 3 periods (8.2.17.d)</span><span style='color:{davg_color};'>{davg_txt}</span></div>"
                )
                st.markdown(
                    f"<div class='card'>{rows_html}"
                    f"<div class='muted' style='font-size:11px;margin-top:4px;'>standby & duty days counted in full \u00b7 sick & annual leave count as days off</div>"
                    f"<div class='muted' style='font-size:11px;margin-top:2px;'>mandatory: {mand_line}</div>"
                    f"{davg_foot}"
                    f"<div class='muted' style='font-size:11px;margin-top:2px;'>(d) needs 3 finalized periods \u2014 finalize past rosters in Roster History.</div></div>",
                    unsafe_allow_html=True)
                for sev, msg in fdp["findings"]:
                    if sev == "violation":
                        st.markdown(f"<div style='font-size:12px;background:#2c1f1f;border:1px solid #ff5252;color:#ff8a8a;padding:8px;border-radius:8px;margin-bottom:6px;'>\u26a0\ufe0f {msg}</div>", unsafe_allow_html=True)
                    else:
                        st.markdown(f"<div style='font-size:12px;background:#33260f;border:1px solid #ffc107;color:#ffd54f;padding:8px;border-radius:8px;margin-bottom:6px;'>\u2139\ufe0f {msg}</div>", unsafe_allow_html=True)
            elif viewing_past:
                st.markdown(
                    "<div class='card' style='border-color:#607d8b;color:#9fb3c8;font-size:12.5px;'>"
                    "\u2139\ufe0f No archived roster saved for this period \u2014 finalize it in Roster History to see its FDP stats.</div>",
                    unsafe_allow_html=True)

            # --- cross-period cumulative (60/105/210 across 28-day period boundaries) ---
            if not viewing_past:
                xpc = cross_period_cumulative(st.session_state['username'], parsed_rows)
                xc = xpc["cumulative"]
                xpc_html = (
                    f"<div class='bidrow'><span>7-day max (cap 60 h)</span><span>{_fmt_hm(xc['7d']['max'])}</span></div>"
                    f"<div class='bidrow'><span>14-day max (cap 105 h)</span><span>{_fmt_hm(xc['14d']['max'])}</span></div>"
                    f"<div class='bidrow'><span>28-day max (cap 210 h)</span><span>{_fmt_hm(xc['28d']['max'])}</span></div>"
                )
                st.markdown(
                    f"<div class='card'><h5>\U0001f517 Cross-period cumulative</h5>{xpc_html}"
                    f"<div class='muted' style='font-size:11px;margin-top:2px;'>rolling 60/105/210 h across finalized periods + current roster (a window can span two 28-day periods)</div></div>",
                    unsafe_allow_html=True)
                for f in xpc["findings"]:
                    st.markdown(f"<div style='font-size:12px;background:#2c1f1f;border:1px solid #ff5252;color:#ff8a8a;padding:8px;border-radius:8px;margin-bottom:6px;'>\u26a0\ufe0f {f}</div>", unsafe_allow_html=True)

            # Estimated allowances: current roster only
            if viewing_past:
                st.markdown(_current_only_note("Estimated allowances"), unsafe_allow_html=True)
            elif est_sal:
                est_total = est_sal["meal_usd"] + est_sal["on_usd"]
                rows_html = ""
                for lv in est_sal.get("layover_allow", []):
                    city = STATION_INFO.get(lv["station"], (lv["station"],))[0]
                    rows_html += (
                        f"<div class='bidrow'><span>{city} ({lv['station']})"
                        f"<span class='muted'> \u00b7 {lv['nights']} night(s), {lv['meals']} meals</span></span>"
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
                    f"<div class='muted'>No roster parsed \u2014 paste your roster above.</div></div>",
                    unsafe_allow_html=True)
        # ---------- CENTER: CALENDAR + LAYOVER INTEL ----------
        with main_col:
            st.markdown("#### Main Roster Calendar View")
            with st.expander("📝 Paste Roster (Instant Parse)"):
                roster_input = st.text_area("Paste Raw Roster Here", value=st.session_state['current_roster'], height=120)
                rp1, rp2 = st.columns([2, 1])
                if rp1.button("Auto-Process Roster", use_container_width=True):
                    if roster_input.strip():
                        save_roster_to_db(st.session_state['username'], roster_input)
                        st.session_state['current_roster'] = roster_input
                        st.success("Roster updated!")
                        st.rerun()
                    else:
                        st.warning("Please paste roster text.")
                if rp2.button("🗑 Clear roster", use_container_width=True):
                    save_roster_to_db(st.session_state['username'], '')
                    st.session_state['current_roster'] = ''
                    st.session_state.pop('cal_period_sel', None)
                    st.success("Roster cleared — paste your real roster to replace it.")
                    st.rerun()

            # ---------- ROSTER HISTORY: finalize ended periods ----------
            if valid_dates_all:
                hist_rows = load_roster_history(st.session_state['username'])
                cur_start = roster_period_of_roster(valid_dates_all)
                cur_end = cur_start + timedelta(days=ROSTER_PERIOD_DAYS)
                cur_finalized = next((h for h in hist_rows
                                      if h["period_start"] == cur_start and h["finalized"]), None)
                period_ended = datetime.now().date() >= cur_end

                if period_ended and not cur_finalized:
                    st.markdown(
                        f"<div style='padding:10px 14px;border-radius:8px;background:#33260f;"
                        f"border:1px solid #ffc107;color:#ffd54f;font-size:13px;margin-bottom:10px;'>"
                        f"⏰ Roster period <b>{cur_start.strftime('%d %b')} – {(cur_end - timedelta(days=1)).strftime('%d %b %Y')}</b> "
                        f"has ended. Paste the <b>performed</b> roster (from the portal's performed view) to finalize it "
                        f"— history & rolling checks use performed rosters, not the plan.</div>",
                        unsafe_allow_html=True)
                    with st.expander("📥 Finalize this period (paste performed roster)", expanded=True):
                        perf_fin = st.text_area("Performed roster (this period)",
                                                value=(cur_finalized["performed_text"] if cur_finalized else ""),
                                                height=140,
                                                key="finalize_perf_input")
                        if st.button("Save as performed & finalize", key="finalize_btn"):
                            if perf_fin.strip():
                                save_roster_history(st.session_state['username'], cur_start,
                                                    published_text=st.session_state['current_roster'],
                                                    performed_text=perf_fin, finalized=True)
                                st.success(f"Period {cur_start.strftime('%d %b')} finalized — rolling checks now use the performed roster.")
                                st.rerun()
                            else:
                                st.warning("Paste the performed roster text first.")

            # Roster history — paste & finalize (period auto-detected), list & delete
            with st.expander("🗂 Roster History"):
                hist_rows = load_roster_history(st.session_state['username'])

                # One-shot flags from the ✏️ / 💾 buttons (both live below the
                # paste box, so they stash a plain flag and rerun; the flag is
                # applied here, BEFORE the text area is instantiated — the only
                # safe time to rewrite a widget-backed key).
                _edit_target = st.session_state.pop('_hist_edit_target', None)
                if _edit_target is not None:
                    _h_edit = next((h for h in hist_rows if h["period_start"] == _edit_target), None)
                    if _h_edit is not None:
                        st.session_state['hist_perf_input'] = (_h_edit["performed_text"]
                                                               or _h_edit["published_text"] or "")
                if st.session_state.pop('_hist_clear_flag', False):
                    st.session_state['hist_perf_input'] = ''

                st.markdown(
                    "<div class='muted' style='margin-bottom:8px;'>Paste your <b>performed</b> rosters (portal's performed view) below — the 28-day period is <b>auto-detected</b> from the dates you paste, so there's nothing to select. Every saved period is listed with ✏️ to load it back for editing and 🗑 to delete it. These performed rosters feed the 8.2.17(d) average &amp; rolling checks and, once a full 1st–end calendar month is performed, the Salary Calculator. Periods before 2026 are auto-removed.</div>",
                    unsafe_allow_html=True)

                def _summarize(h):
                    if not (h["performed_text"] or "").strip():
                        return "saved, no performed text"
                    rows = parse_roster_text(h["performed_text"])
                    dates = [r["DateObj"].date() for r in rows if r.get("DateObj")]
                    days = len(set(dates))
                    span = (f"{min(dates).strftime('%d %b')}–{max(dates).strftime('%d %b')}"
                            if dates else "no dates")
                    tag = "⚠️ partial" if 0 < days < 14 else ("✅ finalized" if h["finalized"] else "⏳ not finalized")
                    return f"{tag} · {len(rows)} row(s) · {span}"

                saved = sorted([h for h in hist_rows if h["period_start"] is not None
                                and ((h["performed_text"] or "").strip()
                                     or (h["published_text"] or "").strip())],
                               key=lambda h: h["period_start"], reverse=True)
                if saved:
                    for h in saved:
                        s = h["period_start"]
                        last = s + timedelta(days=ROSTER_PERIOD_DAYS - 1)
                        sc1, sc2, sc3 = st.columns([6, 0.6, 0.6])
                        with sc1:
                            st.markdown(
                                f"<div class='bidrow'><span>{s.strftime('%d %b')} – {last.strftime('%d %b %Y')}</span>"
                                f"<span>{_summarize(h)}</span></div>", unsafe_allow_html=True)
                        with sc2:
                            if st.button("✏️", key=f"hist_edit_{s.strftime('%Y%m%d')}",
                                         help=f"Load the {s.strftime('%d %b')} performed roster into the box below to edit"):
                                st.session_state['_hist_edit_target'] = s
                                st.rerun()
                        with sc3:
                            if st.button("🗑", key=f"hist_del_{s.strftime('%Y%m%d')}",
                                         help=f"Delete the {s.strftime('%d %b')} period"):
                                delete_roster_history(st.session_state['username'], s)
                                st.session_state.pop('cal_period_sel', None)
                                st.success(f"Period {s.strftime('%d %b')} deleted from Roster History.")
                                st.rerun()
                else:
                    st.markdown("<div class='muted' style='margin-bottom:8px;'>No saved periods yet.</div>",
                                unsafe_allow_html=True)

                man_in = st.text_area(
                    "Paste performed roster (period auto-detected)", height=140,
                    key="hist_perf_input",
                    placeholder="Paste a performed roster here — the app figures out which 28-day period it belongs to…")

                _det_rows = parse_roster_text(man_in) if man_in.strip() else []
                _det_dates = [r["DateObj"].date() for r in _det_rows if r.get("DateObj")]
                _detected = roster_period_of_roster(_det_dates) if _det_dates else None
                if _det_dates and _detected is not None:
                    _det_label = (f"{_detected.strftime('%d %b')} – "
                                  f"{(_detected + timedelta(days=ROSTER_PERIOD_DAYS - 1)).strftime('%d %b %Y')}")
                    if _detected < datetime(2026, 1, 1).date():
                        st.markdown(
                            f"<div style='font-size:12px;background:#331414;border:1px solid #ff5252;color:#ff8a8a;padding:8px;border-radius:8px;margin-bottom:6px;'>"
                            f"📍 Detected period <b>{_det_label}</b> is before 2026 — pre-2026 history is auto-removed on load, so it won't persist. The app only supports 2026 onward.</div>",
                            unsafe_allow_html=True)
                    else:
                        st.markdown(
                            f"<div style='font-size:12px;background:#12301f;border:1px solid #4caf50;color:#a5d6a7;padding:8px;border-radius:8px;margin-bottom:6px;'>"
                            f"📍 Detected period: <b>{_det_label}</b></div>",
                            unsafe_allow_html=True)
                elif man_in.strip():
                    st.markdown("<div class='muted' style='font-size:12px;margin-bottom:6px;'>⚠️ No dates detected in the pasted text — check it parses.</div>", unsafe_allow_html=True)

                if st.button("💾 Save as performed (finalize)", use_container_width=True, key="history_manual_btn"):
                    if not man_in.strip():
                        st.warning("Paste the performed roster text first.")
                    elif _detected is None:
                        st.warning("Couldn't detect a period from the pasted text — check the dates and try again.")
                    else:
                        save_roster_history(st.session_state['username'], _detected,
                                            performed_text=man_in, finalized=True)
                        _slabel = (f"{_detected.strftime('%d %b')} – "
                                   f"{(_detected + timedelta(days=ROSTER_PERIOD_DAYS - 1)).strftime('%d %b %Y')}")
                        st.session_state['_hist_clear_flag'] = True
                        st.success(f"Period {_slabel} finalized — rolling checks now use the performed roster.")
                        st.rerun()

            # 28-day roster period navigation (anchored 13 Jul 2026: 07 Sep–04 Oct is
            # current). State was resolved at the top of this tab into `_period_starts`,
            # so panels and calendar always agree; here we only render the controls.
            cal_rows = _cal_rows
            cal_dates = _cal_dates
            if cal_dates and _period_starts:
                periods = [(p, p + timedelta(days=ROSTER_PERIOD_DAYS - 1)) for p in _period_starts]
                starts = _period_starts
                t0 = _t0
                idx = starts.index(st.session_state['cal_period_sel'])

                def _period_label(d):
                    return (f"{d.strftime('%d %b')} – "
                            f"{(d + timedelta(days=ROSTER_PERIOD_DAYS - 1)).strftime('%d %b %Y')}"
                            + ("  ·  current" if d == t0 else ""))

                nav1, nav2, nav3, nav4 = st.columns([0.8, 4.4, 0.8, 1.4])
                with nav1:
                    st.button("‹", on_click=_nav_go, args=(-1,),
                              use_container_width=True, disabled=idx == 0)
                with nav2:
                    # Direct period picker (doubles as the label) — no more
                    # clicking the arrows all the way back to today.
                    st.selectbox("Roster period", starts, key="cal_period_sel",
                                 format_func=_period_label, label_visibility="collapsed")
                with nav3:
                    st.button("›", on_click=_nav_go, args=(1,),
                              use_container_width=True, disabled=idx == len(periods) - 1)
                with nav4:
                    if t0 in starts:
                        st.button("⟲ Current", on_click=_nav_go, args=("cur",),
                                  use_container_width=True,
                                  disabled=(st.session_state['cal_period_sel'] == t0))
                sel_span = periods[starts.index(st.session_state['cal_period_sel'])]
            else:
                sel_span = None

            # ---------- CALENDAR: read-only view vs EDIT-MODE grid ----------
            # Editing is allowed only for the CURRENT period (never archived/
            # performed periods, and never a past period being browsed).
            _edit_ok = (not viewing_past) and bool(cal_dates) and (sel_span is not None)
            if not _edit_ok:
                st.session_state['edit_mode'] = False
                st.session_state.pop('caledit_day', None)
                st.session_state.pop('caledit_target', None)
            if _edit_ok:
                e1, e2 = st.columns([5, 2])
                with e1:
                    st.markdown(
                        "<div class='muted' style='font-size:12px;padding-top:6px;'>"
                        "✏️ <b>Edit calendar</b> changes the <b>current roster</b> only — older/archived periods are read-only.</div>",
                        unsafe_allow_html=True)
                with e2:
                    _lbl = "✅ Done editing" if st.session_state.get('edit_mode') else "✏️ Edit calendar"
                    if st.button(_lbl, use_container_width=True, key="cedit_toggle"):
                        st.session_state['edit_mode'] = not st.session_state.get('edit_mode', False)
                        if not st.session_state['edit_mode']:
                            st.session_state.pop('caledit_day', None)
                            st.session_state.pop('caledit_target', None)
                        st.rerun()
            if st.session_state.get('edit_mode') and _edit_ok:
                _render_calendar_editor(parsed_rows, sel_span)
            else:
                st.markdown(f"<div class='card'>{build_calendar_html(cal_rows, span=sel_span)}</div>", unsafe_allow_html=True)

            # Flight & Layover Intel — grouped: each layover trip is ONE entry
            # (inbound + 🏨 + outbound); standalone turnarounds get their own entry.
            if viewing_past:
                st.markdown(_current_only_note("Flight & layover intel"), unsafe_allow_html=True)
            else:
                _duties = build_duties(parsed_rows)
                _trip_duties = set()
                _trips = []   # (layover, inbound, outbound)
                for lv in analytics["layovers"]:
                    if not lv["station"]:
                        continue
                    _inb, _outb = layover_flight_duties(parsed_rows, lv["station"], lv["date"], duties=_duties)
                    if _inb is not None:
                        _trip_duties.add(id(_inb))
                    if _outb is not None:
                        _trip_duties.add(id(_outb))
                    _trips.append((lv, _inb, _outb))

                intel_items = []   # (kind, payload, label, anchor_date)
                for lv, _inb, _outb in _trips:
                    city = STATION_INFO.get(lv["station"], (lv["station"],))[0]
                    _fns = []
                    for _du in (_inb, _outb):
                        if _du:
                            _fns += [s["flight"].replace(" ", "") for s in _du["sectors"]]
                    _fns = sorted(set(_fns))
                    fl_lbl = "/".join(_fns) if _fns else "?"
                    lo = (_inb["sectors"][0]["dep"].date()
                          if _inb and isinstance(_inb["sectors"][0].get("dep"), datetime) else None)
                    hi = (_outb["chocks_on"].date()
                          if _outb and isinstance(_outb.get("chocks_on"), datetime) else None)
                    if lo and hi and lo != hi:
                        span = f"{lo.strftime('%d')}\u2013{hi.strftime('%d %b')}"
                    elif hi:
                        span = hi.strftime("%d %b")
                    elif lo:
                        span = lo.strftime("%d %b")
                    else:
                        span = ""
                    _anchor = lo if lo else (lv["date"] if lv.get("date") else None)
                    _lbl = f"🏨 {fl_lbl} · {city} ({lv['station']})" + (f" — {span}" if span else "")
                    intel_items.append(("layover", lv, _lbl, _anchor))
                for du in _duties:
                    if id(du) in _trip_duties:
                        continue   # already shown inside its layover-trip entry
                    d0 = (du["sectors"][0]["dep"].date().strftime("%d %b")
                          if isinstance(du["sectors"][0].get("dep"), datetime) else "")
                    _anchor = (du["sectors"][0]["dep"].date()
                               if isinstance(du["sectors"][0].get("dep"), datetime) else None)
                    intel_items.append(("flight", du, f"✈ {du['label']}" + (f" — {d0}" if d0 else ""), _anchor))
                intel_items.sort(key=lambda it: (it[3] is None, it[3] or datetime.min.date()))
                if intel_items:
                    today = datetime.now().date()
                    def_idx = 0
                    for i, (kind, payload, _lbl, _anchor) in enumerate(intel_items):
                        if _anchor is not None and _anchor >= today:
                            def_idx = i
                            break
                    sel = st.selectbox("Flight & Layover Intel:", list(range(len(intel_items))), index=def_idx,
                                       format_func=lambda i: intel_items[i][2])
                    kind, payload, _lbl, _anchor = intel_items[sel]
                    if kind == "flight":
                        st.markdown(flight_intel_card(payload, parsed_rows), unsafe_allow_html=True)
                    else:
                        lv = payload
                        wx = fetch_station_weather(lv["station"])
                        info = STATION_INFO.get(lv["station"])
                        spots = (info[4] if info and info[4] else DEFAULT_SPOTS)
                        city = wx["city"] if wx else lv["station"]
                        apname = AIRPORT_NAME.get(lv["station"], "")
                        wx_html = (f"{wx['icon']} {wx['temp']}°C · {wx['desc']}" if wx and wx["temp"] is not None else "n/a")
                        lt_html = f"{wx['local_time']} ({wx['gmt']})" if wx else "-"
                        gt_html = f"{lv['ground_hrs']} hrs" if lv["ground_hrs"] else "-"
                        spots_html = "".join(f"<span class='spot'>{s}</span>" for s in spots)
                        ex = station_extra(lv["station"])
                        ex_html = ""
                        if ex:
                            _rows_x = []
                            if ex.get("cur"):
                                _rows_x.append(f"<div class='bidrow'><span>💱 Currency</span><span>{ex['cur']}"
                                               + (f" &nbsp;·&nbsp; {ex['fx']}" if ex.get("fx") else "") + "</span></div>")
                            if ex.get("plug"):
                                _rows_x.append(f"<div class='bidrow'><span>🔌 Plug</span><span>{plug_diagram_html(ex['plug'])}</span></div>")
                            if ex.get("visa_family"):
                                _rows_x.append(f"<div class='bidrow'><span>🛂 Partners/family</span><span>{ex['visa_family']}</span></div>")
                            if ex.get("transit"):
                                _rows_x.append(f"<div class='bidrow'><span>🚇 Trains/buses · paying</span><span>{ex['transit']}</span></div>")
                            tips = ex.get("tips") or []
                            if tips:
                                _rows_x.append(f"<div style='margin-top:4px;'><div class='muted' style='margin-bottom:4px;'>🧭 Crew tips</div>"
                                               + "".join(f"<span class='spot'>{t}</span>" for t in tips) + "</div>")
                            ex_html = (f"<div class='muted' style='margin:8px 0 4px;'>Crew info</div>"
                                       f"<div class='muted' style='font-size:10.5px;margin-bottom:4px;'>{VISA_DISCLAIMER}</div>"
                                       + "".join(_rows_x))
                        # merged flight section: inbound (to the layover) + outbound (back to CMB)
                        _lvd = lv.get("date") if lv.get("date") else None
                        _inb, _outb = (layover_flight_duties(parsed_rows, lv["station"], _lvd)
                                       if _lvd else (None, None))
                        flights_html = ""
                        if _inb or _outb:
                            _fl_bits = []
                            if _inb is not None:
                                _fl_bits.append("<div class='muted' style='font-size:11px;margin-bottom:2px;'>↘ Inbound</div>"
                                                + _duty_sectors_html(_inb, parsed_rows))
                            if _outb is not None:
                                _fl_bits.append("<div class='muted' style='font-size:11px;margin-bottom:2px;'>↗ Outbound</div>"
                                                + _duty_sectors_html(_outb, parsed_rows))
                            flights_html = (f"<div class='muted' style='margin:8px 0 4px;'>Flights</div>"
                                            + "".join(_fl_bits))
                        st.markdown(
                            f"<div class='card' style='border-color:#00bcd4;'>"
                            f"<h5>🏨 Layover Intel: {city} ({lv['station']})" + (f" — {lv['date'].strftime('%d %b')}" if lv['date'] else "") + "</h5>"
                            + (f"<div class='muted' style='margin-bottom:8px;'>✈ {apname}</div>" if apname else "")
                            + f"<div style='display:flex;gap:28px;font-size:13px;margin-bottom:10px;'>"
                            f"<div><div class='muted'>Weather (live)</div>{wx_html}</div>"
                            f"<div><div class='muted'>Local Time</div>{lt_html}</div>"
                            f"<div><div class='muted'>Ground Time</div>{gt_html}</div></div>"
                            + flights_html
                            + f"<div class='muted' style='margin-bottom:4px;'>Explore Spots</div>{spots_html}"
                            + ex_html + "</div>",
                            unsafe_allow_html=True)
                elif active_text:
                    st.info("No flights or layovers detected in this roster for Intel.")
                else:
                    st.info("Paste your roster above to populate the calendar, analytics and flight & layover intel.")
            # ---------- ROSTER GUARDIAN: FAU SOFT-RULES AUDIT ----------
            if viewing_past:
                st.markdown(_current_only_note("Roster Guardian"), unsafe_allow_html=True)
            else:
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
            if viewing_past:
                st.markdown(_current_only_note("Live flight monitoring"), unsafe_allow_html=True)
            else:
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
                            # FDP & rest impact for delayed/diverted flights
                            impact_note = None
                            if telemetry.get("severity") in ("delayed", "diverted"):
                                delay_guess = None
                                m = re.search(r'by ~(\d+) min', telemetry.get("status_message", ""))
                                if m:
                                    delay_guess = int(m.group(1))
                                impact_note = delay_impact_note(parsed_rows, flight["flight_no"],
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
                                                         "impact_note": impact_note,
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
                        if df.get("impact_note"):
                            impact_bad = ("⚠️" in df["impact_note"]) or ("BELOW" in df["impact_note"])
                            r_bc = "#ff5252" if impact_bad else "#4caf50"
                            r_tc = "#ff8a8a" if impact_bad else "#a5d6a7"
                            extra += (f"<div style='margin-top:8px;padding:8px;border-radius:6px;background:#131f2b;"
                                      f"border:1px solid {r_bc};color:{r_tc};font-size:11.5px;'>{df['impact_note']}</div>")
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
            # --- SALARY HISTORY: per-month performed rosters (manual pin, or auto-pull) ---
            today_ym = (datetime.now().year, datetime.now().month)
            sh = load_salary_history(st.session_state['username'])
            with st.expander("🗓 Salary History (per-month)", expanded=False):
                # One-shot flags from the ✏️ / 💾 buttons below: applied here,
                # BEFORE the text area is instantiated (the only safe time to
                # rewrite a widget-backed key). `_sh_edit_text` carries the text
                # the ✏️ button wants loaded (manual entry or a pulled month).
                _sh_edit_text = st.session_state.pop('_sh_edit_text', None)
                if _sh_edit_text is not None:
                    st.session_state['salary_hist_input'] = _sh_edit_text
                if st.session_state.pop('_sh_clear_flag', False):
                    st.session_state['salary_hist_input'] = ''

                st.markdown(
                    "<div class='muted' style='margin-bottom:6px;'>Pin a whole calendar month's <b>performed</b> "
                    "roster here to lock in its payslip — the month is <b>auto-detected</b> from the dates you paste, "
                    "so there's nothing to select. Months already <b>finalized</b> in the Dashboard's 🗂 Roster "
                    "History are pulled automatically — but only once the <b>full month (1st – end)</b> is performed. "
                    "A half-month, or a month still on the live roster, shows \u201cno data\u201d — never a guess.</div>",
                    unsafe_allow_html=True)

                saved_sh = sorted([(mk, e) for mk, e in sh.items() if e.get("text", "").strip()],
                                  key=lambda kv: kv[0], reverse=True)
                if saved_sh:
                    for mk, entry in saved_sh:
                        try:
                            ym = (int(mk[:4]), int(mk[5:7]))
                        except ValueError:
                            continue
                        mlabel = datetime(ym[0], ym[1], 1).strftime("%B %Y")
                        rows = parse_roster_text(entry.get("text", ""))
                        dates = [r["DateObj"].date() for r in rows if r.get("DateObj")]
                        nf = sum(1 for r in rows if r["Type"] == "FLIGHT")
                        span = (f"{min(dates).strftime('%d %b')}\u2013{max(dates).strftime('%d %b')}"
                                if dates else "no dates")
                        s1, s2, s3, s4 = st.columns([6, 0.7, 0.7, 0.7])
                        with s1:
                            st.markdown(
                                f"<div class='bidrow'><span>📌 {mlabel}</span>"
                                f"<span>✅ {nf} flight(s) · {span}</span></div>",
                                unsafe_allow_html=True)
                        with s2:
                            if st.button("✏️", key=f"sh_edit_{mk}",
                                         help=f"Load {mlabel} into the box below to edit"):
                                st.session_state['_sh_edit_text'] = entry.get("text", "")
                                st.rerun()
                        with s3:
                            if st.button("🗑", key=f"sh_del_{mk}",
                                         help=f"Delete {mlabel} from salary history"):
                                delete_salary_history(st.session_state['username'], mk)
                                st.success(f"{mlabel} removed from salary history.")
                                st.rerun()
                        with s4:
                            if st.button("👁", key=f"sh_show_{mk}",
                                         help=f"Show {mlabel} in the payslip"):
                                _show_rows, _ = salary_month_rows(st.session_state['username'], ym, parsed_rows)
                                if any(r["Type"] == "FLIGHT" for r in _show_rows):
                                    st.session_state['salary_month_pick'] = mlabel
                                    st.rerun()
                                else:
                                    st.warning(mlabel + " has no full performed month yet — save it here, or finalize that month's periods in the Dashboard's Roster History.")
                else:
                    st.markdown("<div class='muted' style='margin-bottom:8px;'>No saved months yet.</div>",
                                unsafe_allow_html=True)

                # --- months AUTO-PULLED from the Dashboard's finalized Roster
                # History (no manual pin) — shown so people can see what's being
                # pulled and choose to pin (✏️ → edit & save) or remove (🗑). ---
                pulled = pulled_salary_months(st.session_state['username'], parsed_rows)
                if pulled:
                    st.markdown("<div class='muted' style='font-size:11.5px;margin:10px 0 4px;'>🔗 Pulled from performed rosters (Roster History):</div>",
                                unsafe_allow_html=True)
                    for ym, prows in pulled:
                        mk = _month_key(ym)
                        mlabel = datetime(ym[0], ym[1], 1).strftime("%B %Y")
                        dates = [r["DateObj"].date() for r in prows if r.get("DateObj")]
                        nf = sum(1 for r in prows if r["Type"] == "FLIGHT")
                        span = (f"{min(dates).strftime('%d %b')}\u2013{max(dates).strftime('%d %b')}"
                                if dates else "no dates")
                        s1, s2, s3, s4 = st.columns([6, 0.7, 0.7, 0.7])
                        with s1:
                            st.markdown(
                                f"<div class='bidrow'><span>🔗 {mlabel}</span>"
                                f"<span>{nf} flight(s) · {span}</span></div>",
                                unsafe_allow_html=True)
                        with s2:
                            if st.button("✏️", key=f"sh_pull_edit_{mk}",
                                         help=f"Load {mlabel} into the box to edit & save as a pinned month"):
                                st.session_state['_sh_edit_text'] = _rows_to_roster_text(prows)
                                st.rerun()
                        with s3:
                            if st.button("🗑", key=f"sh_pull_del_{mk}",
                                         help=f"Stop pulling {mlabel} from performed rosters (remove it from salary)"):
                                exclude_salary_month(st.session_state['username'], mk)
                                st.success(f"{mlabel} will no longer be pulled into the salary tab.")
                                st.rerun()
                        with s4:
                            if st.button("👁", key=f"sh_pull_show_{mk}",
                                         help=f"Show {mlabel} in the payslip"):
                                _show_rows, _ = salary_month_rows(st.session_state['username'], ym, parsed_rows)
                                if any(r["Type"] == "FLIGHT" for r in _show_rows):
                                    st.session_state['salary_month_pick'] = mlabel
                                    st.rerun()
                                else:
                                    st.warning(mlabel + " has no full performed month yet.")

                # --- months the user removed from salary (excluded) ---
                hidden = sorted([(mk, e) for mk, e in sh.items()
                                 if not e.get("text", "").strip() and e.get("excluded")],
                                key=lambda kv: kv[0])
                if hidden:
                    htxt = ""
                    for mk, _e in hidden:
                        try:
                            hym = (int(mk[:4]), int(mk[5:7]))
                        except ValueError:
                            continue
                        hlabel = datetime(hym[0], hym[1], 1).strftime("%B %Y")
                        htxt += f"<span style='margin-right:8px;white-space:nowrap;'>{hlabel} <span style='color:#8aa0b8;'>· hidden from salary</span></span>"
                    st.markdown(
                        f"<div class='muted' style='font-size:11.5px;margin:8px 0 4px;'>🙈 Removed from salary: {htxt}"
                        f"</div>",
                        unsafe_allow_html=True)
                    for mk, _e in hidden:
                        if st.button("↩", key=f"sh_restore_{mk}",
                                     help=f"Restore {datetime(int(mk[:4]), int(mk[5:7]), 1).strftime('%B %Y')} to salary"):
                            restore_salary_month(st.session_state['username'], mk)
                            st.rerun()

                sh_in = st.text_area(
                    "Paste performed month roster (month auto-detected)", height=140,
                    key="salary_hist_input",
                    placeholder="Paste a full month's performed roster here — the app figures out which month it belongs to…")

                _det_rows = parse_roster_text(sh_in) if sh_in.strip() else []
                _det_dates = [r["DateObj"].date() for r in _det_rows if r.get("DateObj")]
                _det_ym = _month_of_roster(_det_dates) if _det_dates else None
                if _det_dates and _det_ym is not None:
                    _det_label = datetime(_det_ym[0], _det_ym[1], 1).strftime("%B %Y")
                    if _det_ym < (2026, 1):
                        st.markdown(
                            f"<div style='font-size:12px;background:#331414;border:1px solid #ff5252;color:#ff8a8a;padding:8px;border-radius:8px;margin-bottom:6px;'>"
                            f"📍 Detected month <b>{_det_label}</b> is before 2026 — pre-2026 history is auto-removed on load, so it won't persist. The app only supports 2026 onward.</div>",
                            unsafe_allow_html=True)
                    else:
                        st.markdown(
                            f"<div style='font-size:12px;background:#12301f;border:1px solid #4caf50;color:#a5d6a7;padding:8px;border-radius:8px;margin-bottom:6px;'>"
                            f"📍 Detected month: <b>{_det_label}</b></div>",
                            unsafe_allow_html=True)
                elif sh_in.strip():
                    st.markdown("<div class='muted' style='font-size:12px;margin-bottom:6px;'>⚠️ No dates detected in the pasted text — check it parses.</div>", unsafe_allow_html=True)

                if st.button("💾 Save month", use_container_width=True, key="sh_save"):
                    if not sh_in.strip():
                        st.warning("Paste the performed roster text first.")
                    elif _det_ym is None:
                        st.warning("Couldn't detect a month from the pasted text — check the dates and try again.")
                    else:
                        _mlabel = datetime(_det_ym[0], _det_ym[1], 1).strftime("%B %Y")
                        save_salary_history(st.session_state['username'], _month_key(_det_ym), sh_in)
                        st.session_state['_sh_clear_flag'] = True
                        st.success(f"{_mlabel} saved to salary history.")
                        st.rerun()

            # --- performed roster input (salary is based on the PERFORMED month, not the live roster) ---
            if 'performed_roster' not in st.session_state:
                st.session_state['performed_roster'] = load_performed_roster(st.session_state['username'])
            perf_saved = st.session_state.get('performed_roster', '')
            with st.expander("📋 Performed Roster (paste here) — " + ("saved ✅" if perf_saved.strip() else "empty"),
                             expanded=True):
                st.markdown("<div class='muted' style='margin-bottom:6px;'>Salary is calculated per <b>calendar month (1st – end)</b>, and only when the <b>whole month</b> is performed — a half-month shows \u201cno data\u201d. Paste your <b>performed</b> roster from the crew portal; it's used here only if it covers the full month. Saved separately from your live roster.</div>", unsafe_allow_html=True)
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

            # Salary is per CALENDAR MONTH (1st–end). ONLY a full performed month
            # (1st through the last day) is computed — a partial month or one
            # still on the live roster shows "no data" instead of guessing from
            # the wrong roster. Every month Jan 2026 → now is offered so a
            # month with no data yet is visible as such.
            months = _months_from_2026(today_ym)
            perf_rows = []
            sel_month = None
            src = "none"
            if months:
                labels = [datetime(y, m, 1).strftime("%B %Y") for y, m in months]
                def _mcount(ym):
                    rows, _ = salary_month_rows(st.session_state['username'], ym, parsed_rows)
                    return sum(1 for r in rows if r["Type"] == "FLIGHT")
                full_months = [ym for ym in months if _mcount(ym) > 0]
                # Default to the latest COMPLETED month with data, else the latest
                # completed month — never the ongoing month (it can't be full yet).
                if full_months:
                    default_ym = max(full_months)
                else:
                    _prev_ym = _prev_months(today_ym, 2)[0]
                    default_ym = _prev_ym if _prev_ym in months else months[-1]
                _pick_label = st.session_state.get('salary_month_pick')
                if _pick_label not in labels:
                    st.session_state['salary_month_pick'] = labels[months.index(default_ym)]
                pick = st.selectbox("Salary month (1st – end of month)", labels,
                                    key="salary_month_pick")
                sel_month = months[labels.index(pick)]
                perf_rows, src = salary_month_rows(st.session_state['username'], sel_month, parsed_rows)
            src_caption = {
                "manual": "📌 From your saved salary history for this month.",
                "history": "🗂 Auto-pulled from the Dashboard's finalized Roster History — the full performed month.",
                "slot": "📋 From the performed-roster slot above (full month).",
                "ongoing": "⏳ This month is still in progress — salary only computes completed months.",
            }.get(src, "")
            flights_exist = any(r["Type"] == "FLIGHT" for r in perf_rows)
            if src_caption:
                st.markdown(f"<div class='muted' style='font-size:11.5px;margin-bottom:6px;'>{src_caption}</div>",
                            unsafe_allow_html=True)
            if not flights_exist:
                _mlabel = datetime(sel_month[0], sel_month[1], 1).strftime("%B %Y") if sel_month else "this month"
                if src == "ongoing":
                    st.info(f"⏳ {_mlabel} is still in progress — salary only computes a month once it has ended and the full 1st – end performed roster is available.")
                else:
                    st.info(f"No full performed month for {_mlabel} yet — salary only computes once the whole month (1st – end) is performed. Finalize that month's 28-day periods in the Dashboard's Roster History, or save the full month in 🗓 Salary History.")
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

    # ================= 📊 SALARY ANALYTICS PAGE =================
    with page_analytics:
        st.markdown("#### 📊 Salary Analytics")
        st.markdown("<div class='muted' style='margin-bottom:10px;'>Full performed months only (1st – end), from finalized Roster History or saved salary history — partial months are excluded. Charts use your saved crew profile.</div>", unsafe_allow_html=True)

        a_saved = load_profile(st.session_state['username'])
        a_prof = {
            "cat": a_saved.get("cat", "C3") if a_saved.get("cat", "C3") in HOURLY_PAY else "C3",
            "rate": float(a_saved.get("rate", 318.56) or 318.56),
            "schblk_min": parse_hhmm_minutes(a_saved.get("schblk", "70h 00m")) or 4200,
            "festival": bool(a_saved.get("festival", False)),
            "basic": float(a_saved.get("basic", 0.0) or 0.0),
            "crge": float(a_saved.get("crge", 10000.0) or 0.0),
            "transport": float(a_saved.get("transport", 1000.0) or 0.0),
            "medical": float(a_saved.get("medical", 500.0) or 0.0),
            "fau": float(a_saved.get("fau", 2100.0) or 0.0),
            "stamp": float(a_saved.get("stamp", 0.0) or 0.0),
            "apiit": float(a_saved.get("apiit", 0.0) or 0.0),
            "epf_pct": float(a_saved.get("epf_pct", 10.0) or 10.0),
            "fbpp_overrides": a_saved.get("fbpp_overrides", {}) or {},
        }
        a_months = salary_available_months(st.session_state['username'], parsed_rows)
        if not a_months:
            st.info("No salary months yet — finalize past rosters in the Dashboard's Roster History, or save months in the Salary Calculator.")
        else:
            a_rows = []
            for _ym in a_months:
                _mrows, _src = salary_month_rows(st.session_state['username'], _ym, parsed_rows)
                if not any(r["Type"] == "FLIGHT" for r in _mrows):
                    continue
                _s = compute_salary(_mrows, a_prof)
                _n = sum(1 for r in _mrows if r["Type"] == "FLIGHT")
                a_rows.append({
                    "Month": datetime(_ym[0], _ym[1], 1).strftime("%b %y"),
                    "Net (Rs)": round(_s["net"], 2),
                    "Earnings (Rs)": round(_s["earnings"], 2),
                    "Productivity (Rs)": round(_s["productivity_rs"], 2),
                    "Premium (Rs)": round(_s["premium"], 2),
                    "FBPP (Rs)": round(_s["fbpp_rs"], 2),
                    "Acting (Rs)": round(_s["act_pay_rs"], 2),
                    "Block hours": round(_s["block_min"] / 60, 2),
                    "Meals ($)": _s["meal_usd"],
                    "Layover ($)": _s["on_usd"],
                    "T/A ($)": _s["ta_on_usd"],
                    "Allowance ($)": _s["allow_usd_total"],
                    "Sectors": _n,
                    "Layover nights": _s["l_nights"],
                    "Source": {"manual": "pinned", "history": "auto", "slot": "slot"}.get(_src, _src),
                })
            if not a_rows:
                st.info("No complete months with flights found yet.")
            else:
                a_df = pd.DataFrame(a_rows)
                a_df["Month"] = pd.Categorical(a_df["Month"], categories=[r["Month"] for r in a_rows], ordered=True)
                a_df = a_df.sort_values("Month")

                c1, c2, c3, c4 = st.columns(4)
                with c1:
                    st.metric("Months tracked", len(a_df))
                with c2:
                    st.metric("Best month (net)", a_df.loc[a_df["Net (Rs)"].idxmax(), "Month"])
                with c3:
                    st.metric("Avg net / month", f"Rs {a_df['Net (Rs)'].mean():,.0f}")
                with c4:
                    st.metric("Total net", f"Rs {a_df['Net (Rs)'].sum():,.0f}")

                a_df["Δ Net (Rs)"] = a_df["Net (Rs)"].diff()

                if go is not None:
                    PAL = {"cyan": "#22d3ee", "green": "#34d399", "amber": "#fbbf24",
                           "pink": "#f472b6", "violet": "#a78bfa", "blue": "#60a5fa"}
                    FONT = dict(family="Segoe UI, Arial, sans-serif", size=12, color="#9fb3c8")

                    def _style_axes(fig, height=300, legend=True):
                        fig.update_layout(
                            height=height, margin=dict(l=10, r=10, t=26, b=10),
                            paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                            font=FONT, showlegend=legend, hovermode="x unified",
                            legend=dict(orientation="h", yanchor="bottom", y=1.0, xanchor="left",
                                        x=0, font=dict(size=11)))
                        fig.update_xaxes(showgrid=False, zeroline=False, linecolor="rgba(159,179,200,0.25)")
                        fig.update_yaxes(showgrid=True, gridcolor="rgba(159,179,200,0.12)", zeroline=False)
                        return fig

                    # 1 · Net trend — smooth area line
                    net = go.Figure(go.Scatter(
                        x=a_df["Month"], y=a_df["Net (Rs)"], mode="lines+markers",
                        line=dict(color=PAL["cyan"], width=2.5, shape="spline", smoothing=0.6),
                        marker=dict(size=7, color=PAL["cyan"], line=dict(width=2, color="#0b1622")),
                        fill="tozeroy", fillcolor="rgba(34,211,238,0.12)",
                        hovertemplate="%{x}<br>Net Rs %{y:,.0f}<extra></extra>"))
                    _style_axes(net, height=300, legend=False)
                    st.markdown("##### Net salary by month")
                    st.plotly_chart(net, width="stretch")

                    # 2 · Composition donut + block/sectors dual axis, side by side
                    cL, cR = st.columns([1, 1])
                    with cL:
                        comp = [("Productivity", "Productivity (Rs)", PAL["cyan"]),
                                ("Premium", "Premium (Rs)", PAL["green"]),
                                ("FBPP", "FBPP (Rs)", PAL["pink"]),
                                ("Acting", "Acting (Rs)", PAL["violet"])]
                        keep = [(l, a_df[c].sum(), col) for l, c, col in comp if a_df[c].sum() > 0]
                        if keep:
                            donut = go.Figure(go.Pie(
                                labels=[k[0] for k in keep], values=[k[1] for k in keep],
                                hole=0.62, marker=dict(colors=[k[2] for k in keep],
                                                       line=dict(color="#0b1622", width=2)),
                                textinfo="label+percent", textfont=dict(size=11, color="#c9d6e3"),
                                hovertemplate="%{label}<br>Rs %{value:,.0f}<extra></extra>"))
                            donut.update_layout(
                                height=300, margin=dict(l=10, r=10, t=26, b=10),
                                paper_bgcolor="rgba(0,0,0,0)", font=FONT, showlegend=False,
                                annotations=[dict(
                                    text=f"Rs {sum(k[1] for k in keep):,.0f}"
                                         f"<br><span style='font-size:11px;color:#9fb3c8'>total earnings</span>",
                                    x=0.5, y=0.5, showarrow=False, font=dict(size=15, color="#e6edf3"))])
                            st.markdown("##### Earnings composition")
                            st.plotly_chart(donut, width="stretch")
                        else:
                            st.info("No earnings components to show yet.")
                    with cR:
                        bh = go.Figure()
                        bh.add_trace(go.Scatter(
                            x=a_df["Month"], y=a_df["Block hours"], name="Block hours",
                            mode="lines+markers", line=dict(color=PAL["blue"], width=2.5),
                            marker=dict(size=7, line=dict(width=2, color="#0b1622")), yaxis="y",
                            hovertemplate="%{x}<br>Block %{y:.1f} h<extra></extra>"))
                        bh.add_trace(go.Scatter(
                            x=a_df["Month"], y=a_df["Sectors"], name="Sectors",
                            mode="lines+markers", line=dict(color=PAL["amber"], width=2.5, dash="dot"),
                            marker=dict(size=7, line=dict(width=2, color="#0b1622")), yaxis="y2",
                            hovertemplate="%{x}<br>%{y} sectors<extra></extra>"))
                        bh.update_layout(
                            height=300, margin=dict(l=10, r=10, t=26, b=10),
                            paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                            font=FONT, hovermode="x unified",
                            legend=dict(orientation="h", yanchor="bottom", y=1.0, xanchor="left",
                                        x=0, font=dict(size=11)),
                            yaxis=dict(title=dict(text="hours", font=dict(color=PAL["blue"], size=11)),
                                       gridcolor="rgba(159,179,200,0.12)", zeroline=False),
                            yaxis2=dict(title=dict(text="sectors", font=dict(color=PAL["amber"], size=11)),
                                        overlaying="y", side="right", showgrid=False))
                        bh.update_xaxes(showgrid=False, zeroline=False, linecolor="rgba(159,179,200,0.25)")
                        st.markdown("##### Block hours & sectors")
                        st.plotly_chart(bh, width="stretch")

                    # 3 · Allowances — clean lines
                    allow = go.Figure()
                    for col, name, color in [("Meals ($)", "Meals", PAL["cyan"]),
                                             ("Layover ($)", "Layover", PAL["violet"]),
                                             ("T/A ($)", "T/A overnight", PAL["pink"])]:
                        allow.add_trace(go.Scatter(
                            x=a_df["Month"], y=a_df[col], name=name, mode="lines+markers",
                            line=dict(color=color, width=2.5),
                            marker=dict(size=7, line=dict(width=2, color="#0b1622")),
                            hovertemplate="%{x}<br>" + name + " $%{y:,.0f}<extra></extra>"))
                    _style_axes(allow, height=280)
                    st.markdown("##### Allowances (USD)")
                    st.plotly_chart(allow, width="stretch")
                else:
                    # Plotly not installed — native Streamlit area/line fallback (no bars).
                    st.markdown("##### Net salary by month")
                    st.area_chart(a_df.set_index("Month")[["Net (Rs)"]], height=300)
                    st.markdown("##### Earnings composition (Rs)")
                    st.area_chart(a_df.set_index("Month")[["Productivity (Rs)", "Premium (Rs)",
                                                            "FBPP (Rs)", "Acting (Rs)"]], height=300)
                    st.markdown("##### Block hours & sectors")
                    st.line_chart(a_df.set_index("Month")[["Block hours", "Sectors"]], height=300)
                    st.markdown("##### Allowances (USD)")
                    st.line_chart(a_df.set_index("Month")[["Meals ($)", "Layover ($)", "T/A ($)"]], height=280)

                # 4 · Comparison table with MoM delta arrows
                st.markdown("##### Monthly comparison")
                _rows_html = ""
                for _, r in a_df.iterrows():
                    d = r["Δ Net (Rs)"]
                    if pd.isna(d):
                        dcell = "<span style='color:#8aa0b8;'>—</span>"
                    else:
                        dcell = (f"<span style='color:{'#34d399' if d >= 0 else '#f87171'};'>"
                                 f"{'▲ +' if d >= 0 else '▼ '}{abs(d):,.0f}</span>")
                    src_badge = {"pinned": "📌 pinned", "auto": "🗂 auto", "slot": "📋 slot"}.get(r["Source"], r["Source"])
                    _rows_html += (
                        f"<tr>"
                        f"<td style='padding:6px 10px;border-bottom:1px solid #223144;'>{r['Month']}</td>"
                        f"<td style='padding:6px 10px;border-bottom:1px solid #223144;text-align:right;font-weight:700;color:#4caf50;'>Rs {r['Net (Rs)']:,.0f}</td>"
                        f"<td style='padding:6px 10px;border-bottom:1px solid #223144;text-align:right;'>{dcell}</td>"
                        f"<td style='padding:6px 10px;border-bottom:1px solid #223144;text-align:right;'>{r['Block hours']:,.1f}h</td>"
                        f"<td style='padding:6px 10px;border-bottom:1px solid #223144;text-align:right;'>{int(r['Sectors'])}</td>"
                        f"<td style='padding:6px 10px;border-bottom:1px solid #223144;text-align:right;'>{int(r['Layover nights'])}</td>"
                        f"<td style='padding:6px 10px;border-bottom:1px solid #223144;text-align:right;'>${r['Allowance ($)']:,.0f}</td>"
                        f"<td style='padding:6px 10px;border-bottom:1px solid #223144;'>{src_badge}</td>"
                        f"</tr>")
                _table_html = (
                    "<div class='card' style='padding:8px 10px;'>"
                    "<table style='width:100%;border-collapse:collapse;font-size:13px;'>"
                    "<thead><tr style='color:#9fb3c8;font-size:11px;text-transform:uppercase;letter-spacing:.4px;'>"
                    "<th style='text-align:left;padding:6px 10px;'>Month</th>"
                    "<th style='text-align:right;padding:6px 10px;'>Net (Rs)</th>"
                    "<th style='text-align:right;padding:6px 10px;'>Δ vs prev</th>"
                    "<th style='text-align:right;padding:6px 10px;'>Block h</th>"
                    "<th style='text-align:right;padding:6px 10px;'>Sectors</th>"
                    "<th style='text-align:right;padding:6px 10px;'>L/O nights</th>"
                    "<th style='text-align:right;padding:6px 10px;'>Allowance ($)</th>"
                    "<th style='text-align:left;padding:6px 10px;'>Source</th>"
                    "</tr></thead><tbody>"
                    + _rows_html +
                    "</tbody></table></div>")
                st.markdown(_table_html, unsafe_allow_html=True)
                st.markdown("<div class='muted' style='margin-top:8px;'>⚠️ Estimate from your saved profile & rostered times — refer to official payslips for final figures.</div>", unsafe_allow_html=True)

    # ================= \u23f1 FDP CALCULATOR PAGE =================
    with page_fdp:
        st.markdown("#### \u23f1 FDP Calculator")
        st.markdown(
            "<div class='muted' style='margin-bottom:10px;'>Standalone Flight Duty Period calculator for "
            "<b>cabin crew</b> \u2014 FOM Part A Chapter 08. Enter all times in <b>Colombo (CMB) local time</b>. "
            "Max FDP = Table A/B value + 1:00 cabin-crew allowance (&sect;8.3.a).</div>", unsafe_allow_html=True)

        # ============ ⏳ DELAY SIMULATOR (what-if on your live roster) ============
        st.markdown("##### ⏳ Delay Simulator — what-if on your roster")
        _sim_duties = build_duties(parsed_rows) if parsed_rows else []
        if not _sim_duties:
            st.info("Paste your roster on the Dashboard to unlock the delay simulator — pick any duty, slide its delay, and see the FDP, rest and cumulative ripple effects.")
        else:
            _sim_labels = [f"{du['label']} — {du['report']:%d %b %H:%M}" for du in _sim_duties]
            _sc1, _sc2, _sc3 = st.columns([2.4, 1.2, 1.0])
            with _sc1:
                _sim_sel = st.selectbox("Duty to delay", list(range(len(_sim_duties))),
                                        format_func=lambda i: _sim_labels[i], key="sim_duty")
            with _sc2:
                _sim_delay = st.slider("Delay (minutes)", 0, 480, 60, 15, key="sim_delay")
            with _sc3:
                _sim_tableb = st.checkbox("Not acclimatized (Table B)", value=False, key="sim_tableb")

            _du = _sim_duties[_sim_sel]
            _prev_du = _sim_duties[_sim_sel - 1] if _sim_sel > 0 else None
            _next_du = _sim_duties[_sim_sel + 1] if _sim_sel + 1 < len(_sim_duties) else None
            _n = _du["n"]
            _dep0 = _du["sectors"][0]["dep"]
            _co0 = _du["chocks_on"]
            _acclim = not _sim_tableb

            # preceding rest from the roster (previous duty's chocks-on)
            _prev_rest_h = None
            if _prev_du is not None:
                _prev_rest_h = (_du["report"] - _prev_du["chocks_on"]).total_seconds() / 3600
                if _prev_rest_h < 0:
                    _prev_rest_h += 24

            # band / max FDP — before and with delay (8.2.6 delayed reporting)
            _band0 = fdp_band((_dep0 - timedelta(hours=1)).time())
            _max0 = fdp_limit_min(_acclim, _band0, _n, _prev_rest_h)
            if _sim_delay == 0:
                _band_d, _delay_note = _band0, ""
            else:
                _band_d, _delay_note = apply_delay(_acclim, _n, _prev_rest_h, _dep0, _sim_delay)
            _max_d = fdp_limit_min(_acclim, _band_d, _n, _prev_rest_h)

            # FDP clock: <4h → starts at delayed report; ≥4h → 4h after original report
            _fdp_start = (_du["report"] + timedelta(minutes=_sim_delay) if _sim_delay < 240
                          else _du["report"] + timedelta(hours=4))
            _co_d = _co0 + timedelta(minutes=_sim_delay)
            _fdp0 = max(0, int((_co0 - _du["report"]).total_seconds() // 60))
            _fdp_d = max(0, int((_co_d - _fdp_start).total_seconds() // 60))
            _margin_d = _max_d - _fdp_d

            # --- scenario card ---
            _scn = (
                f"<div class='bidrow'><span>Duty</span><span>{_du['label']} · {_n} sector(s)</span></div>"
                f"<div class='bidrow'><span>Report</span><span>{_du['report']:%d %b %H:%M}"
                + (f" → <b>{_fdp_start:%H:%M}</b>" if _sim_delay else "") + "</span></div>"
                f"<div class='bidrow'><span>First dep</span><span>{_dep0:%d %b %H:%M}"
                + (f" → <b>{(_dep0 + timedelta(minutes=_sim_delay)):%H:%M}</b>" if _sim_delay else "") + "</span></div>"
                f"<div class='bidrow'><span>Final chocks-on</span><span>{_co0:%d %b %H:%M}"
                + (f" → <b>{_co_d:%d %b %H:%M}</b>" if _sim_delay else "") + "</span></div>"
                f"<div class='bidrow'><span>Band (dep − 1h)</span><span>{_band0}"
                + (f" → <b>{_band_d}</b>" if _band_d != _band0 else "") + "</span></div>"
            )
            st.markdown(f"<div class='card'><h5>📋 Scenario</h5>{_scn}</div>", unsafe_allow_html=True)

            # --- verdict ---
            if _margin_d >= 0:
                _vc_bg, _vc_bc, _vc_tc, _vc_icon = "#12301f", "#4caf50", "#a5d6a7", "\u2705"
                _v_title, _v_sub = "WITHIN LIMITS", f"{_fmt_hm(_margin_d)} to spare"
            else:
                _vc_bg, _vc_bc, _vc_tc, _vc_icon = "#331414", "#ff1744", "#ff8a8a", "\u274c"
                _v_title, _v_sub = "EXCEEDS MAX FDP", f"over by {_fmt_hm(-_margin_d)}"
            _vrows = (
                f"<div class='bidrow'><span>Max cabin FDP</span><span>{_fmt_hm(_max_d)}</span></div>"
                f"<div class='bidrow'><span>Actual FDP</span><span>{_fmt_hm(_fdp0)}"
                + (f" → <b>{_fmt_hm(_fdp_d)}</b>" if _sim_delay else "") + "</span></div>"
                f"<div class='bidrow'><span>Latest allowed chocks-on</span><span>{(_fdp_start + timedelta(minutes=_max_d)):%d %b %H:%M}</span></div>"
            )
            st.markdown(
                f"<div class='card' style='text-align:center;background:{_vc_bg};border:1px solid {_vc_bc};'>"
                f"<div style='font-size:22px;'>{_vc_icon}</div>"
                f"<div style='font-size:17px;font-weight:800;color:{_vc_tc};'>{_v_title}</div>"
                f"<div class='muted'>{_v_sub}</div></div>"
                f"<div class='card' style='margin-top:6px;'>{_vrows}</div>",
                unsafe_allow_html=True)
            if _delay_note:
                st.markdown(f"<div class='card' style='font-size:12.5px;border-left:3px solid #ffc107;'>{_delay_note}</div>",
                            unsafe_allow_html=True)

            # --- rest ripple ---
            _need_rest = max(_fdp_d / 60 - 1, 11.0)
            _earliest_next = _co_d + timedelta(hours=_need_rest)
            if _next_du is not None:
                _gap = (_next_du["report"] - _co_d).total_seconds() / 3600
                if _gap < 0:
                    _gap += 24
                _rest_ok = _gap >= _need_rest
                _rr_c, _rr_t = ("#4caf50", "#a5d6a7") if _rest_ok else ("#ff5252", "#ff8a8a")
                _rr_verdict = ("\u2705 rest met" if _rest_ok
                               else f"\u274c short by {_fmt_hm(int((_need_rest - _gap) * 60))}")
                _rest_txt = (f"Next duty <b>{_next_du['label']}</b> reports <b>{_next_du['report']:%d %b %H:%M}</b> "
                             f"\u2192 rest available <b>{_fmt_hm(int(_gap * 60))}</b> \u00b7 "
                             f"<span style='color:{_rr_t};border-bottom:1px solid {_rr_c};'>{_rr_verdict}</span>")
            else:
                _rest_txt = "No next duty in this roster — this is the last duty."
            st.markdown(
                f"<div class='card' style='font-size:12.5px;'><b style='color:#00bcd4;'>Rest ripple:</b> "
                f"required after = max({_fmt_hm(_fdp_d)} \u2212 1h, 11h) = <b>{_fmt_hm(int(_need_rest * 60))}</b> \u00b7 "
                f"earliest next check-in <b>{_earliest_next:%d %b %H:%M}</b><br>{_rest_txt}</div>",
                unsafe_allow_html=True)

            # --- cumulative ripple ---
            _base_periods = _duty_periods(parsed_rows)
            _before = _cumulative_max(_base_periods)
            _after_periods = [dict(p) for p in _base_periods]
            for _i, _p in enumerate(_after_periods):
                if _p["label"] == _du["label"] and _p["start"] == _du["report"]:
                    _after_periods[_i] = {"label": _du["label"], "start": _fdp_start,
                                          "end": _co_d, "minutes": _fdp_d}
                    break
            _after = _cumulative_max(_after_periods)
            _cum_rows = ""
            for _wk, _cap, _cap_txt in (("7d", 60 * 60, "60 h"), ("14d", 105 * 60, "105 h"), ("28d", 210 * 60, "210 h")):
                _b, _a = _before[_wk], _after[_wk]
                _flag = ""
                if _a > _cap:
                    _flag = f" <span style='color:#ff8a8a;'>⚠️ over {_cap_txt}</span>"
                _cum_rows += (f"<div class='bidrow'><span>{_wk.replace('d', '-day')} max (cap {_cap_txt})</span>"
                              f"<span>{_fmt_hm(_b)}" + (f" → <b>{_fmt_hm(_a)}</b>{_flag}" if _a != _b else f"{_flag}") + "</span></div>")
            st.markdown(
                f"<div class='card'><h5>🔗 Cumulative ripple</h5>{_cum_rows}"
                f"<div class='muted' style='font-size:11px;margin-top:4px;'>flight duties + standby counted in full "
                f"(same as the Dashboard) \u00b7 the delayed duty is substituted at its delayed clock times.</div></div>",
                unsafe_allow_html=True)
            st.markdown("<hr style='border-color:#2a3b4d;margin:14px 0 10px;'>", unsafe_allow_html=True)

        lcol, rcol2 = st.columns([1, 1.15])

        with lcol:
            # ---------- 1. Current duty ----------
            st.markdown("##### 1 \u00b7 Current duty")
            ci_date = st.date_input("Check-in (report) date", value=datetime.now().date(), key="fdp_ci_date")
            ci_t = st.time_input("Check-in (report) time", value=dtime(6, 15), key="fdp_ci_t") or dtime(6, 15)
            dep_t = st.time_input("First sector departure time", value=dtime(7, 35), key="fdp_dep_t") or dtime(7, 35)
            dep_next = st.checkbox("Departure is the day AFTER check-in", value=False, key="fdp_dep_next")
            arr_t = st.time_input("Final sector arrival (chocks-on) time", value=dtime(15, 45), key="fdp_arr_t") or dtime(15, 45)
            arr_next = st.checkbox("Chocks-on is the day AFTER departure", value=False, key="fdp_arr_next")
            sectors = st.selectbox("Sectors in this duty", list(range(1, 9)), index=1, key="fdp_sectors",
                                   format_func=lambda n: f"{n} sector" + ("" if n == 1 else "s"))
            delay_min = st.number_input("Delay to reporting time, if told before leaving rest (minutes)",
                                        min_value=0, max_value=720, value=0, step=15, key="fdp_delay")

            ci_dt = datetime.combine(ci_date, ci_t)
            dep_dt = datetime.combine(ci_date + (timedelta(days=1) if dep_next else timedelta(0)), dep_t)
            arr_dt = datetime.combine(dep_dt.date() + (timedelta(days=1) if arr_next else timedelta(0)), arr_t)
            band_t = (dep_dt - timedelta(hours=1)).time()
            band = fdp_band(band_t)

            # ---------- 2. Previous duty ----------
            st.markdown("##### 2 \u00b7 Previous duty (optional)")
            use_prev = st.toggle("Include previous duty", value=False, key="fdp_use_prev")
            prev_type = "Turnaround"
            prev_ci_date = prev_ci_t = prev_co_date = prev_co_t = None
            lay_stn = "RUH"
            out_dep_date = out_dep_t = out_arr_t = None
            out_arr_next = False
            ret_dep_date = ret_dep_t = ret_arr_t = None
            ret_arr_next = False
            ret_arr_dt = None
            if use_prev:
                prev_type = st.radio("Previous duty was a\u2026", ["Turnaround", "Layover"],
                                     index=0, key="fdp_prev_type", horizontal=True)
                if prev_type == "Turnaround":
                    prev_ci_date = st.date_input("Previous check-in date", value=ci_date - timedelta(days=1), key="fdp_pci_date")
                    prev_ci_t = st.time_input("Previous check-in time", value=dtime(6, 0), key="fdp_pci_t") or dtime(6, 0)
                    prev_co_next = st.checkbox("Previous chocks-on was the day AFTER its check-in", value=False, key="fdp_pco_next")
                    prev_co_t = st.time_input("Previous chocks-on time", value=dtime(18, 0), key="fdp_pco_t") or dtime(18, 0)
                    prev_co_date = prev_ci_date + (timedelta(days=1) if prev_co_next else timedelta(0))
                else:
                    stations = sorted(AIRPORT_OFFSET_H.keys())
                    lay_stn = st.selectbox("Layover station", stations,
                                           index=stations.index("RUH") if "RUH" in stations else 0, key="fdp_lay_stn")
                    st.markdown("<div class='muted' style='margin-top:4px;margin-bottom:4px;'><b>Outbound</b> \u2014 e.g. UL265</div>", unsafe_allow_html=True)
                    c1, c2 = st.columns([1, 1])
                    out_dep_date = c1.date_input("Dep CMB date", value=ci_date - timedelta(days=3), key="fdp_out_dep_date")
                    out_dep_t = c2.time_input("Dep CMB time", value=dtime(18, 15), key="fdp_out_dep_t") or dtime(18, 15)
                    c1, c2 = st.columns([1, 1])
                    out_arr_t = c1.time_input(f"Arr {lay_stn} time", value=dtime(21, 20), key="fdp_out_arr_t") or dtime(21, 20)
                    out_arr_next = c2.checkbox(f"Arr {lay_stn} next day", value=False, key="fdp_out_arr_next")
                    st.markdown("<div class='muted' style='margin-top:4px;margin-bottom:4px;'><b>Return</b> \u2014 e.g. UL266</div>", unsafe_allow_html=True)
                    c1, c2 = st.columns([1, 1])
                    ret_dep_date = c1.date_input(f"Dep {lay_stn} date", value=ci_date - timedelta(days=2), key="fdp_ret_dep_date")
                    ret_dep_t = c2.time_input(f"Dep {lay_stn} time", value=dtime(22, 30), key="fdp_ret_dep_t") or dtime(22, 30)
                    c1, c2 = st.columns([1, 1])
                    ret_arr_t = c1.time_input("Arr CMB time", value=dtime(6, 20), key="fdp_ret_arr_t") or dtime(6, 20)
                    ret_arr_next = c2.checkbox("Arr CMB next day", value=True, key="fdp_ret_arr_next")

            # ---------- previous-duty numbers ----------
            prev_dur_h = preceding_rest_h = None
            if use_prev:
                if prev_type == "Turnaround":
                    pci_dt = datetime.combine(prev_ci_date, prev_ci_t)
                    pco_dt = datetime.combine(prev_co_date, prev_co_t)
                    prev_dur_h = (pco_dt - pci_dt).total_seconds() / 3600
                    if prev_dur_h <= 0:
                        prev_dur_h += 24
                    preceding_rest_h = (ci_dt - pco_dt).total_seconds() / 3600
                    if preceding_rest_h < 0:
                        preceding_rest_h += 24
                else:
                    ret_dep_dt = datetime.combine(ret_dep_date, ret_dep_t)
                    ret_arr_dt = datetime.combine(ret_dep_date + (timedelta(days=1) if ret_arr_next else timedelta(0)), ret_arr_t)
                    ret_rep_dt = ret_dep_dt - timedelta(hours=1, minutes=20)   # report \u2248 dep \u2212 1h20m
                    prev_dur_h = (ret_arr_dt - ret_rep_dt).total_seconds() / 3600
                    if prev_dur_h <= 0:
                        prev_dur_h += 24
                    preceding_rest_h = (ci_dt - ret_arr_dt).total_seconds() / 3600
                    if preceding_rest_h < 0:
                        preceding_rest_h += 24

            # ---------- 3. Acclimatization (auto) ----------
            st.markdown("##### 3 \u00b7 Acclimatization")
            acclim_auto = True
            acclim_reason = "No de-acclimatizing layover \u2014 acclimatized at base (CMB)"
            acclim_nights = None
            if use_prev and prev_type == "Layover":
                off = AIRPORT_OFFSET_H.get(lay_stn, 5.5)
                diff = abs(off - 5.5)
                if diff > 2 and ret_arr_dt is not None:
                    acclim_nights = _count_local_nights(ret_arr_dt, ci_dt)
                    if acclim_nights >= 3:
                        acclim_reason = (f"Layover at {lay_stn} (UTC{off:+.1f}, {diff:.1f}h off CMB) de-acclimatized, "
                                         f"but {acclim_nights} local nights at CMB since \u2192 re-acclimatized")
                    else:
                        acclim_auto = False
                        acclim_reason = (f"Layover at {lay_stn} (UTC{off:+.1f}, {diff:.1f}h off CMB) \u2192 de-acclimatized \u00b7 "
                                         f"{acclim_nights} local night(s) at CMB since (need 3)")
                else:
                    acclim_reason = f"Layover at {lay_stn} (UTC{off:+.1f}, within 2h of CMB) \u2192 stays acclimatized"
            elif use_prev and prev_type == "Turnaround":
                acclim_reason = "Turnaround \u2014 duty ends back at CMB, acclimatization unchanged"
            st.markdown(f"<div style='font-size:12px;color:#9fb3c8;margin-bottom:6px;'>{acclim_reason}</div>",
                        unsafe_allow_html=True)
            override = st.checkbox("Override acclimatization (I know my own state)", value=False, key="fdp_acclim_ovr")
            if override:
                acclim_man = st.radio("Acclimatized at start of this duty?", ["Yes", "No"], index=0, key="fdp_acclim_man")
                acclim_bool = (acclim_man == "Yes")
            else:
                acclim_bool = acclim_auto
            st.markdown(
                ("<div style='font-size:12.5px;background:#12301f;border:1px solid #4caf50;color:#a5d6a7;"
                 "padding:8px;border-radius:8px;'>\u2705 Acclimatized \u2192 Table A applies</div>"
                 if acclim_bool else
                 "<div style='font-size:12.5px;background:#33260f;border:1px solid #ffc107;color:#ffd54f;"
                 "padding:8px;border-radius:8px;'>\u23f1 Not acclimatized \u2192 Table B applies "
                 "(need 3 consecutive local nights at CMB to re-acclimatize)</div>"),
                unsafe_allow_html=True)

            # ---------- 4. Extensions & variations ----------
            st.markdown("##### 4 \u00b7 Extensions & variations (optional)")
            split_use = st.checkbox("Split duty \u2014 a break of less than the minimum rest between sectors", value=False, key="fdp_split_use")
            split_rest = None
            if split_use:
                split_rest = st.number_input("Break between sectors (minutes, 180\u2013600)", min_value=180, max_value=600, value=180, step=15, key="fdp_split_rest")
            relief_use = st.checkbox("In-flight relief \u2014 rest taken on board", value=False, key="fdp_relief_use")
            relief_rest = None
            relief_type = None
            if relief_use:
                c1, c2 = st.columns([1, 1])
                relief_rest = c1.number_input("In-flight rest taken (minutes, \u2265 180)", min_value=180, max_value=720, value=180, step=15, key="fdp_relief_rest")
                relief_type = c2.radio("Rest facility", ["Bunk", "Seat"], index=0, key="fdp_relief_type", horizontal=True)
            annex_b1 = st.checkbox("CMB\u2013LHR / CDG / FRA layover reporting 2200\u20130559 (Annex A B.1)", value=False, key="fdp_annex_b1")
            if annex_b1:
                st.markdown("<div class='muted' style='font-size:12px;'>Annex A B.1: max FDP <b>13:00</b>, extendable by in-flight relief "
                            "\u2014 requires <b>6 Economy seats blocked</b> for cabin-crew rest.</div>", unsafe_allow_html=True)

            # ---------- 5. Commencing duty from standby ----------
            st.markdown("##### 5 \u00b7 Commencing duty from standby (optional)")
            sby_use = st.checkbox("This duty starts from standby (called out)", value=False, key="fdp_sby_use")
            sby_start = None
            sby_case_c = False
            if sby_use:
                c1, c2 = st.columns([1, 1])
                sby_date = c1.date_input("Standby start date", value=ci_date, key="fdp_sby_date")
                sby_t = c2.time_input("Standby start time", value=dtime(0, 0), key="fdp_sby_t") or dtime(0, 0)
                sby_start = datetime.combine(sby_date, sby_t)
                sby_case_c = st.checkbox("Standby at home / suitable accommodation 2200\u20130800 with \u2264 2 h notice (Case C)",
                                         value=False, key="fdp_sby_case_c")
                st.markdown("<div class='muted' style='font-size:12px;'>Standby ends when you report (check-in) \u00b7 max standby "
                            "<b>12 h</b>. The standby start time sets the allowable FDP band unless the actual FDP starts in a "
                            "more limiting band.</div>", unsafe_allow_html=True)

        with rcol2:
            # --- delayed-reporting shift (8.2.6) ---
            ci_act = ci_dt + timedelta(minutes=delay_min)
            dep_act = dep_dt + timedelta(minutes=delay_min)
            arr_act = arr_dt + timedelta(minutes=delay_min)
            band_used = band
            fdp_start = ci_dt
            delay_note = ""
            if delay_min > 0:
                band_used, delay_note = apply_delay(acclim_bool, sectors, preceding_rest_h, dep_dt, delay_min)
                fdp_start = ci_act if delay_min < 240 else (ci_dt + timedelta(hours=4))

            sby_note = ""
            if sby_use and sby_start is not None:
                band_sby = fdp_band(sby_start.time())
                band_fdp_act = fdp_band((dep_act - timedelta(hours=1)).time())
                v_sby = fdp_limit_min(acclim_bool, band_sby, sectors, preceding_rest_h)
                v_act = fdp_limit_min(acclim_bool, band_fdp_act, sectors, preceding_rest_h)
                band_used = band_sby if v_sby <= v_act else band_fdp_act
                sby_note = (f"Standby start {sby_start:%H:%M} sets the FDP band \u2192 more limiting of "
                            f"{band_sby} / {band_fdp_act} = <b>{band_used}</b>.")

            base_fdp = 780 if annex_b1 else fdp_limit_min(acclim_bool, band_used, sectors, preceding_rest_h)
            extra, cap, ext_detail = extension_minutes(split_rest, relief_rest, relief_type)
            max_fdp = min(base_fdp + extra, cap) if cap else base_fdp + extra

            actual_fdp = int((arr_act - fdp_start).total_seconds() // 60)
            if actual_fdp < 0:
                actual_fdp = 0
            latest_co = fdp_start + timedelta(minutes=max_fdp)
            margin = max_fdp - actual_fdp

            if margin >= 0:
                vc_bg, vc_bc, vc_tc, vc_icon = "#12301f", "#4caf50", "#a5d6a7", "\u2705"
                v_title, v_sub = "WITHIN LIMITS", f"{_fmt_hm(margin)} to spare"
            else:
                vc_bg, vc_bc, vc_tc, vc_icon = "#331414", "#ff1744", "#ff8a8a", "\u274c"
                v_title, v_sub = "EXCEEDS MAX FDP", f"over by {_fmt_hm(-margin)}"
            st.markdown(
                f"<div class='card' style='text-align:center;background:{vc_bg};border:1px solid {vc_bc};'>"
                f"<div style='font-size:26px;'>{vc_icon}</div>"
                f"<div style='font-size:20px;font-weight:800;color:{vc_tc};'>{v_title}</div>"
                f"<div class='muted'>{v_sub}</div></div>", unsafe_allow_html=True)

            if delay_note:
                st.markdown(f"<div class='card' style='font-size:12.5px;border-left:3px solid #ffc107;'>{delay_note}</div>",
                            unsafe_allow_html=True)
            if sby_note:
                st.markdown(f"<div class='card' style='font-size:12.5px;border-left:3px solid #00bcd4;'>{sby_note}</div>",
                            unsafe_allow_html=True)

            rows = [
                ("Local time of start (dep \u2212 1h)", band_t.strftime("%H:%M") + f" \u2192 band <b>{band_used}</b>"),
                ("Sectors", f"{sectors}"),
                ("Table used", ("Table A \u2014 acclimatized" if acclim_bool else "Table B \u2014 not acclimatized")),
            ]
            if not acclim_bool and preceding_rest_h is not None:
                bucket = "between 18h and 30h" if 18 < preceding_rest_h <= 30 else "up to 18h / over 30h"
                rows.append(("Preceding rest (Table B key)", f"{_fmt_hm(int(preceding_rest_h * 60))} \u2192 {bucket}"))
            if annex_b1:
                rows.append(("Annex A B.1 cap", "13:00 (LHR/CDG/FRA 2200\u20130559)"))
            else:
                rows.append(("Table value (flight crew)", _fmt_hm(base_fdp - 60)))
                rows.append(("Cabin crew (+1:00)", _fmt_hm(base_fdp)))
            if ext_detail:
                rows.append(("Extensions", ext_detail))
            rows += [
                ("Max cabin FDP", _fmt_hm(max_fdp)),
                ("FDP counts from", fdp_start.strftime("%d %b %H:%M")),
                ("Latest allowed chocks-on", latest_co.strftime("%d %b %H:%M")),
                ("Actual chocks-on", arr_act.strftime("%d %b %H:%M")),
                ("Actual FDP", _fmt_hm(actual_fdp)),
            ]
            rows_html = "".join(f"<div class='bidrow'><span>{k}</span><span>{v}</span></div>" for k, v in rows)
            st.markdown(f"<div class='card'>{rows_html}</div>", unsafe_allow_html=True)

            this_dur_h = actual_fdp / 60
            need_rest_h = max(this_dur_h - 1, 11.0)
            earliest_next = arr_act + timedelta(hours=need_rest_h)
            st.markdown(
                f"<div class='card' style='font-size:12.5px;'><b style='color:#00bcd4;'>Rest needed after this duty:</b> "
                f"max({_fmt_hm(actual_fdp)} \u2212 1h, 11h) = <b>{_fmt_hm(int(need_rest_h * 60))}</b> \u00b7 "
                f"earliest next check-in <b>{earliest_next.strftime('%d %b %H:%M')}</b></div>",
                unsafe_allow_html=True)

            if use_prev and prev_dur_h is not None and preceding_rest_h is not None:
                req_rest_h = max(prev_dur_h - 1, 11.0)
                rest_ok = preceding_rest_h >= req_rest_h
                rb_c, rb_t = ("#4caf50", "#a5d6a7") if rest_ok else ("#ff5252", "#ff8a8a")
                verdict = "\u2705 rest met" if rest_ok else f"\u26a0\ufe0f rest short by {_fmt_hm(int((req_rest_h - preceding_rest_h) * 60))}"
                prev_label = "Previous duty" + (" \u2014 return leg" if prev_type == "Layover" else "")
                st.markdown(
                    f"<div class='card' style='font-size:12.5px;'>"
                    f"<b style='color:#00bcd4;'>{prev_label}:</b> duration <b>{_fmt_hm(int(prev_dur_h * 60))}</b> \u00b7 "
                    f"rest before this duty <b>{_fmt_hm(int(preceding_rest_h * 60))}</b><br>"
                    f"Required rest = max({_fmt_hm(int(prev_dur_h * 60))} \u2212 1h, 11h) = "
                    f"<b>{_fmt_hm(int(req_rest_h * 60))}</b> \u2192 "
                    f"<span style='color:{rb_t};border-bottom:1px solid {rb_c};'>{verdict}</span></div>",
                    unsafe_allow_html=True)

            if sby_use and sby_start is not None:
                sby_min = int((ci_act - sby_start).total_seconds() // 60)
                if sby_min < 0:
                    sby_min = 0
                allowed_tot, sby_case, sby_detail = standby_fdp_check(acclim_bool, sectors, preceding_rest_h,
                                                                     band_used, sby_min, sby_case_c)
                actual_tot = sby_min + actual_fdp
                sby_ok = actual_tot <= allowed_tot
                over12 = " \u00b7 \u26a0\ufe0f standby exceeds 12 h" if sby_min > 12 * 60 else ""
                sb_c, sb_t = ("#4caf50", "#a5d6a7") if sby_ok else ("#ff5252", "#ff8a8a")
                sby_verdict = ("\u2705 within limits" if sby_ok else f"\u274c exceeds by {_fmt_hm(actual_tot - allowed_tot)}")
                st.markdown(
                    f"<div class='card' style='font-size:12.5px;'>"
                    f"<b style='color:#00bcd4;'>Standby + FDP (called out):</b> standby {_fmt_hm(sby_min)} + "
                    f"FDP {_fmt_hm(actual_fdp)} = <b>{_fmt_hm(actual_tot)}</b> \u00b7 allowed "
                    f"<b>{_fmt_hm(allowed_tot)}</b> ({sby_case}){over12}<br>"
                    f"<span class='muted'>{sby_detail}</span><br>"
                    f"<span style='color:{sb_t};border-bottom:1px solid {sb_c};'>{sby_verdict}</span></div>",
                    unsafe_allow_html=True)
                st.markdown(
                    f"<div class='muted' style='font-size:11.5px;margin-top:2px;'>Min rest after a call-out is based on the "
                    f"<b>combined</b> standby + FDP duration ({_fmt_hm(actual_tot)}). A standby that finishes <i>without</i> a "
                    f"call-out needs 12 h rest before the next duty.</div>", unsafe_allow_html=True)

            with st.expander("\U0001f4d6 FDP tables (Chapter 08)"):
                ta = "| Local time of start | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8+ |\n|---|---|---|---|---|---|---|---|---|\n"
                for bandk, row in FDP_TABLE_A.items():
                    ta += f"| {bandk} | " + " | ".join(_fmt_hm(v) for v in row) + " |\n"
                st.markdown("**Table A \u2014 Acclimatized** (flight crew; cabin crew +1:00)")
                st.markdown(ta)
                tb = "| Preceding rest | 1 | 2 | 3 | 4 | 5 | 6 | 7+ |\n|---|---|---|---|---|---|---|---|\n"
                for bandk, row in FDP_TABLE_B.items():
                    label = "Up to 18h / over 30h" if bandk == "up18_or_over30" else "Between 18h and 30h"
                    tb += f"| {label} | " + " | ".join(_fmt_hm(v) for v in row[:7]) + " |\n"
                st.markdown("**Table B \u2014 Not acclimatized** (flight crew; cabin crew +1:00)")
                st.markdown(tb)
                st.markdown("<div class='muted'>All times Colombo local. FDP = check-in \u2192 final chocks-on. "
                            "Local time of start = first departure \u2212 1h (flight-crew report time). "
                            "Acclimatized = 3 consecutive local nights at CMB.</div>",
                            unsafe_allow_html=True)

        # ---------- FAQ ----------
        with st.expander("\U0001f4d6 FAQ \u2014 what do all these terms actually mean? (tap to learn)"):
            st.markdown("""
**The basics**
- **FDP (Flight Duty Period)** — your working day, from the moment you **report/check-in** until the aircraft's
  **on-chock** (see below) after your **final sector**. Your commute *after* chocks-on doesn't count.
- **Sector** — one leg: from the aircraft first moving under its own power to it coming to rest at the stand.
  CMB \u2192 BKK is 1 sector; a same-day return CMB \u2192 BKK \u2192 CMB is **2 sectors**.
- **Check-in / report time** — the time you must sign in for the duty (usually departure \u2212 1 h 20 m for cabin crew).
- **On-chock** — when the aircraft is parked and chocks go under the wheels; your FDP officially ends there.

**Reading the FDP table**
- **Local time of start** — the band in Table A/B is read from **first departure \u2212 1 hour** (the flight-crew
  report time), even though you check in a bit earlier. Example: dep 07:35 \u2192 start 06:35 \u2192 band 0600\u20130759.
- **Table A vs Table B** — Table A when you're **acclimatized** to base; Table B when you're **not acclimatized**.
  Table B is also keyed on how long you rested **before** the duty (see below).
- **+1:00 cabin crew** — cabin crew may work **1 hour longer** than the flight-crew value in the table (&sect;8.3.a).

**Acclimatization & local nights**
- **Acclimatized** — your body clock is on base time. You're acclimatized after **3 consecutive local nights** on the
  ground in a time zone no wider than 2 hours, and you stay acclimatized until a duty ends somewhere **more than 2 h**
  off base time.
- **Local night** — an **8-hour period between 2200 and 0800** local time. Landing back in CMB after a RUH/DXB/SIN-style
  layover does **not** instantly re-acclimatize you \u2014 you need 3 local nights at home first.

**Rest rules**
- **Minimum rest before a duty (cabin crew)** — the **greater of (previous duty \u2212 1 hour) or 11 hours**.
- **Preceding rest (for Table B)** — the rest you had *immediately before* this duty. The Table B row changes at
  **18 h and 30 h**: up to 18 h or over 30 h is one row, between 18 h and 30 h is the other (30 h counts as "between").

**Cumulative limits** (the running totals over days and weeks)
- Cabin crew: **60 h in 7 days** (up to 65 h only with unforeseen delays), **105 h in 14 days**, **210 h in 28 days**.
  FDPs, standby and positioning all add into these totals.

**Early / late / night duties**
- **Early Start** — duty starts 0500\u20130659. **Late Finish** — ends 0100\u20130159. **Night Duty** — any part of the
  duty falls between 0200 and 0459.
- Duties touching 0100\u20130659: max **3 in a row** and max **4 in 7 days**, broken only by **\u2265 34 h** clear.
- Before a night-duty block you must be **free by 21:00**.

**Days off**
- A **single day off** = **2 local nights** and at least **34 hours** (each extra consecutive day off adds one more
  local night).
- You may not be **on duty more than 7 days in a row** — the **8th day must be off** (or you may be *positioned*
  back to base on day 8, provided you then get 2 consecutive days off).
- You must have **2 consecutive days off in every 14 days** — so by the **13th/14th day** of any stretch you
  must have had a 2-day-off block.
- At least **7 days off in every 4 weeks**, and an **average of 8 days off per 4-week period** over three periods.
- The dashboard checks **every day off** against the 34 h / 2-local-night rule, and highlights the **mandatory** days
  off (🔴 OFF · MAND = required by the rules above; 🟢 OFF = discretionary) right on the calendar.
- **TOF = time off** — a protected window (e.g. 14:00–20:00), *not* a day off. It doesn't count toward your days off,
  but **no duty may check in or check out during it** — the dashboard flags any flight that does.
- **Standby** — you're on call, not off duty; it **counts in full** toward your cumulative totals.
- **Positioning (deadheading)** — flying as a passenger at the company's request. It's duty time, but **not a sector**.

**Extensions & delays**
- **Split duty** — a duty with a break of less than the minimum rest between sectors can be extended by **half the break**
  (break 3\u201310 h).
- **In-flight relief** — with \u2265 3 h rest on board: **bunk** adds **half** the rest (max 19 h cabin), **seat** adds
  **one third** (max 16 h cabin).
- **Delayed reporting** — if told *before leaving rest*: delay **under 4 h** \u2192 max FDP from the original report band;
  delay **4 h or more** \u2192 the more limiting band, and the FDP clock starts 4 h after the original report time.
- **Delayed after reporting** — once you've reported, the FDP clock runs from check-in to your duty's **final on-chock**.
  A delay on the outbound leg (e.g. UL404) pushes the return leg (UL405) later, so the Agent Scan checks whether the
  whole duty would still fit its maximum FDP — and warns *"operating UL405 would exceed the duty FDP"* if not. The
  17h30m rest rule is only checked **after** the duty ends, before the next report (never mid-turnaround).

**Annex A B.1 (LHR / CDG / FRA)**
- CMB\u2013London/CDG/Frankfurt layovers reporting **2200\u20130559** local: max FDP **13:00**, extendable with in-flight
  relief \u2014 and **6 Economy seats must be blocked** for cabin-crew rest.
""")
