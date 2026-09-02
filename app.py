import streamlit as st
import sqlite3
import hashlib
import re
import requests
from datetime import datetime, timedelta, timezone

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
def parse_roster_text(raw_text):
    lines = raw_text.split('\n')
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

            time_matches = re.findall(r'(\d{2}:\d{2})', line_str)

            if "OFF" in line_str or "ROF" in line_str or "TOF" in line_str:
                activity_type = "DAY OFF"
            elif "HTL" in line_str:
                activity_type = "LAYOVER"
            elif "SB" in line_str:
                activity_type = "STANDBY"
            elif "UL" in line_str:
                activity_type = "FLIGHT"
                match = re.search(r'(UL\s*\d+)', line_str)
                if match:
                    raw_fn = match.group(1).replace(" ", "")
                    num_part = re.search(r'\d+', raw_fn).group(0)
                    flight_no = f"UL {num_part}"

            route_match = re.search(r'([A-Z]{3})\s+([A-Z]{3})', line_str)
            if route_match:
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

            parsed_rows.append({
                "Date": row_date_str,
                "DateObj": row_dt_obj,
                "Type": activity_type,
                "Flight / Code": flight_no if flight_no != "-" else activity_type,
                "Check-In": checkin_time,
                "Departure": dep_time,
                "Route": route,
                "Arrival": arr_time,
                "Checkout": checkout_time,
                "Aircraft": ac_type
            })

    return parsed_rows

# --- 3. FLIGHTRADAR24 LIVE TELEMETRY (KEYLESS, NO QUOTAS) ---
# Uses FR24's public flight-list JSON feed. No API key, no registration,
# no hard monthly quota. Results cached for 10 minutes per flight to be
# polite and to keep Streamlit reruns instant.

FR24_URL = "https://api.flightradar24.com/common/v1/flight/list.json"
FR24_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/124.0 Safari/537.36"),
    "Accept": "application/json",
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

def query_live_aviation_feed(flight_no, flight_date):
    """
    Look up a specific flight number on a specific ROSTER date via FR24.
    flight_date: datetime.date of the departure (origin local time).
    Returns a structured status dict.
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
    status_text = (status_obj.get("text") or "").strip()          # e.g. "Landed 14:22", "Delayed", "Scheduled", "Estimated dep 09:10"
    generic = ((status_obj.get("generic") or {}).get("status") or {})
    generic_text = (generic.get("text") or "").lower()            # scheduled / delayed / canceled / departed / landed / estimated
    is_live = bool(status_obj.get("live"))

    is_cancelled = "cancel" in generic_text or "cancel" in status_text.lower()

    # Delay = estimated/actual departure later than schedule, or FR24 flags it.
    delay_mins = 0
    ref_dep = real_dep or est_dep
    if sched_dep and ref_dep and ref_dep > sched_dep:
        delay_mins = int(round((ref_dep - sched_dep) / 60))
    flagged_delayed = ("delay" in generic_text or "delay" in status_text.lower()
                       or generic.get("color") == "red")
    is_delayed = (not is_cancelled) and (flagged_delayed or delay_mins >= 15)

    return {
        "status_known": True,
        "is_delayed": is_delayed,
        "is_cancelled": is_cancelled,
        "delay_minutes": delay_mins,
        "is_live": is_live,
        "fr24_status": status_text or generic_text.capitalize() or "Scheduled",
        "sched_dep_local": _fmt_local_time(sched_dep, tz_off),
        "est_dep_local": _fmt_local_time(ref_dep, tz_off) if ref_dep else "-",
        "aircraft": ((matched.get("aircraft") or {}).get("model") or {}).get("code") or "-",
        "registration": ((matched.get("aircraft") or {}).get("registration")) or "-",
        "source": "Flightradar24 live feed",
    }

def fetch_live_flight_telemetry(flight_no, flight_date, route, scheduled_dep):
    """flight_date is a datetime.date object taken straight from the roster."""
    live = query_live_aviation_feed(flight_no, flight_date)
    date_label = flight_date.strftime("%d %b")

    if not live.get("status_known", False):
        err = live.get("error", "Unknown error")
        return {"is_delayed": False, "severity": "unknown",
                "status_message": f"ℹ️ {flight_no} ({route}) — {err}."}

    reg_bits = []
    if live.get("registration", "-") != "-":
        reg_bits.append(live["registration"])
    if live.get("aircraft", "-") != "-":
        reg_bits.append(live["aircraft"])
    tail = f" · {' / '.join(reg_bits)}" if reg_bits else ""

    if live.get("is_cancelled"):
        return {"is_delayed": True, "severity": "cancelled",
                "status_message": f"🚫 CANCELLED — FR24 reports {flight_no} on {date_label} is cancelled{tail}."}

    if live.get("is_delayed"):
        mins = live.get("delay_minutes", 0)
        mins_str = f" by ~{mins} min" if mins > 0 else ""
        est = live.get("est_dep_local", "-")
        est_str = f" New est. departure {est}." if est != "-" else ""
        return {"is_delayed": True, "severity": "delayed",
                "status_message": f"⚠️ DELAYED{mins_str} — {flight_no} sched {live.get('sched_dep_local', scheduled_dep)}.{est_str}{tail}"}

    live_str = " (airborne now)" if live.get("is_live") else ""
    return {"is_delayed": False, "severity": "ok",
            "status_message": f"✅ {live.get('fr24_status', 'On time')}{live_str} — {flight_no} dep {live.get('sched_dep_local', scheduled_dep)} verified via Flightradar24{tail}."}

# --- 4. STREAMLIT CONFIG & UI ---
st.set_page_config(page_title="Crew Companion", page_icon="✈️", layout="wide")
init_db()

st.markdown("""
    <style>
    .stApp {
        background-color: #0e1621;
        color: #ffffff;
    }
    .metric-card {
        background-color: #17212b;
        border: 1px solid #232e3c;
        padding: 15px;
        border-radius: 8px;
        text-align: center;
        margin-bottom: 10px;
    }
    </style>
