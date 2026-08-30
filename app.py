import streamlit as st
import sqlite3
import hashlib
import pandas as pd
import re

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

# --- 2. ADVANCED PARSER LOGIC ---
def parse_roster_text(raw_text):
    lines = raw_text.split('\n')
    parsed_rows = []
    
    for line in lines:
        line_str = line.strip()
        if "UL" in line_str or "OFF" in line_str or "HTL" in line_str:
            activity_type = "OTHER"
            flight_no = ""
            
            if "OFF" in line_str:
                activity_type = "DAY OFF"
            elif "HTL" in line_str:
                activity_type = "LAYOVER"
            elif "UL" in line_str:
                activity_type = "FLIGHT"
                match = re.search(r'(UL\d+)', line_str)
                if match:
                    flight_no = match.group(1)
            
            parsed_rows.append({
                "Activity Type": activity_type,
                "Flight / Code": flight_no if flight_no else activity_type,
                "Raw Line": line_str
            })
    return parsed_rows

# --- 3. STREAMLIT INTERFACE ---
st.set_page_config(page_title="Crew Companion", page_icon="✈️", layout="wide")
init_db()

if 'logged_in' not in st.session_state:
    st.session_state['logged_in'] = False
    st.session_state['username'] = ''
    st.session_state['full_name'] = ''
    st.session_state['rank'] = ''

# --- SIDEBAR: AUTHENTICATION ---
st.sidebar.image("https://images.unsplash.com/photo-1436491865332-7a61a109cc05?w=400", use_container_width=True)
st.sidebar.title("Crew Portal Login")

if not st.session_state['logged_in']:
    auth_option = st.sidebar.radio("Select Action", ["Login", "Register Account"])
    
    if auth_option == "Login":
        user_input = st.sidebar.text_input("Staff Email / Username", key="login_user")
        pass_input = st.sidebar.text_input("Password", type="password", key="login_pass")
        
        if st.sidebar.button("Log In"):
            user_record = login_user(user_input, pass_input)
            if user_record:
                st.session_state['logged_in'] = True
                st.session_state['username'] = user_record[0]
                st.session_state['full_name'] = user_record[2]
                st.session_state['rank'] = user_record[3]
                st.rerun()
            else:
                st.sidebar.error("Invalid username or password.")
                
    else:
        new_user = st.sidebar.text_input("Choose Username / Email", key="reg_user")
        new_pass = st.sidebar.text_input("Choose Password", type="password", key="reg_pass")
        new_name = st.sidebar.text_input("Full Name", key="reg_name")
        new_rank = st.sidebar.selectbox("Rank", ["Senior Cabin Crew", "Cabin Crew", "Purser", "Flight Deck"], key="reg_rank")
        
        if st.sidebar.button("Register"):
            if new_user.strip() and new_pass.strip() and new_name.strip():
                success = add_user(new_user.strip(), new_pass, new_name.strip(), new_rank)
                if success:
                    st.sidebar.success("Account created successfully! Please switch to Login.")
                else:
                    st.sidebar.error("Username already exists.")
            else:
                st.sidebar.warning("Please fill in all fields.")

else:
    st.sidebar.success(f"Logged in as:\n**{st.session_state['full_name']}**\n*{st.session_state['rank']}*")
    if st.sidebar.button("Log Out"):
        st.session_state['logged_in'] = False
        st.session_state['username'] = ''
        st.session_state['full_name'] = ''
        st.session_state['rank'] = ''
        st.rerun()

# --- MAIN APP VIEW ---
if not st.session_state['logged_in']:
    st.title("✈️ Crew Companion Platform")
    st.markdown("### Welcome to your interactive crew management and analytics hub.")
    st.info("👈 Please **Log In** or **Register Account** using the sidebar to access your private dashboard.")
else:
    st.title(f"Welcome back, {st.session_state['full_name']}!")
    
    tab1, tab2 = st.tabs(["📅 Roster Parser & Dashboard", "⚙️ Account Settings"])
    
    with tab1:
        st.subheader("Your Flight & Roster Dashboard")
        
        if 'current_roster' not in st.session_state:
            st.session_state['current_roster'] = load_roster_from_db(st.session_state['username'])
            
        with st.expander("Update / Paste Raw Roster Text"):
            roster_input = st.text_area("Raw Roster Text", value=st.session_state['current_roster'], height=150)
            if st.button("Save & Parse Roster"):
                if roster_input.strip():
                    save_roster_to_db(st.session_state['username'], roster_input)
                    st.session_state['current_roster'] = roster_input
                    st.success("Roster saved and parsed successfully!")
                    st.rerun()
                else:
                    st.warning("Please paste some roster text first.")
                
        active_text = st.session_state.get('current_roster', '')
        if active_text:
            st.markdown("---")
            
            col1, col2, col3 = st.columns(3)
            col1.metric("Total Recorded Sectors", "8")
            col2.metric("Monthly Block Hours Target", "78 / 85 hrs")
            col3.metric("Next Rest Period", "Compliant")
            
            st.markdown("### Scheduled Activities")
            rows = parse_roster_text(active_text)
            if rows:
                df = pd.DataFrame(rows)
                st.dataframe(df, use_container_width=True, hide_index=True)
            else:
                st.info("No recognized patterns found.")
                
    with tab2:
        st.subheader("User Configuration")
        st.write(f"**Username / Email:** {st.session_state['username']}")
        st.write(f"**Assigned Rank:** {st.session_state['rank']}")
        st.info("Custom preference settings (like preferred layovers and notifications) will appear here soon.")
