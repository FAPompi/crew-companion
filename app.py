import streamlit as st
import sqlite3
import hashlib

# --- 1. DATABASE SETUP ---
def init_db():
    conn = sqlite3.connect('crew_companion.db')
    c = conn.cursor()
    # Create table for crew users
    c.execute('''
        CREATE TABLE IF NOT EXISTS users (
            username TEXT PRIMARY KEY,
            password TEXT,
            full_name TEXT,
            rank TEXT
        )
    ''')
    conn.commit()
    conn.close()

# Hash passwords for security
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
    if data:
        if check_hash(password, data[1]):
            return data # Returns (username, hashed_password, full_name, rank)
    return None

# --- 2. STREAMLIT INTERFACE ---
st.set_page_config(page_title="Crew Companion", page_icon="✈️", layout="wide")

init_db()

# Session state management for login
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
                
    else: # Register
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

# --- 3. MAIN APP VIEW ---
if not st.session_state['logged_in']:
    st.title("✈️ Crew Companion Platform")
    st.markdown("### Welcome to your interactive crew management and analytics hub.")
    st.info("👈 Please **Log In** or **Register Account** using the sidebar to access your private roster dashboard.")
else:
    st.title(f"Welcome back, {st.session_state['full_name']}!")
    st.write(f"**Rank:** {st.session_state['rank']}")
    st.success("Database connection active. Secure session initialized.")
    
    # Placeholder for the next step (Roster Upload & Dashboard)
    st.markdown("---")
    st.info("Next up: We will add the **Roster Ingestion and Parsing Engine** right here so you can paste or upload your schedule and see the visual dashboard.")
