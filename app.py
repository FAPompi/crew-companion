import streamlit as st
import sqlite3
import hashlib
import pandas as pd
import re
from datetime import datetime, timedelta
import requests

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
    
    for line in lines:
        line_str = line.strip()
        if not line_str:
            continue
            
        if "UL" in line_str or "OFF" in line_str or "HTL" in line_str:
            activity_type = "OTHER"
            flight_no = "-"
            checkin_time = "-"
            dep_time = "-"
            route = "-"
            arr_time = "-"
            checkout_time = "-"
            ac_type = "-"
            date_str = "-"
            dt_obj = None
            
            dt_matches = re.findall(r'(\d{2}[A-Z]{3}\d{2}\s+(\d{2}:\d{2}))', line_str)
            time_only_matches = [m[1] for m in dt_matches]
            
            date_match = re.search(r'(\d{2}[A-Z]{3}\d{2})', line_str)
            if date_match:
                date_match_str = date_match.group(1)
                try:
                    dt_obj = datetime.strptime(date_match_str, "%d%b%y")
                    date_str = date_match_str
                except ValueError:
                    pass
            
            if "OFF" in line_str:
                activity_type = "DAY OFF"
                if time_only_matches:
                    checkin_time = time_only_matches[0]
            elif "HTL" in line_str:
                activity_type = "LAYOVER"
                if len(time_only_matches) >= 2:
                    checkin_time = time_only_matches[0]
                    checkout_time = time_only_matches[1]
                elif len(time_only_matches) == 1:
                    checkin_time = time_only_matches[0]
                
                route_match = re.search(r'([A-Z]{3})\s+([A-Z]{3})', line_str)
                if route_match:
                    route = f"{route_match.group(1)} ➔ {route_match.group(2)}"
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
                
                if len(time_only_matches) >= 4:
                    checkin_time = time_only_matches[0]
                    dep_time = time_only_matches[1]
                    arr_time = time_only_matches[2]
                    checkout_time = time_only_matches[3]
                elif len(time_only_matches) == 3:
                    checkin_time = time_only_matches[0]
                    dep_time = time_only_matches[1]
                    arr_time = time_only_matches[2]
                elif len(time_only_matches) == 2:
                    checkin_time = time_only_matches[0]
                    dep_time = time_only_matches[0]
                    arr_time = time_only_matches[1]
                elif len(time_only_matches) == 1:
                    dep_time = time_only_matches[0]
                    
                parts = line_str.split()
                for p in parts:
                    if len(p) == 3 and p.isalnum() and p not in ["FA", "J28", "CMB", "CAN", "BKK", "TRZ", "MAA"]:
                        ac_type = p
            
            parsed_rows.append({
                "Date": date_str,
                "DateObj": dt_obj,
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

# --- 3. LIVE INTRANET i-FLEET API SESSION (WITH MANUAL COOKIE OVERRIDE) ---
def authenticate_and_fetch_flight_status(intranet_user, intranet_pass, flight_no, flight_date, dep_stn="CMB", arr_stn="", manual_cookie=""):
    login_url = "https://intraneti.srilankan.com/ifv"
    details_endpoint = "https://intraneti.srilankan.com/IFV/iFLEET_Local/GetFlightDetails"
    
    session = requests.Session()
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8"
    }
    
    try:
        # If manual cookie is provided, bypass automated credential posting entirely
        if manual_cookie:
            session.cookies.set("ASP.NET_SessionId", manual_cookie.strip())
            # Alternatively set cookies directly via domain jar if formatted like name=val
            if "=" in manual_cookie:
                for cookie_pair in manual_cookie.split(";"):
                    if "=" in cookie_pair:
                        k, v = cookie_pair.strip().split("=", 1)
                        session.cookies.set(k, v, domain="intraneti.srilankan.com")
        else:
            if not intranet_user or not intranet_pass:
                return {"success": False, "status_code": None, "error": "Intranet credentials or manual session cookie missing.", "delayed": False}
                
            # Step 1: Hit login portal to establish cookies & harvest ALL hidden form fields
            get_resp = session.get(login_url, headers=headers, timeout=10, verify=True)
            if get_resp.status_code != 200:
                return {"success": False, "status_code": get_resp.status_code, "error": "Failed to reach portal page.", "delayed": False}

            form_payload = {}
            inputs = re.findall(r'<input[^>]+>', get_resp.text, re.IGNORECASE)
            for inp in inputs:
                name_match = re.search(r'name=["\']([^"\']+)["\']', inp, re.IGNORECASE)
                val_match = re.search(r'value=["\']([^"\']*)["\']', inp, re.IGNORECASE)
                if name_match:
                    field_name = name_match.group(1)
                    field_val = val_match.group(1) if val_match else ""
                    form_payload[field_name] = field_val

            user_field = next((k for k in form_payload.keys() if 'user' in k.lower() or 'email' in k.lower()), 'username')
            pass_field = next((k for k in form_payload.keys() if 'pass' in k.lower()), 'password')

            form_payload[user_field] = intranet_user
            form_payload[pass_field] = intranet_pass

            post_headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Referer": login_url,
                "Origin": "https://intraneti.srilankan.com",
                "Content-Type": "application/x-www-form-urlencoded"
            }
            
            # Step 2: Submit credentials via POST
            login_resp = session.post(login_url, data=form_payload, headers=post_headers, timeout=10, allow_redirects=True, verify=True)
            
            if "#input-box" in login_resp.text or "passcode" in login_resp.text.lower() or "login" in login_resp.url.lower():
                return {
                    "success": False, 
                    "status_code": login_resp.status_code, 
                    "error": "Authentication challenge failed or session rejected (returned login HTML form).", 
                    "delayed": False
                }

        # Step 3: Query the flight details endpoint using the session cookies
        api_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://intraneti.srilankan.com/IFV/",
            "Content-Type": "application/json; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest"
        }
        
        api_payload = {
            "DATOP": flight_date, 
            "FLTID": flight_no, 
            "DEPSTN": dep_stn, 
            "ARRSTN": arr_stn, 
            "FlyingTime": "0"
        }
        
        resp = session.post(details_endpoint, json=api_payload, headers=api_headers, timeout=10)
        
        if resp.status_code == 200:
            try:
                data = resp.json()
                dep_details = data.get("DepDetails", "On Time")
                arr_details = data.get("ArrDetails", "On Time")
                late_min_dep = data.get("Latemin_Dep", 0)
                late_min_arr = data.get("Latemin_Arr", 0)
                
                is_delayed = (
                    (late_min_dep and late_min_dep > 0) or 
                    (late_min_arr and late_min_arr > 0) or
                    ("delay" in str(dep_details).lower()) or 
                    ("delay" in str(arr_details).lower()) or
                    (data.get("STATUS") and "cancel" in str(data.get("STATUS")).lower())
                )
                
                status_text = f"Dep: {dep_details} | Arr: {arr_details}"
                if late_min_dep > 0:
                    status_text += f" (Late Dep: {data.get('Latemin_Dep_Str', f'{late_min_dep} mins')})"
                
                return {
                    "success": True,
                    "status_code": resp.status_code,
                    "delayed": is_delayed,
                    "status_message": status_text,
                    "eta": data.get("ETA", "As Scheduled")
                }
            except ValueError:
                return {
                    "success": False, 
                    "status_code": resp.status_code, 
                    "error": "API returned non-JSON data (Session cookie expired or unauthenticated).", 
                    "delayed": False
                }
                
        return {"success": False, "status_code": resp.status_code, "error": f"Details API returned status {resp.status_code}"}
        
    except requests.exceptions.RequestException as e:
        return {"success": False, "status_code": "Timeout/DNS Error", "error": str(e), "delayed": False}

