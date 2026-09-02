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
    "User-Agent": 
