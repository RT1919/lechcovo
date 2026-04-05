import os
import secrets
from datetime import datetime
from zoneinfo import ZoneInfo
from flask import Flask, render_template, request, redirect, url_for, flash
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
import psycopg2
from psycopg2.extras import DictCursor

app = Flask(__name__)
app.secret_key = secrets.token_hex(16)

DATABASE_URL = os.environ.get('DATABASE_URL')
ADMIN_SECRET_TOKEN = os.environ.get('ADMIN_SECRET_TOKEN')
LOCAL_TZ = ZoneInfo("Europe/Paris")

# --- DATABASE INITIALIZATION (Runs on startup) ---
def init_db():
    conn = psycopg2.connect(DATABASE_URL, sslmode='require')
    cur = conn.cursor()
    # Create Users
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            name VARCHAR(100) NOT NULL,
            email VARCHAR(100) UNIQUE NOT NULL,
            password TEXT NOT NULL,
            is_admin BOOLEAN DEFAULT FALSE
        );
    """)
    # Create Rides with 'active' column for Soft Delete
    cur.execute("""
        CREATE TABLE IF NOT EXISTS rides (
            id SERIAL PRIMARY KEY,
            user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
            departure VARCHAR(100) NOT NULL,
            destination VARCHAR(100) NOT NULL,
            date VARCHAR(20) NOT NULL,
            time VARCHAR(10) NOT NULL,
            seats INTEGER DEFAULT 1,
            departure_ts BIGINT,
            contact VARCHAR(100),
            active BOOLEAN DEFAULT TRUE
        );
    """)
    # Create Reservations
    cur.execute("""
        CREATE TABLE IF NOT EXISTS reservations (
            id SERIAL PRIMARY KEY,
            user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
            ride_id INTEGER REFERENCES rides(id) ON DELETE CASCADE,
            status VARCHAR(20) DEFAULT 'confirmed',
            UNIQUE(user_id, ride_id)
        );
    """)
    conn.commit()
    cur.close()
    conn.close()

# Run initialization
init_db()

@app.template_filter('date_french')
def date_french(date_str):
    try:
        if len(date_str) == 10 and date_str[2] == '-' and date_str[5] == '-': return date_str
        dt = datetime.strptime(date_str, '%Y-%m-%d')
        return dt.strftime('%d-%m-%Y')
    except: return date_str

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

def get_db():
    return psycopg2.connect(DATABASE_URL, sslmode='require')

class User(UserMixin):
    def __init__(self, id, name, email, is_admin):
        self.id = id
        self.name = name
        self.email = email
        self.is_admin = bool(is_admin)

@login_manager.user_loader
def load_user(user_id):
    conn = get_db()
    cur = conn.cursor(cursor_factory=DictCursor)
    cur.execute("SELECT id, name, email, is_admin FROM users WHERE id = %s", (user_id,))
    u = cur.fetchone()
    cur.close(); conn.close()
    if u: return User(u['id'], u['name'], u['email'], u['is_admin'])
    return None

@app.route('/')
def index():
    conn = get_db()
    cur = conn.cursor(cursor_factory=DictCursor)
    
    # GLOBAL IMPACT (Community)
    cur.execute("SELECT COUNT(*) FROM reservations WHERE status = 'confirmed'")
    global_count = cur.fetchone()[0] or 0
    stats = {"co2": global_count * 2.4, "money": global_count * 4.2, "connections": global_count + 8}

    # Only show active rides
    cur.execute("""
        SELECT r.*, u.name as driver_name FROM rides r 
        LEFT JOIN users u ON r.user_id = u.id 
        WHERE r.active = TRUE ORDER BY r.departure_ts ASC
    """)
    rides = cur.fetchall()
    cur.close(); conn.close()
    return render_template('index.html', rides=rides, stats=stats)

@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if request.method == 'POST':
        is_admin = (request.form.get('admin_token') == ADMIN_SECRET_TOKEN) if ADMIN_SECRET_TOKEN else False
        conn = get_db()
        cur = conn.cursor()
        try:
            cur.execute("INSERT INTO users (name, email, password, is_admin) VALUES (%s, %s, %s, %s)",
                       (request.form['name'], request.form['email'], generate_password_hash(request.form['password']), is_admin))
            conn.commit()
            return redirect(url_for('login'))
        except: flash("Email déjà utilisé.")
        finally: cur.close(); conn.close()
    return render_template('signup.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        conn = get_db()
        cur = conn.cursor(cursor_factory=DictCursor)
        cur.execute("SELECT * FROM users WHERE email = %s", (request.form['email'],))
        u = cur.fetchone()
        cur.close(); conn.close()
        if u and check_password_hash(u['password'], request.form['password']):
            login_user(User(u['id'], u['name'], u['email'], u['is_admin']))
            return redirect(url_for('index'))
        flash("Identifiants incorrects.")
    return render_template('login.html')

@app.route('/publish', methods=['POST'])
@login_required
def publish():
    try:
        raw_date, time_input = request.form['date'], request.form['time']
        local_dt = datetime.strptime(f"{raw_date} {time_input}", "%Y-%m-%d %H:%M").replace(tzinfo=LOCAL_TZ)
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO rides (user_id, departure, destination, date, time, seats, departure_ts, contact, active) 
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, TRUE)
        """, (current_user.id, request.form['departure'], request.form['destination'], raw_date, time_input, int(request.form['seats']), int(local_dt.timestamp()), request.form['contact']))
        conn.commit(); cur.close(); conn.close()
    except Exception as e: flash(f"Erreur: {e}")
    return redirect(url_for('index'))