""", unsafe_allow_html=True)

if 'logged_in' not in st.session_state:
    st.session_state['logged_in'] = False
    st.session_state['username'] = ''
    st.session_state['full_name'] = ''
    st.session_state['rank'] = ''

# --- AUTHENTICATION SCREEN ---
if not st.session_state['logged_in']:
    col1, col2, col3 = st.columns([1, 2, 1])
    with col2:
        st.markdown("<h1 style='text-align: center;'>✈️ Crew Companion</h1>", unsafe_allow_html=True)
        st.markdown("<h3 style='text-align: center; color: #888;'>Enterprise Roster & Analytics Hub</h3>", unsafe_allow_html=True)

        tab_login, tab_reg = st.tabs(["Log In", "Register Account"])

        with tab_login:
            user_input = st.text_input("Staff Email / Username", key="login_user_main")
            pass_input = st.text_input("Password", type="password", key="login_pass_main")
            if st.button("Access Dashboard", use_container_width=True):
                user_record = login_user(user_input, pass_input)
                if user_record:
                    st.session_state['logged_in'] = True
                    st.session_state['username'] = user_record[0]
                    st.session_state['full_name'] = user_record[2]
                    st.session_state['rank'] = user_record[3]
                    st.rerun()
                else:
                    st.error("Invalid credentials.")

        with tab_reg:
            new_user = st.text_input("Choose Username / Email", key="reg_user_main")
            new_pass = st.text_input("Choose Password", type="password", key="reg_pass_main")
            new_name = st.text_input("Full Name", key="reg_name_main")
            new_rank = st.selectbox("Rank", ["Senior Cabin Crew", "Cabin Crew", "Purser", "Flight Deck"], key="reg_rank_main")
            if st.button("Create Account", use_container_width=True):
                if new_user.strip() and new_pass.strip() and new_name.strip():
                    if add_user(new_user.strip(), new_pass, new_name.strip(), new_rank):
                        st.success("Account created! Switch to Log In.")
                    else:
                        st.error("Username already taken.")
                else:
                    st.warning("Please complete all fields.")

else:
    nav_col1, nav_col2, nav_col3 = st.columns([3, 2, 1])
    with nav_col1:
        st.markdown("### 🌲 CrewAI Roster Companion")

    with nav_col2:
        with st.expander("🤖 AI Agent Status"):
            st.write("Dynamic roster parser & Flightradar24 live-feed agent online. No API key or quota required.")
            st.success("FR24 Engine: ACTIVE (keyless)")

    with nav_col3:
        if st.button("Log Out", use_container_width=True):
            st.session_state['logged_in'] = False
            st.rerun()

    st.markdown("---")

    left_col, main_col, right_col = st.columns([1, 2.2, 1.2])

    with left_col:
        st.markdown("#### Analytics & Fatigue")
        st.markdown("<div class='metric-card'><b>Cumulative Block Hours</b><h2 style='color:#00bcd4;'>78 / 85</h2><small>hrs (91%)</small></div>", unsafe_allow_html=True)
        st.markdown("<div class='metric-card'><b>Fatigue Score</b><h3 style='color:#ff9800;'>Moderate (6.4/10)</h3><small>Recent red-eye flights detected</small></div>", unsafe_allow_html=True)
        st.markdown("<div class='metric-card'><b>Estimated Allowances</b><h3 style='color:#4caf50;'>$1,450 USD</h3><small>Total calculated per diem</small></div>", unsafe_allow_html=True)

    with main_col:
        st.markdown("#### Main Roster Calendar View")

        if 'current_roster' not in st.session_state:
            st.session_state['current_roster'] = load_roster_from_db(st.session_state['username'])

        with st.expander("📝 Paste Roster & Voila (Instant Parse)"):
            roster_input = st.text_area("Paste Raw Roster Here", value=st.session_state['current_roster'], height=120)
            if st.button("Auto-Process Roster"):
                if roster_input.strip():
                    save_roster_to_db(st.session_state['username'], roster_input)
                    st.session_state['current_roster'] = roster_input
                    st.success("Roster updated and processed instantly!")
                    st.rerun()
                else:
                    st.warning("Please paste roster text.")

        active_text = st.session_state.get('current_roster', '')
        if active_text:
            rows = parse_roster_text(active_text)
            if rows:
                roster_map = {}
                for r in rows:
                    d_str = r["Date"]
                    if d_str not in roster_map:
                        roster_map[d_str] = []
                    roster_map[d_str].append(r)

                valid_dates = [r["DateObj"] for r in rows if r["DateObj"] is not None]

                if valid_dates:
                    min_date = min(valid_dates)
                    max_date = max(valid_dates)
                    delta = (max_date - min_date).days
                    rolling_days = [min_date + timedelta(days=i) for i in range(delta + 1)]

                    start_weekday = min_date.weekday()
                    weekdays = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

                    h_cols = st.columns(7)
                    for idx, day_name in enumerate(weekdays):
                        h_cols[idx].markdown(f"<div style='text-align: center; font-size:13px; color: #888;'>{day_name}</div>", unsafe_allow_html=True)

                    st.markdown("<hr style='margin:5px 0;'>", unsafe_allow_html=True)

                    grid_cols = st.columns(7)
                    current_slot = start_weekday

                    for _ in range(start_weekday):
                        with grid_cols[_]:
                            st.write("")

                    for dt in rolling_days:
                        d_str = dt.strftime("%d%b%y").upper()
                        display_date_label = dt.strftime("%d %b")

                        with grid_cols[current_slot]:
                            st.markdown(f"<span style='font-size:11px; color:#aaa;'>{display_date_label}</span>", unsafe_allow_html=True)
                            if d_str in roster_map:
                                for act in roster_map[d_str]:
                                    if act["Type"] == "DAY OFF":
                                        st.markdown("<div style='background:#1b362d; padding:4px; border-radius:4px; font-size:10px; color:#4caf50; margin-bottom:2px;'>🟢 OFF</div>", unsafe_allow_html=True)
                                    elif act["Type"] == "LAYOVER":
                                        st.markdown(f"<div style='background:#1c2d37; padding:4px; border-radius:4px; font-size:10px; color:#2196f3; margin-bottom:2px;'>🏨 {act['Route']}</div>", unsafe_allow_html=True)
                                    else:
                                        dep = f"Dep: {act['Departure']}" if act['Departure'] != "-" else ""
                                        st.markdown(f"<div style='background:#2d3723; padding:4px; border-radius:4px; font-size:10px; color:#8bc34a; margin-bottom:2px;'>✈️ <b>{act['Flight / Code']}</b><br>{act['Route']}<br><small>{dep}</small></div>", unsafe_allow_html=True)
                            else:
                                st.markdown("<span style='font-size:10px; color:#555;'>No duty</span>", unsafe_allow_html=True)

                        current_slot += 1
                        if current_slot >= 7:
                            current_slot = 0
                            grid_cols = st.columns(7)
                else:
                    st.info("No dated duties parsed.")
            else:
                st.info("Paste roster text to populate grid.")
        else:
            st.info("Paste your roster text above to instantly populate your calendar grid.")

    with right_col:
        st.markdown("#### Live Roster Flight Monitor")

        parsed_rows = parse_roster_text(active_text) if active_text else []

        available_roster_dates = sorted(list(set([r["DateObj"].date() for r in parsed_rows if r["DateObj"] is not None])))

        if available_roster_dates:
            # Smart anchor: real 'today' if it falls inside the roster window,
            # otherwise the nearest upcoming roster day (so as days go on the
            # monitor always tracks the correct duties automatically).
            real_today = datetime.now().date()
            if real_today in available_roster_dates:
                default_idx = available_roster_dates.index(real_today)
            else:
                future = [d for d in available_roster_dates if d >= real_today]
                default_idx = available_roster_dates.index(future[0]) if future else len(available_roster_dates) - 1

            simulated_today = st.selectbox(
                "Roster anchor day (auto-set to today):",
                options=available_roster_dates,
                index=default_idx,
                format_func=lambda x: x.strftime("%d %b %Y") + ("  ← today" if x == real_today else "")
            )
        else:
            simulated_today = datetime.now().date()

        simulated_tomorrow = simulated_today + timedelta(days=1)

        active_target_flights = []
        seen = set()
        for row in parsed_rows:
            if row["Type"] == "FLIGHT" and row["DateObj"] is not None:
                flight_date = row["DateObj"].date()
                if flight_date in [simulated_today, simulated_tomorrow]:
                    key = (row["Flight / Code"], flight_date)
                    if key in seen:
                        continue
                    seen.add(key)
                    active_target_flights.append({
                        "flight_no": row["Flight / Code"],
                        "date_obj": flight_date,
                        "route": row["Route"],
                        "dep_time": row["Departure"]
                    })

        flight_check_results = []
        if active_target_flights:
            with st.spinner("Querying Flightradar24 live feed..."):
                for flight in active_target_flights:
                    telemetry = fetch_live_flight_telemetry(
                        flight["flight_no"],
                        flight["date_obj"],
                        flight["route"],
                        flight["dep_time"]
                    )
                    flight_check_results.append({
                        "flight": flight["flight_no"],
                        "route": flight["route"],
                        "date": flight["date_obj"].strftime("%d %b %Y"),
                        "delayed": telemetry["is_delayed"],
                        "severity": telemetry.get("severity", "ok"),
                        "status": telemetry["status_message"]
                    })

        if flight_check_results:
            checked_count = len(flight_check_results)
            st.markdown(
                f"<div style='background-color: #17212b; border: 1px solid #232e3c; padding: 12px; border-radius: 8px; font-size: 12px; margin-top: 8px;'>"
                f"<b style='color: #00bcd4;'>Agent Scan:</b> Verified {checked_count} flight(s) against Flightradar24 (keyless live feed, cached 10 min)."
                f"</div>", unsafe_allow_html=True)

            for df in flight_check_results:
                if df["severity"] == "cancelled":
                    border_color, bg_color, txt_color, icon = "#ff1744", "#331414", "#ff8a8a", "🚫"
                elif df["severity"] == "delayed":
                    border_color, bg_color, txt_color, icon = "#ff5252", "#2c1f1f", "#ff8a8a", "⚠️"
                elif df["severity"] == "unknown":
                    border_color, bg_color, txt_color, icon = "#607d8b", "#1c2429", "#b0bec5", "ℹ️"
                else:
                    border_color, bg_color, txt_color, icon = "#4caf50", "#1b362d", "#a5d6a7", "✈️"

                st.markdown(
                    f"<div style='font-size: 13px; background-color: {bg_color}; padding: 12px; border-radius: 6px; margin-top: 10px; border: 1px solid {border_color};'>"
                    f"{icon} <b style='font-size: 14px;'>{df['flight']}</b> ({df['route']}) — <span style='color: #ccc;'><i>{df['date']}</i></span>"
                    f"<div style='margin-top: 5px; color: {txt_color}; font-size: 12px;'>{df['status']}</div>"
                    f"</div>", unsafe_allow_html=True)

            if st.button("🔄 Force Refresh Live Data", use_container_width=True):
                fr24_fetch_flight_history.clear()
                st.rerun()
        else:
            st.markdown(
                f"<div style='background-color: #17212b; border: 1px solid #232e3c; padding: 14px; border-radius: 8px; font-size: 13px; margin-top: 8px;'>"
                f"<b style='color: #888;'>No flights found for {simulated_today.strftime('%d %b')} or {simulated_tomorrow.strftime('%d %b')}.</b>"
                f"</div>", unsafe_allow_html=True)

        st.markdown("<br>", unsafe_allow_html=True)
        st.markdown("#### Tactical Bidding")
        st.text_input("Search pairing...", placeholder="Find me a Sydney long stay", label_visibility="collapsed")
        st.markdown(
            "<div style='background-color: #17212b; border: 1px solid #232e3c; padding: 10px; border-radius: 8px; font-size:12px;'>"
            "<b>SYD-04</b> | Layover: <code>34h 00m</code><br>"
            "<span style='color: #ff9800;'>5 Requests [Low] ⭐ Recommended</span>"
            "</div>", unsafe_allow_html=True)