def get_upcoming_roster_flights(parsed_rows, current_date):
    target_flights = []
    tomorrow_date = current_date + timedelta(days=1)
    
    for row in parsed_rows:
        if row["Type"] == "FLIGHT" and row["DateObj"] is not None:
            if row["DateObj"].date() in [current_date.date(), tomorrow_date.date()]:
                formatted_date = row["DateObj"].strftime("%Y-%m-%d")
                
                route_parts = row["Route"].split("➔")
                dep_stn = route_parts[0].strip() if len(route_parts) > 0 else "CMB"
                arr_stn = route_parts[1].strip() if len(route_parts) > 1 else ""
                
                target_flights.append({
                    "flight_no": row["Flight / Code"],
                    "date": formatted_date,
                    "dep_stn": dep_stn,
                    "arr_stn": arr_stn,
                    "route": row["Route"]
                })
    return target_flights

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
    nav_col1, nav_col2, nav_col3 = st.columns([3, 4, 1])
    with nav_col1:
        st.markdown("### 🌲 CrewAI Roster Companion")
    with nav_col3:
        if st.button("Log Out"):
            st.session_state['logged_in'] = False
            st.rerun()

    st.markdown("---")

    # --- SIDEBAR: INTRANET CREDENTIALS & COOKIE BRIDGE ---
    with st.sidebar:
        st.markdown("### 🔗 Intranet Integration")
        st.write("If automated login fails due to portal security walls, log in via your browser, copy your `ASP.NET_SessionId` cookie, and paste it below.")
        
        if 'intranet_user' not in st.session_state:
            st.session_state['intranet_user'] = ""
        if 'intranet_pass' not in st.session_state:
            st.session_state['intranet_pass'] = ""
        if 'manual_cookie' not in st.session_state:
            st.session_state['manual_cookie'] = ""
            
        i_user = st.text_input("Intranet Username / ID", value=st.session_state['intranet_user'])
        i_pass = st.text_input("Intranet Password", type="password", value=st.session_state['intranet_pass'])
        
        st.markdown("---")
        m_cookie = st.text_input("Manual Session Cookie / ID", value=st.session_state['manual_cookie'], placeholder="e.g. abc123xyz789 or ASP.NET_SessionId=...")
        
        if st.button("Save Intranet Settings"):
            st.session_state['intranet_user'] = i_user
            st.session_state['intranet_pass'] = i_pass
            st.session_state['manual_cookie'] = m_cookie
            st.success("Settings saved for session!")

    # --- MAIN DASHBOARD LAYOUT ---
    left_col, main_col, right_col = st.columns([1, 2.2, 1.2])
    
    with left_col:
        st.markdown("#### Analytics & Fatigue")
        st.markdown("<div class='metric-card'><b>Cumulative Block Hours</b><br><h2 style='color:#00bcd4;'>78 / 85</h2><small>hrs (91%)</small></div>", unsafe_allow_html=True)
        st.markdown("<div class='metric-card'><b>Fatigue Score</b><br><h3 style='color:#ff9800;'>Moderate (6.4/10)</h3><small>Recent red-eye flights detected</small></div>", unsafe_allow_html=True)
        st.markdown("<div class='metric-card'><b>Estimated Allowances</b><br><h3 style='color:#4caf50;'>$1,450 USD</h3><small>Total calculated per diem</small></div>", unsafe_allow_html=True)

    with main_col:
        st.markdown("#### Main Roster Calendar View")
        
        if 'current_roster' not in st.session_state:
            st.session_state['current_roster'] = load_roster_from_db(st.session_state['username'])
            
        with st.expander("📝 Update / Paste Raw Roster Text"):
            roster_input = st.text_area("Paste Roster", value=st.session_state['current_roster'], height=100)
            if st.button("Save & Refresh Roster"):
                if roster_input.strip():
                    save_roster_to_db(st.session_state['username'], roster_input)
                    st.session_state['current_roster'] = roster_input
                    st.success("Saved successfully!")
                    st.rerun()
                else:
                    st.warning("Enter text first.")
                    
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
                today_date = datetime(2026, 8, 30)
                
                if valid_dates:
                    max_date = max(valid_dates)
                    delta = max(0, (max_date - today_date).days)
                    rolling_days = [today_date + timedelta(days=i) for i in range(delta + 1)]
                    
                    start_weekday = today_date.weekday()
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
            st.info("Paste your roster text in the expander above to view your rolling calendar grid.")

    with right_col:
        st.markdown("#### Flight Monitoring Agent")
        
        parsed_rows = parse_roster_text(active_text) if active_text else []
        today_dt = datetime(2026, 8, 30)
        upcoming_flights = get_upcoming_roster_flights(parsed_rows, today_dt)
        
        test_user = st.session_state.get('intranet_user', '')
        test_pass = st.session_state.get('intranet_pass', '')
        test_cookie = st.session_state.get('manual_cookie', '')
        
        flight_check_results = []
        last_success = False
        last_status_code = None
        last_error_msg = "No execution attempted yet"
        
        if upcoming_flights:
            for flight in upcoming_flights:
                res = authenticate_and_fetch_flight_status(
                    test_user, test_pass, flight["flight_no"], flight["date"], flight["dep_stn"], flight["arr_stn"], manual_cookie=test_cookie
                )
                last_success = res.get("success")
                last_status_code = res.get("status_code")
                last_error_msg = res.get("error", "Unknown error")
                if res.get("delayed"):
                    flight_check_results.append({
                        "flight": flight["flight_no"],
                        "route": flight["route"],
                        "status": res.get("status_message")
                    })
        
        with st.expander("🛠️ Intranet Diagnostic Log"):
            st.write(f"**Target User:** {test_user if test_user else 'None Provided'}")
            st.write(f"**Parsed Upcoming Flights:** {[f['flight_no'] for f in upcoming_flights] if upcoming_flights else 'None for today/tomorrow'}")
            st.write(f"**Connection Success:** {last_success}")
            st.write(f"**HTTP Status Code:** {last_status_code}")
            st.write(f"**Details / Error:** {last_error_msg}")

        if flight_check_results:
            for df in flight_check_results:
                st.markdown(f"""
                    <div style='background-color: #2c1f1f; border: 1px solid #ff5252; padding: 12px; border-radius: 8px; margin-bottom: 8px;'>
                        <b style='color: #ff5252;'>⚠️ Disruption Detected ({df['flight']} - {df['route']}):</b><br>{df['status']}<br>
                    </div>
                """, unsafe_allow_html=True)
        else:
            checked_count = len(upcoming_flights)
            flight_names = ', '.join([f['flight_no'] for f in upcoming_flights]) if upcoming_flights else 'None'
            if last_success:
                st.markdown(f"""
                    <div style='background-color: #1b362d; border: 1px solid #4caf50; padding: 12px; border-radius: 8px;'>
                        <b style='color: #4caf50;'>Operational Status Update:</b> Checked {checked_count} flight(s) ({flight_names}). All operating on schedule.<br>
                    </div>
                """, unsafe_allow_html=True)
            else:
                st.markdown(f"""
                    <div style='background-color: #2a2517; border: 1px solid #ff9800; padding: 12px; border-radius: 8px;'>
                        <b style='color: #ff9800;'>Intranet Session Warning:</b> Could not query live i-Fleet API. Provide manual session cookie in sidebar if login challenge persists.<br>
                    </div>
                """, unsafe_allow_html=True)
        
        st.markdown("#### Tactical Bidding")
        st.text_input("Search pairing...", placeholder="Find me a Sydney long stay", label_visibility="collapsed")
        st.markdown("""
            <div style='background-color: #17212b; border: 1px solid #232e3c; padding: 10px; border-radius: 8px; font-size:12px;'>
                <b>SYD-04</b> | Layover: 34h 00m<br>
                <span style='color: #ff9800;'>5 Requests [Low] ⭐ Recommended</span>
            </div>
        """, unsafe_allow_html=True)