@app.route("/delete/<int:ride_id>")
@login_required
def delete_ride(ride_id):
    conn = get_db()
    cur = conn.cursor()
    # SOFT DELETE: Ride stays in DB for CO2 stats but disappears from UI
    if current_user.is_admin:
        cur.execute("UPDATE rides SET active = FALSE WHERE id = %s", (ride_id,))
    else:
        cur.execute("UPDATE rides SET active = FALSE WHERE id = %s AND user_id = %s", (ride_id, current_user.id))
    conn.commit(); cur.close(); conn.close()
    return redirect(request.referrer or url_for('index'))

@app.route('/my-account')
@login_required
def my_account():
    conn = get_db()
    cur = conn.cursor(cursor_factory=DictCursor)
    # INDIVIDUAL IMPACT (Count all history)
    cur.execute("SELECT COUNT(*) FROM reservations WHERE user_id = %s AND status = 'confirmed'", (current_user.id,))
    count = cur.fetchone()[0] or 0
    impact = {"co2": count * 2.4, "money": count * 4.2, "rides": count}

    cur.execute("SELECT * FROM rides WHERE user_id = %s AND active = TRUE ORDER BY departure_ts DESC", (current_user.id,))
    driving = cur.fetchall()
    cur.execute("SELECT r.* FROM rides r JOIN reservations res ON r.id = res.ride_id WHERE res.user_id = %s AND r.active = TRUE", (current_user.id,))
    joined = cur.fetchall()
    cur.close(); conn.close()
    return render_template('my_account.html', driving=driving, joined=joined, impact=impact)

@app.route('/reserve/<int:ride_id>')
@login_required
def reserve(ride_id):
    conn = get_db()
    cur = conn.cursor(cursor_factory=DictCursor)
    try:
        cur.execute("SELECT seats, user_id FROM rides WHERE id = %s AND active = TRUE", (ride_id,))
        ride = cur.fetchone()
        if ride and ride['user_id'] != current_user.id:
            status = 'confirmed' if ride['seats'] > 0 else 'waiting'
            cur.execute("INSERT INTO reservations (user_id, ride_id, status) VALUES (%s, %s, %s)", (current_user.id, ride_id, status))
            if status == 'confirmed': cur.execute("UPDATE rides SET seats = seats - 1 WHERE id = %s", (ride_id,))
            conn.commit()
    except: flash("Déjà inscrit.")
    finally: cur.close(); conn.close()
    return redirect(url_for("index"))

@app.route('/unreserve/<int:ride_id>')
@login_required
def unreserve(ride_id):
    conn = get_db()
    cur = conn.cursor(cursor_factory=DictCursor)
    cur.execute("DELETE FROM reservations WHERE user_id = %s AND ride_id = %s RETURNING status", (current_user.id, ride_id))
    res = cur.fetchone()
    if res and res['status'] == 'confirmed':
        cur.execute("UPDATE rides SET seats = seats + 1 WHERE id = %s", (ride_id,))
    conn.commit(); cur.close(); conn.close()
    return redirect(url_for("my_account"))

@app.route('/logout')
def logout():
    logout_user()
    return redirect(url_for('index'))

if __name__ == "__main__":
    app.run()