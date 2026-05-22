from flask import Flask, render_template, request, redirect, url_for, flash, session , jsonify, json
import re
import uuid
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
from flask_dance.contrib.google import make_google_blueprint, google
from flask_login import LoginManager, UserMixin, login_user, logout_user, current_user
from flask_mail import Mail, Message
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail as SGMail
import os, time, random
from jinja2 import Undefined
from datetime import datetime, timedelta
import time
import requests
from collections import defaultdict
import psycopg2
import psycopg2.extras
from dotenv import load_dotenv
from psycopg2 import pool
from supabase import create_client, Client
import bcrypt

# Generate shipping dates (delivery 5–6 days from now)
today = datetime.now()
shipping_start = today + timedelta(days=5)
shipping_end = today + timedelta(days=6)

# Format dates → "16 Oct"
shipping_start_str = shipping_start.strftime("%d %b")
shipping_end_str = shipping_end.strftime("%d %b")
# --------------------------
# Flask Setup
# --------------------------
app = Flask(__name__)

load_dotenv()   # MOVE THIS HERE FIRST

try:
    print("CONNECTING TO DATABASE...")

    db_pool = psycopg2.pool.SimpleConnectionPool(
        1,
        20,
        host=os.getenv("DB_HOST"),
        database=os.getenv("DB_NAME"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        port=os.getenv("DB_PORT", 5432),
        sslmode="require",
        connect_timeout=10
    )

    print("DATABASE CONNECTED!")

except Exception as e:
    print("DATABASE CONNECTION ERROR:")
    print(str(e))
    raise e
    
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_SECURE'] = False

app.config['MAIL_SERVER'] = 'smtp-relay.brevo.com'
app.config['MAIL_PORT'] = 587
app.config['MAIL_USE_TLS'] = True
app.config['MAIL_USE_SSL'] = False
app.config['MAIL_USERNAME'] = os.getenv("SMTP_USER")
app.config['MAIL_PASSWORD'] = os.getenv("SMTP_KEY")
app.config['MAIL_DEFAULT_SENDER'] = ('FitHub', 'mariaviezelmanzano@gmail.com')
UPLOAD_FOLDER = 'static/uploads/sellers'
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "webp"}
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
mail = Mail(app)
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = "sb_publishable_-Jn8Wh89YI5Zhi_u5iGZaQ_o1fZ01EJ"
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
app.secret_key = "ceb91904e927c351a7b81cce0fa0384fd181ac541087ac909b64d80710d700f9"
# mail = Mail(app)
# load_dotenv()

# --------------------------
# PostgreSQL Connection
# --------------------------

SUPABASE_STORAGE_URL = f"{SUPABASE_URL}/storage/v1/object/public/products"

def product_image_url(filename):
    if not filename:
        return url_for('static', filename='uploads/sellers/default.png')
    # Already a full URL
    if filename.startswith('http'):
        return filename
    # Supabase files have a timestamp prefix pattern: digits_filename
    if re.match(r'^\d+_', filename):
        return f"{SUPABASE_STORAGE_URL}/{filename}"
    # Local file — serve from static
    return url_for('static', filename=f'uploads/sellers/{filename}')

app.jinja_env.globals['product_image_url'] = product_image_url

def get_db():
    return db_pool.getconn()
# def get_db():
#     return db_pool.getconn(
#         host=os.getenv("DB_HOST"),
#         database=os.getenv("DB_NAME"),
#         user=os.getenv("DB_USER"),
#         password=os.getenv("DB_PASSWORD"),
#         port=os.getenv("DB_PORT", 5432)
#     )

def upload_to_supabase_storage(file, bucket_name="products"):
    """Upload file to Supabase Storage and return the filename"""
    try:
        # Read file bytes
        file_bytes = file.read()
        
        # Generate unique filename
        filename = secure_filename(file.filename)
        unique_filename = f"{int(time.time() * 1000)}_{filename}"
        
        # Upload to Supabase Storage
        supabase.storage.from_(bucket_name).upload(
            path=unique_filename,
            file=file_bytes,
            file_options={"content-type": file.content_type}
        )
        
        # Reset file pointer for potential re-reading
        file.seek(0)
        
        return unique_filename
    except Exception as e:
        print(f"Upload error: {e}")
        raise e

def safe_json_load(value):
    if isinstance(value, str):
        return json.loads(value)
    return value or {}
# --------------------------
# Login Manager
# --------------------------
login_manager = LoginManager(app)
login_manager.login_view = 'login'

# --------------------------
# Google OAuth Setup
# --------------------------
google_bp = make_google_blueprint(
        client_id=os.getenv("GOOGLE_CLIENT_ID"),
        client_secret=os.getenv("GOOGLE_CLIENT_SECRET"),
    scope=[
        "openid",
        "https://www.googleapis.com/auth/userinfo.email",
        "https://www.googleapis.com/auth/userinfo.profile"
    ],
    redirect_to="google_login"
)
google_bp.authorization_url_params = {"prompt": "select_account"}
app.register_blueprint(google_bp)

def clean_value(val):
    if isinstance(val, Undefined):
        return None
    return val

# --------------------------
# User Class
# --------------------------
class User(UserMixin):
    def __init__(self, id, username, email, photo=None):
        self.id = id
        self.username = username
        self.email = email
        self.photo = photo

def geocode_address(address):
    try:
        url = "https://nominatim.openstreetmap.org/search"
        params = {"q": f"{address}, Philippines", "format": "json"}
        headers = {"User-Agent": "FitHubApp/1.0"}
        resp = requests.get(url, params=params, headers=headers, timeout=5)
        data = resp.json()
        if data:
            return float(data[0]["lat"]), float(data[0]["lon"])
    except Exception as e:
        print("Geocoding error for", address, e)
    return None, None

def normalize_name(text):
    if not text:
        return ""
    text = text.strip().lower()
    parts = text.replace("-", " - ").split()
    formatted = []
    for p in parts:
        if p == "-":
            formatted.append("-")
        else:
            formatted.append(p.capitalize())
    return " ".join(formatted).replace(" - ", "-")

def clean_street_name(street):
    if not street:
        return ""
    street = street.strip()
    suffixes = ["street", "st.", "st", "avenue", "ave.", "ave", "road", "rd.", "rd", "lane", "ln.", "ln"]
    for suffix in suffixes:
        if street.lower().endswith(suffix):
            street = street[: -len(suffix)].strip()
    return street

def normalize_text(text):
    if not text:
        return ""
    return " ".join([p.capitalize() for p in text.strip().split()])

@login_manager.user_loader
def load_user(user_id):
    conn = get_db()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cursor.execute("SELECT * FROM users WHERE user_id = %s", (user_id,))
    account = cursor.fetchone()
    db_pool.putconn(conn)
    if account:
        return User(id=account['user_id'], username=account['username'],
                    email=account['email'], photo=account.get('photo'))
    return None

def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

# --------------------------
# Routes
# --------------------------
@app.route("/")
def index():
    return redirect(url_for("login"))


@app.route("/signup_users")
def signup_users():
    if request.method == 'POST':
        try:
            first_name = normalize_name(request.form['first_name'])
            middle_name = normalize_name(request.form['middle_name'])
            last_name = normalize_name(request.form['last_name'])
            suffix = request.form.get('suffix', '').upper()
            email = request.form['email']
            password = request.form['password']
            confirm_password = request.form['confirm_password']
            mobile = request.form['mobile']
            payout = normalize_name(request.form['payout'])
            region = request.form['region']
            province = request.form['province']
            city = request.form['city']
            barangay = request.form['barangay']
            postal = request.form['postal']
            street = normalize_name(clean_street_name(request.form['street']))
            otp_input = request.form['otp_code']
        except Exception as e:
            flash("Form data error.", "error")
            return render_template("signup_users.html", form=request.form)

        if password != confirm_password:
            flash("Passwords do not match!", 'error')
            return render_template("signup_users.html", form=request.form)
        if len(password) < 8:
            flash("Password must be at least 8 characters!", 'error')
            return render_template("signup_users.html", form=request.form)

        conn = get_db()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute("SELECT email FROM users WHERE email=%s", (email,))
        if cursor.fetchone():
            flash("Email already registered!", "error")
            return render_template("signup_users.html", form=request.form)

        otp_saved = session.get("otp_code")
        otp_email = session.get("otp_email")
        if not otp_saved or otp_input != otp_saved or email != otp_email:
            flash("Invalid OTP.", "error")
            return render_template("signup_users.html", form=request.form)

        def save_file(file_field):
            if file_field and file_field.filename != "":
                filename = secure_filename(file_field.filename)
                unique_name = datetime.now().strftime("%Y%m%d%H%M%S_") + filename
                file_field.save(os.path.join(app.config['UPLOAD_FOLDER'], unique_name))
                return unique_name
            return None

        payout_doc = save_file(request.files.get('payout_doc'))
        hashed_pw = generate_password_hash(password)

        try:
            cursor.execute("""
                INSERT INTO users 
                (first_name, middle_name, last_name, suffix, email, mobile, role, payout, password,
                region, province, city, barangay, postal, street, verified)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """, (first_name, middle_name, last_name, suffix, email, mobile, "seller", payout, hashed_pw,
                  region, province, city, barangay, postal, street, 'pending'))
            conn.commit()
            flash("Account successfully created! Wait for the admin to approve your account.", "success")
        except Exception as e:
            conn.rollback()
            return "DB ERROR: " + str(e)
        finally:
            cursor.close()
            db_pool.putconn(conn)

        session.pop("otp_code", None)
        session.pop("otp_email", None)
        session.pop("otp_time", None)
        return redirect(url_for("login"))

    return render_template("signup_users.html", form=request.form)


@app.route('/login', methods=['GET', 'POST'])
def login():
    msg = ''
    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')
        role_choice = request.form.get('role')

        if not role_choice:
            flash("Please select Buyer / Seller / Rider", "error")
            return redirect(url_for("login"))

        conn = get_db()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        if role_choice == "buyer":
            table = "users"
            verify_column = "is_verified"
        elif role_choice == "seller":
            table = "sellers"
            verify_column = "verified"
        elif role_choice == "rider":
            table = "riders"
            verify_column = "verified"
        else:
            flash("Invalid role selected.", "error")
            return redirect(url_for("login"))

        cursor.execute(f"SELECT * FROM {table} WHERE email = %s", (email,))
        account = cursor.fetchone()
        db_pool.putconn(conn)

        if account is None:
            flash("Email not found.", "error")
            return redirect(url_for("login"))

        stored_hash = account['password']

        if not bcrypt.checkpw(
            password.encode('utf-8'),
            stored_hash.encode('utf-8')
        ):
            flash("Incorrect password.", "error")
            return redirect(url_for("login"))
        
        # if role_choice == "buyer":
        #     if account[verify_column] != 1:
        #         flash("Please verify your email first.", "error")
        #         return redirect(url_for("login"))
        #     buyer_status = account.get('verified', 'enabled').lower()
        #     if buyer_status == "disabled":
        #         flash("Your account has been disabled.", "error")
        #         return redirect(url_for("login"))
        # else:
        #     status = account[verify_column].lower()
        #     if status == "disabled":
        #         flash("Your account has been disabled.", "error")
        #         return redirect(url_for("login"))
        #     if status == "pending":
        #         flash("Your application is still pending.", "warning")
        #         return redirect(url_for("login"))
        #     if status == "rejected":
        #         flash("Your application was rejected.", "error")
        #         return redirect(url_for("login"))
        #     if status not in ["approved", "enabled"]:
        #         flash("Unable to log in. Your account is not approved yet.", "error")
        #         return redirect(url_for("login"))

        session['loggedin'] = True
        session['id'] = account[list(account.keys())[0]]
        session['email'] = account['email']
        session['role'] = account['role']
        session['photo'] = account.get("photo") or "default_photo_1.png"

        if role_choice == "buyer":
            session['user_id'] = account['user_id']
            redirect_page = session.pop("redirect_after_login", None)
            if redirect_page:
                return redirect(redirect_page)
            return redirect(url_for("homepage"))
        elif role_choice == "seller":
            session['seller_id'] = account['seller_id']
            session['first_name'] = account['first_name']
            store_logo_filename = account.get('store_logo') or 'default-user.jpg'
            session['store_logo'] = url_for('static', filename=f'uploads/sellers/{store_logo_filename}')
            return redirect(url_for("seller_dashboard"))
        elif role_choice == "rider":
            session['rider_id'] = account['rider_id']
            session['first_name'] = account['first_name']
            return redirect(url_for("driver_dashboard"))

    return render_template('login.html', msg=msg)


@app.route("/login_admin", methods=["GET", "POST"])
def login_admin():
    if request.method == "POST":
        email = request.form.get("email")
        password = request.form.get("password")

        conn = get_db()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute("SELECT * FROM admin WHERE admin_email = %s LIMIT 1", (email,))
        admin = cursor.fetchone()
        db_pool.putconn(conn)

        if not admin:
            flash("Email not found", "error")
            return redirect(url_for("login_admin"))
        if admin["admin_password"] != password:
            flash("Incorrect password", "error")
            return redirect(url_for("login_admin"))

        session["admin_id"] = admin["admin_id"]
        session["admin_username"] = admin["admin_username"]
        session["admin_fullname"] = admin["admin_fullname"]
        return redirect(url_for("admin_dashboard"))

    return render_template("login_admin.html")


import random

@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        username = request.form['username']
        email = request.form['email']
        password = request.form['password']
        confirm = request.form['confirm_password']
        role = request.form['account_type']

        conn = get_db()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        cursor.execute("SELECT email FROM users WHERE email=%s", (email,))
        if cursor.fetchone():
            flash("Email already exists!", "error")
            return redirect("/signup")

        if password != confirm:
            flash("Passwords do not match!", "error")
            return redirect("/signup")

        if len(password) < 8:
            flash("Password must be at least 8 characters!", "error")
            return redirect("/signup")

        hashed = generate_password_hash(password)
        otp = str(random.randint(100000, 999999))
        default_photos = ['default_photo_1.png','default_photo_2.png','default_photo_3.png',
                          'default_photo_4.png','default_photo_5.png','default_photo_6.png']
        chosen_photo = random.choice(default_photos)

        cursor.execute("""
            INSERT INTO users (username, email, password, role, otp_code, is_verified, verified, photo)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """, (username, email, hashed, role, otp, 0, "approved", chosen_photo))
        conn.commit()
        db_pool.putconn(conn)

        msg = Message(subject="FitHub Email Verification", recipients=[email],
                      body=f"Hello {username},\n\nYour OTP verification code is: {otp}")
        mail.send(msg)
        session['pending_email'] = email
        return redirect("/verify")

    return render_template("signup.html")


@app.route('/signup_sellers', methods=['GET', 'POST'])
def signup_sellers():
    if request.method == 'POST':
        try:
            first_name = normalize_name(request.form['first_name'])
            middle_name = normalize_name(request.form['middle_name'])
            last_name = normalize_name(request.form['last_name'])
            suffix = request.form.get('suffix', '').upper()
            email = request.form['email']
            password = request.form['password']
            confirm_password = request.form['confirm_password']
            mobile = request.form['mobile']
            payout = normalize_name(request.form['payout'])
            business_name = request.form['business_name']
            business_type = request.form['business_type']
            region = request.form['region']
            province = request.form['province']
            city = request.form['city']
            barangay = request.form['barangay']
            postal = request.form['postal']
            street = normalize_name(clean_street_name(request.form['street']))
            operating_hours = request.form.get('operating_hours', '')
            otp_input = request.form['otp_code']
        except Exception as e:
            flash("Form data error.", "error")
            return render_template("signup_sellers.html", form=request.form)

        if password != confirm_password:
            flash("Passwords do not match!", 'error')
            return render_template("signup_sellers.html", form=request.form)
        if len(password) < 8:
            flash("Password must be at least 8 characters!", 'error')
            return render_template("signup_sellers.html", form=request.form)

        conn = get_db()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute("SELECT email FROM sellers WHERE email=%s", (email,))
        if cursor.fetchone():
            flash("Email already registered!", "error")
            return render_template("signup_sellers.html", form=request.form)

        otp_saved = session.get("otp_code")
        otp_email = session.get("otp_email")
        if not otp_saved or otp_input != otp_saved or email != otp_email:
            flash("Invalid OTP.", "error")
            return render_template("signup_sellers.html", form=request.form)

        def save_file(file_field):
            if file_field and file_field.filename != "":
                filename = secure_filename(file_field.filename)
                unique_name = datetime.now().strftime("%Y%m%d%H%M%S_") + filename
                file_field.save(os.path.join(app.config['UPLOAD_FOLDER'], unique_name))
                return unique_name
            return None

        store_logo = save_file(request.files.get('store_logo'))
        id_file = save_file(request.files.get('id_file'))
        permit_file = save_file(request.files.get('permit_file'))
        payout_doc = save_file(request.files.get('payout_doc'))

        if not store_logo:
            store_logo = random.choice(['default_photo_1.png','default_photo_2.png',
                                        'default_photo_3.png','default_photo_4.png',
                                        'default_photo_5.png','default_photo_6.png'])

        hashed_pw = generate_password_hash(password)

        try:
            cursor.execute("""
                INSERT INTO sellers 
                (first_name, middle_name, last_name, suffix, email, mobile, role, payout, password,
                business_name, business_type, region, province, city, barangay, postal, street,
                operating_hours, store_logo, id_file, permit_file, payout_doc, verified)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """, (first_name, middle_name, last_name, suffix, email, mobile, "seller", payout, hashed_pw,
                  business_name, business_type, region, province, city, barangay, postal, street,
                  operating_hours, store_logo, id_file, permit_file, payout_doc, 'pending'))
            conn.commit()
            flash("Account successfully created! Wait for the admin to approve your account.", "success")
        except Exception as e:
            conn.rollback()
            return "DB ERROR: " + str(e)
        finally:
            cursor.close()
            db_pool.putconn(conn)

        session.pop("otp_code", None)
        session.pop("otp_email", None)
        session.pop("otp_time", None)
        return redirect(url_for("login"))

    return render_template("signup_sellers.html", form=request.form)


@app.route("/signup_rider", methods=["GET", "POST"])
def signup_rider():
    if request.method == "POST":
        try:
            first_name = normalize_name(request.form['first_name'])
            middle_name = normalize_name(request.form['middle_name'])
            last_name = normalize_name(request.form['last_name'])
            suffix = request.form['suffix'].upper() if request.form['suffix'] else ""
            email = request.form['email']
            mobile = request.form['mobile']
            password = request.form['password']
            confirm_password = request.form['confirm_password']
            region = request.form['region']
            province = request.form['province']
            city = request.form['city']
            barangay = request.form['barangay']
            postal = request.form['postal']
            street = normalize_name(clean_street_name(request.form['street']))
            working_hours = request.form['working_hours']
            payout = normalize_name(request.form['payout'])
            vehicle_type = normalize_name(request.form['vehicle_type'])
            plate = request.form['plate'].upper().strip()
            otp_input = request.form['otp_code']
        except Exception as e:
            flash("Form data error.", "error")
            return render_template("signup_rider.html", form=request.form)

        if password != confirm_password:
            flash("Passwords do not match!", 'error')
            return render_template("signup_rider.html", form=request.form)
        if len(password) < 8:
            flash("Password must be at least 8 characters!", 'error')
            return render_template("signup_rider.html", form=request.form)

        conn = get_db()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute("SELECT email FROM riders WHERE email=%s", (email,))
        if cursor.fetchone():
            flash("Email already registered!", "error")
            return render_template("signup_rider.html", form=request.form)
        cursor.execute("SELECT mobile FROM riders WHERE mobile=%s", (mobile,))
        if cursor.fetchone():
            flash("Mobile number already registered!", "error")
            return render_template("signup_rider.html", form=request.form)

        otp_saved = session.get("otp_code")
        otp_email = session.get("otp_email")
        if not otp_saved or otp_input != otp_saved or email != otp_email:
            flash("Invalid OTP.", "error")
            return render_template("signup_rider.html", form=request.form)

        def save_file(file_field):
            if file_field and file_field.filename != "":
                filename = secure_filename(file_field.filename)
                unique_name = datetime.now().strftime("%Y%m%d%H%M%S_") + filename
                file_field.save(os.path.join(app.config['UPLOAD_FOLDER'], unique_name))
                return unique_name
            return None

        id_file = save_file(request.files.get("id_file"))
        license_file = save_file(request.files.get("license"))
        orcr_file = save_file(request.files.get("orcr"))
        hashed_pw = generate_password_hash(password)

        try:
            cursor.execute("""
                INSERT INTO riders (
                    first_name, middle_name, last_name, suffix, email, mobile, role,
                    region, province, city, barangay, postal, street,
                    working_hours, payout, valid_id, license, vehicle_type,
                    plate, orcr, otp_code, verified, password
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """, (first_name, middle_name, last_name, suffix, email, mobile, "rider",
                  region, province, city, barangay, postal, street,
                  working_hours, payout, id_file, license_file, vehicle_type,
                  plate, orcr_file, otp_input, 'pending', hashed_pw))
            conn.commit()
        except Exception as e:
            conn.rollback()
            return "DB ERROR: " + str(e)
        finally:
            cursor.close()
            db_pool.putconn(conn)

        session.pop("otp_code", None)
        session.pop("otp_email", None)
        flash("Account successfully created! Wait for the admin to approve your account.", "success")
        return redirect(url_for("login"))

    return render_template("signup_rider.html", form=request.form)


@app.route("/send_otp", methods=["POST"])
def send_otp():
    data = request.get_json()
    email = data.get("email")
    if not email:
        return jsonify({"success": False, "message": "Email is required"})
    otp = str(random.randint(100000, 999999))
    session["otp_code"] = otp
    session["otp_email"] = email
    try:
        msg = Message(subject="FitHub Email Verification", recipients=[email],
                      body=f"\nYour OTP verification code is: {otp}")
        mail.send(msg)
        return jsonify({"success": True, "message": "OTP sent successfully!"})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})


@app.route("/verify", methods=["GET", "POST"])
def verify():
    if request.method == "POST":
        otp_input = request.form['otp']
        email = session.get('pending_email')
        conn = get_db()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute("SELECT otp_code FROM users WHERE email=%s", (email,))
        account = cursor.fetchone()
        if account and otp_input == account['otp_code']:
            cursor.execute("UPDATE users SET is_verified=1, otp_code=NULL WHERE email=%s", (email,))
            conn.commit()
            db_pool.putconn(conn)
            return redirect(url_for("login"))
        db_pool.putconn(conn)
        flash("Invalid OTP! Try again.", "error")
    return render_template("verify.html")


@app.route("/resend")
def resend():
    email = session.get('pending_email')
    if not email:
        flash("No email to resend verification code.", "error")
        return redirect(url_for("signup"))
    conn = get_db()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    new_otp = str(random.randint(100000, 999999))
    cursor.execute("UPDATE users SET otp_code=%s WHERE email=%s", (new_otp, email))
    conn.commit()
    db_pool.putconn(conn)
    msg = Message(subject="FitHub - New Verification Code", recipients=[email],
                  body=f"Your new OTP verification code is: {new_otp}")
    mail.send(msg)
    flash("A new verification code has been sent to your email.")
    return redirect(url_for("verify"))


@app.route("/google_login")
def google_login():
    if not google.authorized:
        return redirect(url_for("google.login"))
    resp = google.get("https://www.googleapis.com/oauth2/v3/userinfo")
    if not resp.ok:
        flash("Google login failed. Please try again.", "error")
        return redirect(url_for("login"))
    user_info = resp.json()
    email = user_info.get("email")
    username = user_info.get("name", email.split("@")[0])
    conn = get_db()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cursor.execute("SELECT * FROM users WHERE email = %s", (email,))
    account = cursor.fetchone()
    if not account:
        default_photo = "default.png"
        cursor.execute(
            "INSERT INTO users (username, email, password, photo, role, is_verified) VALUES (%s, %s, %s, %s, %s, %s)",
            (username, email, "", default_photo, "buyer", 1))
        conn.commit()
        cursor.execute("SELECT * FROM users WHERE email = %s", (email,))
        account = cursor.fetchone()
    db_pool.putconn(conn)
    if account['is_verified'] != 1:
        flash("Please verify your email first.", "error")
        return redirect(url_for("login"))
    buyer_status = account.get('verified', 'enabled').lower()
    if buyer_status == "disabled":
        flash("Your account has been disabled. Please contact support.", "error")
        return redirect(url_for("login"))
    session['loggedin'] = True
    session['user_id'] = account['user_id']
    session['email'] = account['email']
    session['role'] = 'buyer'
    session['photo'] = url_for('static', filename=f"uploads/sellers/{account['photo']}")
    redirect_page = session.pop("redirect_after_login", None)
    if redirect_page:
        return redirect(redirect_page)
    return redirect(url_for("homepage"))


@app.route("/terms_and_conditions")
def terms():
    return render_template("terms.html")


def build_category_tree(cursor):
    cursor.execute("""
        SELECT main_category, category_name, sub_category, image, sub_image
        FROM categories ORDER BY main_category, category_name, sub_category
    """)
    rows = cursor.fetchall()
    category_tree = {}
    for r in rows:
        main = r["main_category"]
        cat = r["category_name"]
        sub = r["sub_category"]
        main_img = r["image"] or "default-category.jpg"
        sub_img = r["sub_image"] or main_img
        if main not in category_tree:
            category_tree[main] = {}
        if cat not in category_tree[main]:
            category_tree[main][cat] = {"image": main_img, "subcats": []}
        if sub:
            category_tree[main][cat]["subcats"].append({"name": sub, "image": sub_img})
    return category_tree


def get_cart_count(cursor, user_id):
    if not user_id:
        return 0
    cursor.execute("SELECT COUNT(cart_id) AS total FROM cart WHERE user_id = %s", (user_id,))
    result = cursor.fetchone()
    return result["total"] if result and result["total"] else 0


@app.route("/homepage")
def homepage():
    conn = get_db()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cursor.execute("""
        SELECT * FROM products WHERE status = 'approved'
        ORDER BY created_at DESC LIMIT 12
    """)
    products = cursor.fetchall()

    for p in products:
        if p["color_price"]:
            try:
                color_price = p["color_price"]

                prices = [float(v) for v in color_price.values() if v]

                p["min_price"] = min(prices) if prices else 0
                p["max_price"] = max(prices) if prices else 0

            except Exception as e:
                print("PRICE ERROR:", e)
                p["min_price"] = p["max_price"] = 0
        else:
            p["min_price"] = p["max_price"] = 0

    product_ids = [p['product_id'] for p in products]
    if product_ids:
        format_strings = ','.join(['%s'] * len(product_ids))
        cursor.execute(f"""
            SELECT product_id, SUM(quantity) as total_sold FROM order_items
            WHERE status='Delivered' AND product_id IN ({format_strings})
            GROUP BY product_id
        """, tuple(product_ids))
        sold_counts = {row['product_id']: row['total_sold'] for row in cursor.fetchall()}
    else:
        sold_counts = {}

    for p in products:
        p['total_sold'] = sold_counts.get(p['product_id'], 0)

    # PostgreSQL requires all non-aggregate columns in GROUP BY
    cursor.execute("""
        SELECT oi.product_id, p.name AS product_name, p.main_image, p.color_price,
               SUM(oi.quantity) AS total_sold
        FROM order_items oi
        JOIN products p ON oi.product_id = p.product_id
        WHERE oi.status = 'Delivered' AND p.status = 'approved'
        GROUP BY oi.product_id, p.name, p.main_image, p.color_price
        ORDER BY total_sold DESC LIMIT 10
    """)
    top_products = cursor.fetchall()

    for t in top_products:
        if t["color_price"]:
            try:
                color_price = t["color_price"]

                prices = [float(v) for v in color_price.values() if v]

                t["min_price"] = min(prices) if prices else 0
                t["max_price"] = max(prices) if prices else 0

            except Exception as e:
                print("TOP PRICE ERROR:", e)
                t["min_price"] = t["max_price"] = 0
        else:
            t["min_price"] = t["max_price"] = 0

    cart_count = get_cart_count(cursor, session.get("user_id"))
    category_tree = build_category_tree(cursor)
    db_pool.putconn(conn)
    return render_template("homepage.html", products=products, top_products=top_products,
                           cart_count=cart_count, category_tree=category_tree)


@app.route("/category")
def category():
    main = request.args.get("main")
    cat = request.args.get("cat")
    sub = request.args.get("sub")
    search_query = request.args.get("q")
    top = request.args.get("top")

    conn = get_db()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    products = []

    if top == "1":
        cursor.execute("SELECT p.product_id, p.name, p.main_image, p.color_price FROM products p")
        products = cursor.fetchall()
    else:
        query = """
            SELECT p.* FROM products p
            JOIN categories c ON p.category_id = c.category_id WHERE 1=1
        """
        params = []
        if main:
            query += " AND c.main_category = %s"; params.append(main)
        if cat:
            query += " AND c.category_name = %s"; params.append(cat)
        if sub:
            query += " AND c.sub_category = %s"; params.append(sub)
        if search_query:
            query += " AND p.name LIKE %s"; params.append(f"%{search_query}%")
        query += " ORDER BY p.created_at DESC"
        cursor.execute(query, tuple(params))
        products = cursor.fetchall()

    for p in products:
        try:
            if p.get("color_price"):
                cp = json.loads(p["color_price"])
                values = [float(v) for v in cp.values() if v]
                p["min_price"] = min(values) if values else 0
                p["max_price"] = max(values) if values else 0
            else:
                p["min_price"] = p["max_price"] = 0
        except:
            p["min_price"] = p["max_price"] = 0

    if products:
        product_ids = [p['product_id'] for p in products]
        format_strings = ','.join(['%s'] * len(product_ids))
        cursor.execute(f"""
            SELECT product_id, SUM(quantity) AS total_sold FROM order_items
            WHERE status='Delivered' AND product_id IN ({format_strings})
            GROUP BY product_id
        """, tuple(product_ids))
        sold_counts = {row['product_id']: row['total_sold'] for row in cursor.fetchall()}
        for p in products:
            p['total_sold'] = sold_counts.get(p['product_id'], 0)

    cart_count = get_cart_count(cursor, session.get("user_id"))
    category_tree = build_category_tree(cursor)
    db_pool.putconn(conn)

    display_cat = "Top Products" if top == "1" else cat
    return render_template("category.html", products=products,
                           main=None if top == "1" else main,
                           cat=display_cat, sub=None if top == "1" else sub,
                           category_tree=category_tree,
                           search_query=None if top == "1" else search_query,
                           cart_count=cart_count)


@app.route("/admin_delete_category/<int:category_id>", methods=["POST"])
def admin_delete_category(category_id):
    try:
        conn = get_db()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute("DELETE FROM categories WHERE category_id = %s", (category_id,))
        conn.commit()
        db_pool.putconn(conn)
        return jsonify({"status": "success", "message": "Category deleted successfully!"})
    except Exception as e:
        return jsonify({"status": "error", "message": "Failed to delete category."})


@app.route("/view_product/<int:product_id>")
def view_product(product_id):
    conn = get_db()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cursor.execute("""
        SELECT p.*, c.main_category, c.category_name, c.sub_category
        FROM products p LEFT JOIN categories c ON p.category_id = c.category_id
        WHERE p.product_id = %s
    """, (product_id,))
    product = cursor.fetchone()
    if not product:
        return "Product not found", 404

    cursor.execute("""
        SELECT seller_id, business_name, region, province, store_logo
        FROM sellers WHERE seller_id = %s
    """, (product['seller_id'],))
    seller = cursor.fetchone()

    gallery = [img for img in product["gallery_images"].split(",") if img.strip()] if product["gallery_images"] else []

    color_list = []
    color_image_map = {}
    if product.get("color_images"):
        try:
            # color_image_map = json.loads(product["color_images"])
            color_image_map = safe_json_load(product.get("color_images"))
            color_list = list(color_image_map.keys())
        except:
            pass

    # color_price_map = json.loads(product["color_price"]) if product.get("color_price") else {}
    color_price_map = safe_json_load(product.get("color_price"))
    color_original_price_map = safe_json_load(product.get("color_original_price"))
    color_stock_map = safe_json_load(product.get("color_stock"))
    color_original_price_map = safe_json_load(product.get("color_original_price"))
    color_stock_map = safe_json_load(product.get("color_stock"))
    specs = (product.get("specifications") or "").strip()

    cursor.execute("""
        SELECT r.*, u.username, u.photo AS user_photo FROM product_reviews r
        LEFT JOIN users u ON r.user_id = u.user_id
        WHERE r.product_id = %s ORDER BY r.created_at DESC
    """, (product_id,))
    fetched_reviews = cursor.fetchall()
    reviews = []
    for review in fetched_reviews:
        try:
            review['photos'] = safe_json_load(review['photos']) if review['photos'] else []
        except:
            review['photos'] = []
        reviews.append(review)

    per_page = 3
    total_reviews = len(reviews)
    total_pages = (total_reviews + per_page - 1) // per_page

    category_tree = build_category_tree(cursor)
    cart_count = get_cart_count(cursor, session.get("user_id"))

    cursor.execute("SELECT rating FROM product_reviews WHERE product_id = %s", (product_id,))
    ratings = cursor.fetchall()
    if ratings:
        ratings_list = [r['rating'] for r in ratings]
        avg_rating = round(sum(ratings_list) / len(ratings_list), 1)
        total_ratings = len(ratings_list)
    else:
        avg_rating = 0
        total_ratings = 0

    cursor.execute("""
        SELECT SUM(CASE WHEN rating=5 THEN 1 ELSE 0 END) AS r5,
               SUM(CASE WHEN rating=4 THEN 1 ELSE 0 END) AS r4,
               SUM(CASE WHEN rating=3 THEN 1 ELSE 0 END) AS r3,
               SUM(CASE WHEN rating=2 THEN 1 ELSE 0 END) AS r2,
               SUM(CASE WHEN rating=1 THEN 1 ELSE 0 END) AS r1
        FROM product_reviews WHERE product_id = %s
    """, (product_id,))
    rating_counts = cursor.fetchone()

    cursor.execute("SELECT COUNT(*) AS comments_count FROM product_reviews WHERE product_id=%s AND review IS NOT NULL AND review != ''", (product_id,))
    comments_count = cursor.fetchone()["comments_count"]

    cursor.execute("SELECT COUNT(*) AS media_count FROM product_reviews WHERE product_id=%s AND photos IS NOT NULL AND photos != '[]'::jsonb AND photos != '[]'", (product_id,))
    media_count = cursor.fetchone()["media_count"]

    cursor.execute("SELECT SUM(quantity) as total_sold FROM order_items WHERE product_id=%s AND status='Delivered'", (product_id,))
    sold_data = cursor.fetchone()
    total_sold = sold_data['total_sold'] or 0
    if total_sold >= 1000000:
        sold_display = f"{total_sold//1000000}M+ Sold"
    elif total_sold >= 1000:
        sold_display = f"{total_sold//1000}K+ Sold"
    else:
        sold_display = f"{total_sold} Sold"

    # PostgreSQL: use RANDOM() instead of RAND()
    cursor.execute("""
        SELECT product_id, name, main_image, color_price, color_original_price,
            (SELECT SUM(quantity) FROM order_items
             WHERE order_items.product_id = products.product_id AND status='Delivered') AS sold
        FROM products WHERE category_id = %s AND product_id != %s
        ORDER BY RANDOM() LIMIT 9
    """, (product["category_id"], product_id))
    similar_products = cursor.fetchall()

    for sp in similar_products:
        try:
            # price_map = json.loads(sp["color_price"]) if sp["color_price"] else {}
            price_map = safe_json_load(sp.get("color_price"))
            sp["price"] = list(price_map.values())[0] if price_map else 0
        except:
            sp["price"] = 0
        sold = sp["sold"] or 0
        if sold >= 1_000_000:
            sp["sold_display"] = f"{sold//1_000_000}M+ sold"
        elif sold >= 1000:
            sp["sold_display"] = f"{sold//1000}K+ sold"
        else:
            sp["sold_display"] = f"{sold} sold"

    db_pool.putconn(conn)

    return render_template("view_product.html", product=product, seller=seller, gallery=gallery,
                           specs=specs, colors=color_list, cart_count=cart_count,
                           color_price_map=color_price_map, color_original_price_map=color_original_price_map,
                           color_image_map=color_image_map, color_stock_map=color_stock_map,
                           shipping_start=shipping_start_str, shipping_end=shipping_end_str,
                           category_tree=category_tree, avg_rating=avg_rating, total_ratings=total_ratings,
                           total_sold=sold_display, reviews=reviews, total_pages=total_pages,
                           per_page=per_page, rating_counts=rating_counts, comments_count=comments_count,
                           media_count=media_count, similar_products=similar_products)


@app.route("/get_reviews")
def get_reviews():
    product_id = request.args.get("product_id")
    rating = request.args.get("rating", "all")
    comments = request.args.get("comments")
    media = request.args.get("media")

    query = """
        SELECT r.*, u.username, u.photo AS user_photo FROM product_reviews r
        LEFT JOIN users u ON r.user_id = u.user_id WHERE r.product_id = %s
    """
    params = [product_id]
    if rating != "all":
        query += " AND r.rating = %s"; params.append(int(rating))
    if comments == "1":
        query += " AND r.review IS NOT NULL AND r.review != ''"
    if media == "1":
        query += " AND r.photos IS NOT NULL AND r.photos != '[]'::jsonb AND r.photos != '[]'"

    conn = get_db()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cursor.execute(query, params)
    results = cursor.fetchall()
    db_pool.putconn(conn)

    for r in results:
        if r["photos"]:
            try:
                r["photos"] = json.loads(r["photos"])
            except:
                r["photos"] = []
        else:
            r["photos"] = []

    return jsonify(results)


@app.route("/add_to_cart")
def add_to_cart():
    if "user_id" not in session:
        return redirect(url_for("login"))

    user_id = session["user_id"]
    conn = get_db()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cart_count = get_cart_count(cursor, user_id)

    cursor.execute("""
        SELECT cart.*, products.name, products.color_images, products.main_image,
               products.seller_id, sellers.business_name AS seller_name
        FROM cart
        JOIN products ON cart.product_id = products.product_id
        JOIN sellers ON products.seller_id = sellers.seller_id
        WHERE cart.user_id = %s
    """, (user_id,))
    items = cursor.fetchall()

    for item in items:
        try:
            item["color_stock_map"] = safe_json_load(item.get("color_stock"))
        except:
            item["color_stock_map"] = {}
        try:
            item["color_price_map"] = safe_json_load(item.get("color_price"))
        except:
            item["color_price_map"] = {}
        try:
            color_map = safe_json_load(item.get("color_images"))
            item["display_image"] = color_map.get(item.get("variation"), item["main_image"])
        except:
            item["display_image"] = item["main_image"]

    category_tree = build_category_tree(cursor)
    db_pool.putconn(conn)

    items_by_seller = defaultdict(list)
    for item in items:
        items_by_seller[item['seller_id']].append(item)

    items_by_seller_list = [
        {"seller_id": sid, "seller_name": seller_items[0]['seller_name'], "cart_items": seller_items}
        for sid, seller_items in items_by_seller.items()
    ]

    return render_template("add_to_cart.html", items_by_seller=items_by_seller_list,
                           cart_count=cart_count, category_tree=category_tree)


@app.route("/update_cart_quantity", methods=["POST"])
def update_cart_quantity():
    data = request.get_json()
    cart_id = data.get("cart_id")
    requested_qty = int(data.get("quantity", 1))
    user_id = session["user_id"]

    conn = get_db()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cursor.execute("""
        SELECT cart.*, products.color_stock, products.color_price
        FROM cart JOIN products ON cart.product_id = products.product_id
        WHERE cart.cart_id = %s AND cart.user_id = %s
    """, (cart_id, user_id))
    cart_item = cursor.fetchone()

    if not cart_item:
        db_pool.putconn(conn)
        return jsonify({"success": False, "error": "Cart item not found"}), 404

    # ✅ FIXED
    color_stock_map = safe_json_load(cart_item.get("color_stock"))
    color_price_map = safe_json_load(cart_item.get("color_price"))

    max_stock = color_stock_map.get(cart_item["variation"], 999)
    unit_price = float(color_price_map.get(cart_item["variation"], cart_item.get("price") or 0))
    new_qty = min(requested_qty, max_stock)
    total_price = round(unit_price * new_qty, 2)

    cursor.execute("UPDATE cart SET quantity=%s, total_price=%s WHERE cart_id=%s",
                   (new_qty, total_price, cart_id))
    conn.commit()
    db_pool.putconn(conn)

    return jsonify({
        "success": True,
        "new_qty": new_qty,
        "unit_price": unit_price,
        "new_total_price": total_price,
        "max_stock": max_stock
    })


@app.route("/checkout", methods=["POST"])
def checkout():
    if "loggedin" not in session:
        return jsonify({"redirect": "/login"})

    data = request.get_json()
    items = data.get("items", [])
    if not items:
        return jsonify({"error": "No items selected"}), 400

    user_id = session["user_id"]
    conn = get_db()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cursor.execute("""
        SELECT first_name, middle_name, last_name, suffix, mobile,
               region, province, city, barangay, postal, street
        FROM users WHERE user_id = %s
    """, (user_id,))
    user = cursor.fetchone()
    db_pool.putconn(conn)

    fields = ["first_name", "last_name", "mobile", "region", "province", "city", "barangay", "postal", "street"]
    if any(not user[f] for f in fields):
        return jsonify({"incomplete": True, "message": "Complete your information before checking out"})

    session["checkout_items"] = items
    return jsonify({"redirect": "/payment"})


@app.route("/payment")
def payment():
    items = session.get("checkout_items", [])
    if not items:
        return redirect("/add_to_cart")

    conn = get_db()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    grouped_products = {}
    subtotal = 0
    unique_sellers = set()

    for cart_id in items:
        cursor.execute("""
            SELECT c.cart_id, c.quantity, c.total_price, c.variation,
                   p.name, p.main_image, p.color_images, p.seller_id, s.business_name
            FROM cart c JOIN products p ON c.product_id = p.product_id
            JOIN sellers s ON p.seller_id = s.seller_id WHERE c.cart_id = %s
        """, (cart_id,))
        row = cursor.fetchone()
        if row:
            unique_sellers.add(row["seller_id"])
            color_images = row["color_images"] if row["color_images"] else {}
            row["variation_image"] = color_images.get(row["variation"], row["main_image"])
            subtotal += float(row["total_price"])
            seller_id = row["seller_id"]
            if seller_id not in grouped_products:
                grouped_products[seller_id] = {"business_name": row["business_name"], "products": []}
            grouped_products[seller_id]["products"].append(row)

    shipping_fee = 50 * len(unique_sellers)
    total = subtotal + shipping_fee

    user_id = session["user_id"]
    cursor.execute("""
        SELECT first_name, middle_name, last_name, suffix, mobile,
               region, province, city, barangay, postal, street
        FROM users WHERE user_id = %s
    """, (user_id,))
    user = cursor.fetchone()
    db_pool.putconn(conn)

    full_name = f"{user['first_name']} {user['middle_name'] or ''} {user['last_name']} {user['suffix'] or ''}".strip()
    full_address = f"{user['street']}, {user['barangay']}, {user['city']}, {user['province']}, {user['region']}, {user['postal']}"

    return render_template("payment.html", sellers=grouped_products,
                           subtotal=f"{subtotal:.2f}", shipping_fee=f"{shipping_fee:.2f}",
                           total=f"{total:.2f}", full_name=full_name,
                           mobile=user['mobile'], full_address=full_address, shops=len(unique_sellers))


@app.route("/finalize_payment", methods=["POST"])
def finalize_payment():
    if "loggedin" not in session:
        return jsonify({"redirect": "/login"}), 401

    items = session.get("checkout_items", [])
    if not items:
        return jsonify({"error": "no checkout items"}), 400

    user_id = session["user_id"]
    conn = get_db()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    try:
        seller_groups = {}
        for cart_id in items:
            cursor.execute("""
                SELECT c.cart_id, c.product_id, c.quantity, c.total_price, c.variation,
                       p.seller_id, p.name, p.main_image, p.color_images, p.color_stock
                FROM cart c JOIN products p ON c.product_id = p.product_id
                WHERE c.cart_id = %s
            """, (cart_id,))
            row = cursor.fetchone()
            if not row:
                continue
            color_images = row["color_images"] if row["color_images"] else {}
            row["photo"] = color_images.get(row["variation"], row["main_image"])
            seller_groups.setdefault(row["seller_id"], []).append(row)

        if not seller_groups:
            return jsonify({"error": "no valid items"}), 400

        created_order_ids = []
        for seller_id, cart_rows in seller_groups.items():
            seller_subtotal = sum(float(r["total_price"]) for r in cart_rows)
            seller_total = seller_subtotal + 50.0

            # RETURNING order_id (PostgreSQL syntax)
            cursor.execute("""
                INSERT INTO orders (user_id, subtotal, shipping_fee, total, payment_method, payment_status)
                VALUES (%s, %s, %s, %s, %s, %s) RETURNING order_id
            """, (user_id, seller_subtotal, 50.0, seller_total, "COD", "Pending"))
            conn.commit()
            order_id = cursor.fetchone()["order_id"]
            created_order_ids.append(order_id)

            cursor.execute("""
                SELECT first_name, middle_name, last_name, suffix, mobile,
                       region, province, city, barangay, postal, street
                FROM users WHERE user_id = %s
            """, (user_id,))
            user = cursor.fetchone()
            full_name = f"{user['first_name']} {user.get('middle_name') or ''} {user['last_name']} {user.get('suffix') or ''}".strip()

            cursor.execute("""
                INSERT INTO order_shipping (order_id, full_name, mobile, region, province, city, barangay, postal, street)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """, (order_id, full_name, user["mobile"], user["region"], user["province"],
                  user["city"], user["barangay"], user["postal"], user["street"]))

            for r in cart_rows:
                qty = int(r["quantity"])
                total_price = float(r["total_price"])
                unit_price = total_price / qty if qty else total_price

                cursor.execute("""
                    INSERT INTO order_items (order_id, product_id, seller_id, user_id, variation, quantity, price, total_price, photo)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """, (order_id, r["product_id"], seller_id, user_id, r["variation"], qty, unit_price, total_price, r["photo"]))

                cursor.execute("SELECT color_stock FROM products WHERE product_id = %s", (r["product_id"],))
                stock_row = cursor.fetchone()
                if stock_row and stock_row["color_stock"]:
                    color_stock = stock_row["color_stock"] if stock_row["color_stock"] else {}
                    if r["variation"] in color_stock:
                        color_stock[r["variation"]] = max(0, color_stock[r["variation"]] - qty)
                        cursor.execute("UPDATE products SET color_stock=%s WHERE product_id=%s",
                                       (json.dumps(color_stock), r["product_id"]))

            conn.commit()

        if items:
            placeholders = ",".join(["%s"] * len(items))
            cursor.execute(f"DELETE FROM cart WHERE user_id=%s AND cart_id IN ({placeholders})",
                           (user_id, *items))
            conn.commit()

        session.pop("checkout_items", None)
        return jsonify({"success": True, "order_ids": created_order_ids, "redirect": "/homepage"})

    except Exception as e:
        conn.rollback()
        print("finalize_payment error:", e)
        return jsonify({"error": "could not finalize order"}), 500
    finally:
        cursor.close()
        db_pool.putconn(conn)


@app.route("/save_address", methods=["POST"])
def save_address():
    if "loggedin" not in session:
        return jsonify({"error": "Not logged in"}), 403

    data = request.get_json()
    user_id = session["user_id"]
    first_name = normalize_name(data.get("first_name"))
    middle_name = normalize_name(data.get("middle_name"))
    last_name = normalize_name(data.get("last_name"))
    suffix = normalize_name(data.get("suffix"))
    street = normalize_name(clean_street_name(data.get("street")))
    region = normalize_text(data.get("region"))
    province = normalize_text(data.get("province"))
    city = normalize_text(data.get("city"))
    barangay = normalize_text(data.get("barangay"))
    postal = data.get("postal", "").strip()
    mobile = data.get("mobile", "").strip()

    conn = get_db()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cursor.execute("""
        UPDATE users SET first_name=%s, middle_name=%s, last_name=%s, suffix=%s,
            mobile=%s, region=%s, province=%s, city=%s, barangay=%s, postal=%s, street=%s
        WHERE user_id=%s
    """, (first_name, middle_name, last_name, suffix, mobile, region, province, city, barangay, postal, street, user_id))
    conn.commit()
    db_pool.putconn(conn)
    return jsonify({"success": True})


@app.route("/delete_cart/<int:cart_id>")
def delete_cart(cart_id):
    conn = get_db()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cursor.execute("DELETE FROM cart WHERE cart_id = %s", (cart_id,))
    conn.commit()
    db_pool.putconn(conn)
    return redirect(url_for("add_to_cart"))


@app.route("/add_to_cart_backend", methods=["POST"])
def add_to_cart_backend():
    user_id = session["user_id"]
    product_id = request.form["product_id"]
    variation = request.form.get("variation", None)
    quantity_to_add = int(request.form.get("quantity", 1))

    conn = get_db()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cursor.execute("SELECT * FROM products WHERE product_id = %s", (product_id,))
    product = cursor.fetchone()
    if not product:
        db_pool.putconn(conn)
        return jsonify({"success": False, "error": "Product not found"}), 404

    color_price_map = safe_json_load(product.get("color_price"))
    color_stock_map = safe_json_load(product.get("color_stock"))
    max_stock = color_stock_map.get(variation, 0)
    price = float(color_price_map.get(variation, 0))

    cursor.execute("SELECT quantity, total_price FROM cart WHERE user_id=%s AND product_id=%s AND variation=%s",
                   (user_id, product_id, variation))
    existing = cursor.fetchone()

    if existing:
        new_quantity = existing["quantity"] + quantity_to_add
        if new_quantity > max_stock:
            db_pool.putconn(conn)
            return jsonify({"success": False, "error": f"Purchase limit exceeded."})
        cursor.execute("UPDATE cart SET quantity=%s, total_price=%s WHERE user_id=%s AND product_id=%s AND variation=%s",
                       (new_quantity, new_quantity * price, user_id, product_id, variation))
    else:
        quantity_to_add = min(quantity_to_add, max_stock)
        if quantity_to_add <= 0:
            db_pool.putconn(conn)
            return jsonify({"success": False, "error": "This product is out of stock."})
        cursor.execute("INSERT INTO cart (user_id, product_id, variation, quantity, price, total_price) VALUES (%s,%s,%s,%s,%s,%s)",
                       (user_id, product_id, variation, quantity_to_add, price, quantity_to_add * price))

    conn.commit()
    cart_count = get_cart_count(cursor, user_id)
    db_pool.putconn(conn)
    return jsonify({"success": True, "cart_count": cart_count})


@app.route('/shop/<int:seller_id>')
def shop(seller_id):
    conn = get_db()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cursor.execute("SELECT seller_id, business_name, store_logo FROM sellers WHERE seller_id=%s", (seller_id,))
    seller = cursor.fetchone()
    if not seller:
        return "Seller not found", 404

    cursor.execute("SELECT * FROM products WHERE seller_id=%s AND status='approved' ORDER BY created_at DESC", (seller_id,))
    seller_products = cursor.fetchall()

    for p in seller_products:
        if p["color_price"]:
            try:
                color_price = p["color_price"]
                prices = [float(v) for v in color_price.values() if v]
                p["min_price"] = min(prices) if prices else 0
                p["max_price"] = max(prices) if prices else 0
            except:
                p["min_price"] = p["max_price"] = 0
        else:
            p["min_price"] = p["max_price"] = 0

    product_ids = [p['product_id'] for p in seller_products]
    if product_ids:
        format_strings = ','.join(['%s'] * len(product_ids))
        cursor.execute(f"SELECT product_id, SUM(quantity) as total_sold FROM order_items WHERE status='Delivered' AND product_id IN ({format_strings}) GROUP BY product_id", tuple(product_ids))
        sold_counts = {row['product_id']: row['total_sold'] for row in cursor.fetchall()}
    else:
        sold_counts = {}
    for p in seller_products:
        p['total_sold'] = sold_counts.get(p['product_id'], 0)

    cart_count = get_cart_count(cursor, session.get("user_id"))
    category_tree = build_category_tree(cursor)
    db_pool.putconn(conn)

    return render_template("shop.html", seller_products=seller_products,
                           seller_name=seller["business_name"], seller_logo=seller["store_logo"],
                           cart_count=cart_count, category_tree=category_tree,
                           datetime=datetime, timedelta=timedelta)


@app.route("/favorites")
def favorites():
    return render_template("favorites.html")


@app.route("/my_account")
def my_account():
    conn = get_db()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cart_count = 0
    user_data = {}
    if "user_id" in session:
        cart_count = get_cart_count(cursor, session["user_id"])
        cursor.execute("""
            SELECT username, first_name, middle_name, last_name, suffix,
                   email, mobile, gender, birthdate, photo
            FROM users WHERE user_id = %s
        """, (session["user_id"],))
        user_data = cursor.fetchone()
        if user_data:
            session['photo'] = user_data['photo'] or 'default_photo_1.png'
            session['email'] = user_data['email']

    category_tree = build_category_tree(cursor)
    db_pool.putconn(conn)
    return render_template("my_account.html", category_tree=category_tree,
                           cart_count=cart_count, user_data=user_data)


@app.route("/update_profile", methods=["POST"])
def update_profile():
    if "user_id" not in session:
        return redirect(url_for("login"))

    conn = get_db()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    user_id = session["user_id"]
    first_name = request.form.get("first_name")
    middle_name = request.form.get("middle_name")
    last_name = request.form.get("last_name")
    suffix = request.form.get("suffix")
    gender = request.form.get("gender")
    birthdate = request.form.get("birthdate")

    photo_file = request.files.get("photo")
    if photo_file and photo_file.filename != "":
        filename = secure_filename(photo_file.filename)
        upload_path = os.path.join(app.root_path, "static/uploads/sellers", filename)
        os.makedirs(os.path.dirname(upload_path), exist_ok=True)
        photo_file.save(upload_path)
        cursor.execute("""
            UPDATE users SET first_name=%s, middle_name=%s, last_name=%s, suffix=%s,
                gender=%s, birthdate=%s, photo=%s WHERE user_id=%s
        """, (first_name, middle_name, last_name, suffix, gender, birthdate, filename, user_id))
        session['photo'] = filename
    else:
        cursor.execute("""
            UPDATE users SET first_name=%s, middle_name=%s, last_name=%s, suffix=%s,
                gender=%s, birthdate=%s WHERE user_id=%s
        """, (first_name, middle_name, last_name, suffix, gender, birthdate, user_id))

    conn.commit()
    db_pool.putconn(conn)
    flash("Profile updated successfully!", "success")
    return redirect(url_for("my_account"))


@app.route("/bank")
def bank():
    conn = get_db()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cart_count = get_cart_count(cursor, session.get("user_id"))
    category_tree = build_category_tree(cursor)
    cards = []
    if "user_id" in session:
        cursor.execute("SELECT * FROM user_banks WHERE user_id=%s", (session["user_id"],))
        cards = cursor.fetchall()
    db_pool.putconn(conn)
    return render_template("banks.html", cards=cards, category_tree=category_tree, cart_count=cart_count)


@app.route("/add_card", methods=["POST"])
def add_card():
    if "user_id" not in session:
        return redirect("/login")
    try:
        name = request.form.get("cardholder")
        number = request.form.get("card_number")
        expiry = request.form.get("expiry")
        cvv = request.form.get("cvv")
        bank_name = request.form.get("bank_name")
        conn = get_db()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute("""
            INSERT INTO user_banks (user_id, cardholder_name, card_number, expiry, cvv, bank_name)
            VALUES (%s, %s, %s, %s, %s, %s)
        """, (session["user_id"], name, number, expiry, cvv, bank_name))
        conn.commit()
        db_pool.putconn(conn)
    except Exception as e:
        print("ERROR inserting card:", e)
    return redirect(url_for("bank"))


@app.route("/remove_card/<int:bank_id>", methods=["POST"])
def remove_card(bank_id):
    conn = get_db()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cursor.execute("DELETE FROM user_banks WHERE id=%s AND user_id=%s", (bank_id, session["user_id"]))
    conn.commit()
    db_pool.putconn(conn)
    return redirect(url_for("bank"))


@app.route("/addresses")
def addresses():
    conn = get_db()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    user_address = None
    if "user_id" in session:
        cursor.execute("""
            SELECT first_name, last_name, mobile, region, province, city,
                   barangay, postal, street FROM users WHERE user_id=%s
        """, (session["user_id"],))
        user_address = cursor.fetchone()
    cart_count = get_cart_count(cursor, session.get("user_id"))
    category_tree = build_category_tree(cursor)
    db_pool.putconn(conn)
    return render_template("addresses.html", category_tree=category_tree,
                           cart_count=cart_count, user_address=user_address)


@app.post("/update_address")
def update_address():
    if "user_id" not in session:
        return redirect("/login")
    conn = get_db()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cursor.execute("""
        UPDATE users SET mobile=%s, region=%s, province=%s, city=%s,
            barangay=%s, postal=%s, street=%s WHERE user_id=%s
    """, (request.form["mobile"], request.form["region"], request.form["province"],
          request.form["city"], request.form["barangay"], request.form["postal"],
          request.form["street"], session["user_id"]))
    conn.commit()
    db_pool.putconn(conn)
    return redirect("/addresses")


@app.post("/update_password")
def update_password():
    if "user_id" not in session:
        return redirect("/login")

    current_password = request.form.get("current_password")
    new_password = request.form.get("new_password")
    confirm_password = request.form.get("confirm_password")

    conn = get_db()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cursor.execute("SELECT first_name, last_name, mobile, region, province, city, barangay, postal, street FROM users WHERE user_id=%s", (session["user_id"],))
    user_address = cursor.fetchone()
    cart_count = get_cart_count(cursor, session.get("user_id"))
    category_tree = build_category_tree(cursor)

    if not current_password or not new_password or not confirm_password:
        db_pool.putconn(conn)
        return render_template("addresses.html", error="Please fill out all fields",
                               category_tree=category_tree, cart_count=cart_count, user_address=user_address)

    if new_password != confirm_password:
        db_pool.putconn(conn)
        return render_template("addresses.html", error="New password and confirm password do not match",
                               category_tree=category_tree, cart_count=cart_count, user_address=user_address)

    cursor.execute("SELECT password FROM users WHERE user_id=%s", (session["user_id"],))
    user = cursor.fetchone()

    if not user or not check_password_hash(user["password"], current_password):
        db_pool.putconn(conn)
        return render_template("addresses.html", error="Current password is incorrect",
                               category_tree=category_tree, cart_count=cart_count, user_address=user_address)

    cursor.execute("UPDATE users SET password=%s WHERE user_id=%s",
                   (generate_password_hash(new_password), session["user_id"]))
    conn.commit()
    db_pool.putconn(conn)
    return redirect("/addresses")


@app.route("/privacy_settings")
def privacy():
    conn = get_db()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cart_count = get_cart_count(cursor, session.get("user_id"))
    category_tree = build_category_tree(cursor)
    db_pool.putconn(conn)
    return render_template("privacy.html", category_tree=category_tree, cart_count=cart_count)


@app.route("/request_account_deletion", methods=["POST"])
def request_account_deletion():
    if "user_id" not in session:
        return {"success": False, "message": "User not logged in"}, 403
    data = request.get_json()
    reason = data.get("reason", "")
    conn = get_db()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cursor.execute("INSERT INTO account_deletion_requests (user_id, reason, created_at) VALUES (%s, %s, NOW())",
                   (session["user_id"], reason))
    conn.commit()
    db_pool.putconn(conn)
    return {"success": True}


@app.route("/notification_settings")
def notification():
    conn = get_db()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cart_count = get_cart_count(cursor, session.get("user_id"))
    category_tree = build_category_tree(cursor)
    db_pool.putconn(conn)
    return render_template("notification.html", category_tree=category_tree, cart_count=cart_count)


@app.route("/purchase")
def purchase():
    user_id = session.get("user_id")
    search_query = request.args.get("q", "").strip()
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    query = """
        SELECT oi.item_id, oi.order_id, oi.product_id, oi.seller_id,
               oi.variation, oi.quantity, oi.price, oi.total_price, oi.status,
               oi.created_at, oi.photo, oi.order_received, oi.cancel_reason,
               p.name AS product_name, s.business_name AS seller_name, s.store_logo AS seller_logo
        FROM order_items oi
        JOIN products p ON oi.product_id = p.product_id
        JOIN sellers s ON oi.seller_id = s.seller_id
        WHERE oi.user_id = %s
    """
    params = [user_id]
    if search_query:
        query += " AND (p.name LIKE %s OR s.business_name LIKE %s OR CAST(oi.order_id AS TEXT) LIKE %s)"
        search_like = f"%{search_query}%"
        params.extend([search_like, search_like, search_like])
    query += " ORDER BY oi.created_at DESC"
    cur.execute(query, params)
    items = cur.fetchall()

    for item in items:
        cur.execute("""
            SELECT review_id FROM product_reviews
            WHERE user_id=%s AND product_id=(SELECT product_id FROM order_items WHERE item_id=%s)
            AND order_item_id=%s
        """, (user_id, item['item_id'], item['item_id']))
        item['already_rated'] = True if cur.fetchone() else False

    categorized = {"all": [], "to_pay": [], "to_ship": [], "to_receive": [],
                   "completed": [], "cancelled": [], "refund": []}

    for item in items:
        categorized["all"].append(item)
        if item["status"] == "Pending":
            categorized["to_pay"].append(item)
        elif item["status"] in ["Preparing", "Accepted"]:
            categorized["to_ship"].append(item)
        elif item["status"] in ["Shipped", "Delivery"]:
            categorized["to_receive"].append(item)
        elif item["status"] == "Delivered" and not item.get("order_received"):
            categorized["to_receive"].append(item)
        elif item["status"] == "Delivered" and item["order_received"]:
            categorized["completed"].append(item)
        elif item["status"] == "Cancelled":
            categorized["cancelled"].append(item)
        elif item["status"] in ["Return Requested", "Refund Requested", "Refund", "Returned", "Refunded"]:
            categorized["refund"].append(item)

    status_order = ["Pending","Preparing","Accepted","Shipped","Delivery","Delivered",
                    "Cancelled","Return Requested","Refund Requested","Refund","Returned","Refunded"]
    categorized["all"].sort(key=lambda x: status_order.index(x["status"]) if x["status"] in status_order else 99)
    counts = {key: len(val) for key, val in categorized.items() if len(val) > 0}

    cart_count = get_cart_count(cur, session.get("user_id"))
    category_tree = build_category_tree(cur)
    db_pool.putconn(conn)

    return render_template("purchase.html", orders=categorized, counts=counts,
                           category_tree=category_tree, cart_count=cart_count, search_query=search_query)


@app.route("/order_received/<int:item_id>", methods=["POST"])
def order_received(item_id):
    user_id = session.get("user_id")
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute("""
        UPDATE order_items SET order_received=1, status='Delivered', received_at=NOW()
        WHERE item_id=%s AND user_id=%s
    """, (item_id, user_id))

    cur.execute("SELECT seller_id, total_price, order_id FROM order_items WHERE item_id=%s", (item_id,))
    item = cur.fetchone()

    if item:
        seller_id = item["seller_id"]
        order_id = item["order_id"]
        seller_amount = round(float(item["total_price"]) * 0.9, 2)
        cur.execute("""
            INSERT INTO sellers_earnings (seller_id, order_id, item_id, amount, payout_status)
            VALUES (%s, %s, %s, %s, 'pending')
        """, (seller_id, order_id, item_id, seller_amount))

        cur.execute("SELECT COUNT(*) AS remaining FROM order_items WHERE order_id=%s AND order_received=0", (order_id,))
        remaining = cur.fetchone()["remaining"]
        if remaining == 0:
            cur.execute("UPDATE orders SET order_status='Delivered' WHERE order_id=%s", (order_id,))

    conn.commit()
    db_pool.putconn(conn)
    return redirect(url_for("purchase"))


@app.route("/auto_confirm_orders")
def auto_confirm_orders():
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # PostgreSQL: use INTERVAL '3 days' syntax
    cur.execute("""
        UPDATE order_items
        SET order_received=1, status='Completed', received_at=NOW()
        WHERE status='Delivered' AND order_received=0
          AND created_at <= NOW() - INTERVAL '3 days'
    """)
    conn.commit()

    # PostgreSQL: no JOIN in UPDATE, use subquery
    cur.execute("""
        UPDATE orders SET payment_status='Paid to Seller'
        WHERE order_id IN (
            SELECT order_id FROM order_items
            GROUP BY order_id
            HAVING SUM(CASE WHEN order_received=0 THEN 1 ELSE 0 END) = 0
        )
    """)
    conn.commit()
    db_pool.putconn(conn)
    return "Auto confirm complete"


@app.route("/rate_product", methods=["POST"])
def rate_product():
    user_id = session.get("user_id")
    item_id = request.form.get("item_id")
    product_rating = request.form.get("product_rating")
    product_review = request.form.get("product_review")
    rider_rating = request.form.get("rider_rating")
    rider_review = request.form.get("rider_review")

    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    photos_list = []
    for file in request.files.getlist('product_photos'):
        if file and allowed_file(file.filename):
            filename = secure_filename(file.filename)
            file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
            photos_list.append(filename)

    photos_json = json.dumps(photos_list) if photos_list else None

    cur.execute("""
        INSERT INTO product_reviews (user_id, product_id, order_item_id, rating, review, photos, created_at)
        SELECT %s, product_id, item_id, %s, %s, %s, NOW()
        FROM order_items WHERE item_id=%s AND user_id=%s
    """, (user_id, product_rating, product_review, photos_json, item_id, user_id))

    cur.execute("""
        INSERT INTO rider_reviews (user_id, rider_id, rating, review, created_at)
        SELECT %s, o.rider_id, %s, %s, NOW()
        FROM order_items oi JOIN orders o ON oi.order_id=o.order_id
        WHERE oi.item_id=%s AND oi.user_id=%s
    """, (user_id, rider_rating, rider_review, item_id, user_id))

    conn.commit()
    db_pool.putconn(conn)
    return redirect(url_for("purchase"))


@app.route("/request_refund/<int:item_id>", methods=["GET", "POST"])
def request_refund(item_id):
    user_id = session.get("user_id")
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    if request.method == "POST":
        reason = request.form.get("reason")
        cur.execute("""
            INSERT INTO refund_requests (user_id, item_id, reason, status, created_at)
            VALUES (%s, %s, %s, 'Pending', NOW())
        """, (user_id, item_id, reason))
        conn.commit()
        db_pool.putconn(conn)
        return redirect(url_for("purchase"))

    cur.execute("SELECT * FROM order_items WHERE item_id=%s AND user_id=%s", (item_id, user_id))
    item = cur.fetchone()
    db_pool.putconn(conn)
    return render_template("request_refund.html", item=item)


@app.route('/cancel_order/<int:item_id>', methods=['POST'])
def cancel_order(item_id):
    conn = get_db()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    try:
        cancel_reason = request.form.get("cancel_reason")
        other_reason = request.form.get("other_reason")
        if cancel_reason == "Others":
            cancel_reason = other_reason
        if not cancel_reason:
            flash("Cancellation reason is required.", "danger")
            return redirect(request.referrer or url_for('purchase'))

        cursor.execute("SELECT product_id, variation, quantity, total_price, order_id FROM order_items WHERE item_id=%s", (item_id,))
        item = cursor.fetchone()
        if not item:
            flash("Order item not found.", "danger")
            return redirect(request.referrer or url_for('purchase'))

        product_id = item['product_id']
        variation = item['variation']
        quantity = item['quantity']
        order_id = item['order_id']

        cursor.execute("UPDATE order_items SET status='Cancelled', cancel_reason=%s WHERE item_id=%s", (cancel_reason, item_id))

        cursor.execute("SELECT color_stock, stock FROM products WHERE product_id=%s", (product_id,))
        product = cursor.fetchone()
        color_stock = json.loads(product['color_stock'])
        color_stock[variation] = color_stock.get(variation, 0) + quantity
        cursor.execute("UPDATE products SET color_stock=%s, stock=%s WHERE product_id=%s",
                       (json.dumps(color_stock), sum(color_stock.values()), product_id))

        cursor.execute("SELECT COALESCE(SUM(total_price),0) AS new_subtotal FROM order_items WHERE order_id=%s AND status!='Cancelled'", (order_id,))
        new_subtotal = cursor.fetchone()['new_subtotal']
        cursor.execute("SELECT shipping_fee FROM orders WHERE order_id=%s", (order_id,))
        shipping_fee = cursor.fetchone()['shipping_fee']
        cursor.execute("UPDATE orders SET subtotal=%s, total=%s WHERE order_id=%s",
                       (new_subtotal, new_subtotal + shipping_fee, order_id))

        cursor.execute("SELECT COUNT(*) AS active_items FROM order_items WHERE order_id=%s AND status!='Cancelled'", (order_id,))
        if cursor.fetchone()['active_items'] == 0:
            cursor.execute("UPDATE orders SET order_status='Cancelled' WHERE order_id=%s", (order_id,))

        conn.commit()
        flash("Order cancelled successfully.", "success")

    except Exception as e:
        conn.rollback()
        flash(f"Error: {str(e)}", "danger")
    finally:
        cursor.close()
        db_pool.putconn(conn)

    return redirect(request.referrer or url_for('purchase'))


@app.route("/order_update")
def order_update():
    conn = get_db()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cart_count = get_cart_count(cursor, session.get("user_id"))
    category_tree = build_category_tree(cursor)
    orders = []

    if "user_id" in session:
        user_id = session["user_id"]
        cursor.execute("SELECT * FROM orders WHERE user_id=%s ORDER BY created_at DESC", (user_id,))
        orders_data = cursor.fetchall()
        for order in orders_data:
            order_id = order["order_id"]
            cursor.execute("SELECT oi.*, p.name FROM order_items oi JOIN products p ON oi.product_id=p.product_id WHERE oi.order_id=%s", (order_id,))
            items = cursor.fetchall()
            cursor.execute("SELECT * FROM order_shipping WHERE order_id=%s", (order_id,))
            shipping = cursor.fetchone()
            orders.append({"order": order, "items": items, "shipping": shipping})

    db_pool.putconn(conn)
    return render_template("order_update.html", category_tree=category_tree, cart_count=cart_count, orders=orders)


@app.route("/admin_dashboard")
def admin_dashboard():
    conn = get_db()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cursor.execute("SELECT COUNT(*) AS total FROM sellers")
    total_sellers = cursor.fetchone()['total']
    cursor.execute("SELECT COUNT(*) AS total FROM users WHERE role='buyer'")
    total_buyers = cursor.fetchone()['total']
    cursor.execute("SELECT COUNT(*) AS total FROM riders")
    total_drivers = cursor.fetchone()['total']

    cursor.execute("""
        SELECT SUM(oi.total_price * 0.10) AS admin_earnings
        FROM order_items oi LEFT JOIN sellers_earnings se ON se.item_id=oi.item_id
        WHERE oi.order_received=1 AND oi.status='Delivered' AND se.payout_status='paid'
    """)
    result = cursor.fetchone()
    admin_earnings = round(float(result["admin_earnings"]), 2) if result["admin_earnings"] else 0.00

    cursor.execute("""
        SELECT users.first_name, users.last_name, users.role,
               order_items.created_at, order_items.item_id, products.name
        FROM order_items
        JOIN users ON order_items.user_id=users.user_id
        JOIN products ON order_items.product_id=products.product_id
        ORDER BY order_items.created_at DESC LIMIT 5
    """)
    recent_orders = cursor.fetchall()

    # PostgreSQL: use EXTRACT instead of MONTH()
    cursor.execute("""
        SELECT EXTRACT(MONTH FROM oi.created_at) AS month, SUM(oi.total_price * 0.10) AS earnings
        FROM order_items oi LEFT JOIN sellers_earnings se ON se.item_id=oi.item_id
        WHERE oi.order_received=1 AND oi.status='Delivered' AND se.payout_status='paid'
        GROUP BY EXTRACT(MONTH FROM oi.created_at)
    """)
    monthly_data = cursor.fetchall()
    earnings_by_month = [0] * 12
    for row in monthly_data:
        month_index = int(row['month']) - 1
        earnings_by_month[month_index] = float(row['earnings'])

    # PostgreSQL: GROUP BY must include all selected non-aggregate columns
    cursor.execute("""
        SELECT products.name, COUNT(*) AS order_count
        FROM order_items JOIN products ON order_items.product_id=products.product_id
        WHERE order_items.status='Delivered'
        GROUP BY products.product_id, products.name
        ORDER BY order_count DESC LIMIT 5
    """)
    top_products_data = cursor.fetchall()
    top_products = [p['name'] for p in top_products_data]
    top_products_count = [p['order_count'] for p in top_products_data]

    db_pool.putconn(conn)
    return render_template("admin_dashboard.html", total_sellers=total_sellers, total_buyers=total_buyers,
                           total_drivers=total_drivers, admin_earnings=admin_earnings,
                           recent_orders=recent_orders, earnings_by_month=earnings_by_month,
                           top_products=top_products, top_products_count=top_products_count)


@app.route("/admin_accounts")
def admin_accounts():
    conn = get_db()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cursor.execute("SELECT * FROM sellers ORDER BY seller_id DESC")
    sellers = cursor.fetchall()

    cursor.execute("""
        SELECT u.*, (SELECT COUNT(*) FROM orders o WHERE o.user_id=u.user_id) AS total_orders
        FROM users u WHERE is_verified=1
    """)
    buyers = cursor.fetchall()

    cursor.execute("""
        SELECT r.*, (SELECT COUNT(*) FROM orders o WHERE o.rider_id=r.rider_id) AS deliveries,
               (SELECT ROUND(AVG(rating)::numeric,1) FROM rider_reviews rr WHERE rr.rider_id=r.rider_id) AS rating
        FROM riders r
    """)
    drivers = cursor.fetchall()
    db_pool.putconn(conn)

    return render_template("admin_accounts.html", sellers=sellers, buyers=buyers, drivers=drivers)


@app.route("/update_verification", methods=["POST"])
def update_verification():
    user_type = request.form.get("type")
    user_id = request.form.get("id")
    new_status = request.form.get("status")

    conn = get_db()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    if user_type == "seller":
        cursor.execute("UPDATE sellers SET verified=%s WHERE seller_id=%s", (new_status, user_id))
    elif user_type == "rider":
        cursor.execute("UPDATE riders SET verified=%s WHERE rider_id=%s", (new_status, user_id))
    elif user_type == "buyer":
        cursor.execute("UPDATE users SET verified=%s WHERE user_id=%s", (new_status, user_id))

    conn.commit()
    db_pool.putconn(conn)
    return jsonify({"success": True})


@app.route("/delete_account", methods=["POST"])
def delete_account():
    acc_type = request.form["type"]
    acc_id = request.form["id"]

    conn = get_db()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    try:
        if acc_type == "seller":
            cursor.execute("UPDATE sellers_earnings SET seller_id=NULL WHERE seller_id=%s", (acc_id,))
            cursor.execute("DELETE FROM products WHERE seller_id=%s", (acc_id,))
            cursor.execute("DELETE FROM order_items WHERE seller_id=%s", (acc_id,))
            # PostgreSQL: no multi-table DELETE, use subquery
            cursor.execute("""
                DELETE FROM orders WHERE order_id IN (
                    SELECT DISTINCT o.order_id FROM orders o
                    JOIN order_items oi ON o.order_id=oi.order_id WHERE oi.seller_id=%s
                )
            """, (acc_id,))
            cursor.execute("DELETE FROM sellers WHERE seller_id=%s", (acc_id,))

        elif acc_type == "buyer":
            cursor.execute("DELETE FROM rider_reviews WHERE user_id=%s", (acc_id,))
            # PostgreSQL: no multi-table DELETE
            cursor.execute("""
                DELETE FROM order_shipping WHERE order_id IN (
                    SELECT order_id FROM orders WHERE user_id=%s
                )
            """, (acc_id,))
            cursor.execute("""
                DELETE FROM order_items WHERE order_id IN (
                    SELECT order_id FROM orders WHERE user_id=%s
                )
            """, (acc_id,))
            cursor.execute("DELETE FROM orders WHERE user_id=%s", (acc_id,))
            cursor.execute("DELETE FROM users WHERE user_id=%s", (acc_id,))

        elif acc_type == "rider":
            cursor.execute("DELETE FROM rider_reviews WHERE rider_id=%s", (acc_id,))
            cursor.execute("UPDATE orders SET rider_id=NULL WHERE rider_id=%s", (acc_id,))
            cursor.execute("DELETE FROM delivery_reports WHERE rider_id=%s", (acc_id,))
            cursor.execute("DELETE FROM riders WHERE rider_id=%s", (acc_id,))

        conn.commit()
        return jsonify({"message": "Account and related data deleted successfully!"})

    except Exception as e:
        conn.rollback()
        return jsonify({"message": f"Error: {str(e)}"})
    finally:
        db_pool.putconn(conn)


@app.route("/admin_categories")
def admin_categories():
    conn = get_db()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cursor.execute("SELECT category_id, main_category, category_name, sub_category, image, sub_image FROM categories ORDER BY category_name")
    categories = cursor.fetchall()
    db_pool.putconn(conn)
    return render_template("admin_categories.html", categories=categories)


@app.route("/admin_categories_search")
def admin_categories_search():
    search_query = request.args.get('search', '').strip()
    conn = get_db()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    sql = "SELECT category_id, main_category, category_name, sub_category, image, sub_image FROM categories"
    params = []
    if search_query:
        # PostgreSQL: CAST for integer comparisons with LIKE
        sql += " WHERE CAST(category_id AS TEXT) LIKE %s OR main_category LIKE %s OR category_name LIKE %s OR sub_category LIKE %s"
        like_query = f"%{search_query}%"
        params.extend([like_query, like_query, like_query, like_query])
    sql += " ORDER BY category_name"
    cursor.execute(sql, tuple(params))
    categories = cursor.fetchall()
    db_pool.putconn(conn)
    return jsonify(categories)


@app.route("/admin_update_category", methods=["POST"])
def admin_update_category():
    category_id = request.form['category_id']
    category_name = request.form['category_name']
    sub_category = request.form['sub_category']

    conn = get_db()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    upload_folder = os.path.join(app.root_path, 'static', 'images')
    os.makedirs(upload_folder, exist_ok=True)

    image_file = request.files.get('image')
    if image_file and image_file.filename != '':
        image_filename = secure_filename(image_file.filename)
        image_file.save(os.path.join(upload_folder, image_filename))
        cursor.execute("UPDATE categories SET image=%s WHERE category_name=%s", (image_filename, category_name))

    sub_image_file = request.files.get('sub_image')
    if sub_image_file and sub_image_file.filename != '':
        sub_image_filename = secure_filename(sub_image_file.filename)
        sub_image_file.save(os.path.join(upload_folder, sub_image_filename))
        cursor.execute("UPDATE categories SET sub_image=%s WHERE category_id=%s", (sub_image_filename, category_id))

    cursor.execute("UPDATE categories SET main_category=%s, category_name=%s, sub_category=%s WHERE category_id=%s",
                   ("Sports & Outdoors", category_name, sub_category, category_id))
    conn.commit()
    db_pool.putconn(conn)
    flash("Category updated successfully!", "success")
    return redirect(url_for("admin_categories"))


@app.route("/admin_add_category", methods=["POST"])
def admin_add_category():
    category_name = request.form.get("category_name")
    sub_category = request.form.get("sub_category")
    if not category_name:
        flash("Category Name is required", "danger")
        return redirect(url_for("admin_categories"))

    conn = get_db()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    upload_folder = os.path.join(app.root_path, "static", "images")
    os.makedirs(upload_folder, exist_ok=True)

    image_filename = None
    sub_image_filename = None

    image_file = request.files.get("image")
    if image_file and image_file.filename != "":
        image_filename = secure_filename(image_file.filename)
        image_file.save(os.path.join(upload_folder, image_filename))

    sub_image_file = request.files.get("sub_image")
    if sub_image_file and sub_image_file.filename != "":
        sub_image_filename = secure_filename(sub_image_file.filename)
        sub_image_file.save(os.path.join(upload_folder, sub_image_filename))

    cursor.execute("INSERT INTO categories (main_category, category_name, sub_category, image, sub_image) VALUES (%s,%s,%s,%s,%s)",
                   ("Sports & Outdoors", category_name, sub_category, image_filename, sub_image_filename))
    conn.commit()
    db_pool.putconn(conn)
    flash("New category added successfully!", "success")
    return redirect(url_for("admin_categories"))


@app.route("/admin_products_verification")
def admin_products_verification():
    conn = get_db()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cursor.execute("""
        SELECT p.product_id, p.seller_id, p.name, p.description, p.category_id, p.discount,
               p.colors, p.color_images, p.color_stock, p.stock, p.specifications, p.main_image,
               p.gallery_images, p.video, p.created_at, p.status, p.color_price,
               p.color_original_price, s.first_name, s.middle_name, s.last_name, s.suffix, s.business_name
        FROM products p LEFT JOIN sellers s ON p.seller_id=s.seller_id
        ORDER BY p.created_at DESC
    """)
    products = cursor.fetchall()
    db_pool.putconn(conn)

    for p in products:
        # Check if already a dict (JSONB column), otherwise parse JSON string
        if isinstance(p['color_images'], str):
            p['color_images'] = json.loads(p['color_images']) if p['color_images'] else {}
        elif p['color_images'] is None:
            p['color_images'] = {}
            
        if isinstance(p['color_price'], str):
            p['color_price'] = json.loads(p['color_price']) if p['color_price'] else {}
        elif p['color_price'] is None:
            p['color_price'] = {}

    return render_template("admin_products_verification.html", products=products)


@app.route("/admin_products_verification_search")
def admin_products_verification_search():
    search_query = request.args.get('search', '').strip()
    conn = get_db()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    sql = """
        SELECT p.product_id, p.seller_id, p.name, p.description, p.category_id, p.discount,
               p.colors, p.color_images, p.color_stock, p.stock, p.specifications, p.main_image,
               p.gallery_images, p.video, p.created_at, p.status, p.color_price,
               p.color_original_price, s.first_name, s.middle_name, s.last_name, s.suffix, s.business_name
        FROM products p LEFT JOIN sellers s ON p.seller_id=s.seller_id
    """
    params = []
    if search_query:
        sql += " WHERE p.name LIKE %s OR CONCAT(s.first_name,' ',s.last_name) LIKE %s OR CAST(p.product_id AS TEXT) LIKE %s"
        like_query = f"%{search_query}%"
        params.extend([like_query, like_query, like_query])
    sql += " ORDER BY p.created_at DESC"
    cursor.execute(sql, tuple(params))
    products = cursor.fetchall()
    db_pool.putconn(conn)

    for p in products:
        # Check if already a dict (JSONB column), otherwise parse JSON string
        if isinstance(p['color_images'], str):
            p['color_images'] = json.loads(p['color_images']) if p['color_images'] else {}
        elif p['color_images'] is None:
            p['color_images'] = {}
            
        if isinstance(p['color_price'], str):
            p['color_price'] = json.loads(p['color_price']) if p['color_price'] else {}
        elif p['color_price'] is None:
            p['color_price'] = {}

    return jsonify(products)


@app.route('/admin_approve_product/<int:product_id>')
def admin_approve_product(product_id):
    conn = get_db()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cursor.execute("UPDATE products SET status='approved' WHERE product_id=%s", (product_id,))
    conn.commit()
    db_pool.putconn(conn)
    return redirect(url_for('admin_products_verification'))


@app.route('/admin_reject_product/<int:product_id>')
def admin_reject_product(product_id):
    conn = get_db()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cursor.execute("UPDATE products SET status='rejected' WHERE product_id=%s", (product_id,))
    conn.commit()
    db_pool.putconn(conn)
    return redirect(url_for('admin_products_verification'))


@app.route("/admin_orders")
def admin_orders():
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    search_query = request.args.get('search', '').strip()
    status_filter = request.args.get('status', '').strip()

    # PostgreSQL: use CONCAT function (same as MySQL here)
    order_sql = """
        SELECT o.order_id, o.user_id, o.subtotal, o.shipping_fee, o.total,
               o.payment_method, o.payment_status, o.order_status, o.created_at, o.rider_id,
               CONCAT(u.first_name, ' ', u.last_name) AS buyer_name,
               CONCAT(r.first_name, ' ', r.last_name) AS rider_name
        FROM orders o
        LEFT JOIN users u ON o.user_id=u.user_id
        LEFT JOIN riders r ON o.rider_id=r.rider_id
    """
    conditions = []
    params = []

    if search_query:
        # PostgreSQL: cast order_id to text for LIKE
        conditions.append("(CAST(o.order_id AS TEXT) LIKE %s OR CONCAT(u.first_name, ' ', u.last_name) LIKE %s)")
        params.extend([f"%{search_query}%", f"%{search_query}%"])
    if status_filter and status_filter.lower() != "all status":
        conditions.append("o.order_status = %s")
        params.append(status_filter)
    if conditions:
        order_sql += " WHERE " + " AND ".join(conditions)
    order_sql += " ORDER BY o.order_id DESC"

    cur.execute(order_sql, tuple(params))
    orders = cur.fetchall()

    cur.execute("""
        SELECT oi.item_id, oi.order_id, oi.product_id, p.name AS product_name,
               oi.seller_id, s.business_name AS seller_name, oi.quantity, oi.total_price,
               oi.status, oi.order_received, oi.received_at, oi.cancel_reason
        FROM order_items oi
        LEFT JOIN sellers s ON oi.seller_id=s.seller_id
        LEFT JOIN products p ON oi.product_id=p.product_id
        ORDER BY oi.order_id DESC
    """)
    order_items = cur.fetchall()

    cur.execute("SELECT earning_id, order_id, item_id, amount, payout_status, created_at, paid_at FROM sellers_earnings")
    earnings = cur.fetchall()

    cur.execute("SELECT shipping_id, order_id, full_name, mobile, region, province, city, barangay, postal, street FROM order_shipping")
    shipping = cur.fetchall()

    total_orders = len(orders)
    pending_orders = len([o for o in orders if o['order_status'].lower() == 'pending'])
    shipped_orders = len([o for o in orders if o['order_status'].lower() == 'shipped'])
    cancelled_orders = len([o for o in orders if o['order_status'].lower() == 'cancelled'])

    db_pool.putconn(conn)
    return render_template("admin_orders.html", orders=orders, order_items=order_items,
                           earnings=earnings, shipping=shipping, total_orders=total_orders,
                           pending_orders=pending_orders, shipped_orders=shipped_orders,
                           cancelled_orders=cancelled_orders, search_query=search_query,
                           status_filter=status_filter)


@app.route("/admin_pay_seller/<int:earning_id>", methods=["POST"])
def admin_pay_seller(earning_id):
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("UPDATE sellers_earnings SET payout_status='paid', paid_at=NOW() WHERE earning_id=%s", (earning_id,))
    conn.commit()
    db_pool.putconn(conn)
    return redirect(url_for("admin_orders"))


@app.route("/admin_reports")
def admin_reports():
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute("""
        SELECT SUM(oi.total_price * 0.10) AS admin_revenue
        FROM order_items oi LEFT JOIN sellers_earnings se ON se.item_id=oi.item_id
        WHERE oi.order_received=1 AND oi.status='Delivered' AND se.payout_status='paid'
    """)
    result = cur.fetchone()
    admin_revenue = round(float(result["admin_revenue"]), 2) if result["admin_revenue"] else 0.00

    cur.execute("SELECT COUNT(*) AS total_orders FROM orders")
    total_orders = cur.fetchone()["total_orders"]

    cur.execute("SELECT COUNT(*) AS new_users FROM users")
    new_users = cur.fetchone()["new_users"]

    cur.execute("SELECT COUNT(*) AS active_drivers FROM riders WHERE verified='approved'")
    active_drivers = cur.fetchone()["active_drivers"]

    cur.execute("""
        SELECT DATE(created_at) AS date, SUM(total_price) AS total_sales
        FROM order_items WHERE status='Delivered' AND order_received=1
        GROUP BY DATE(created_at) ORDER BY DATE(created_at)
    """)
    chart_data = cur.fetchall()

    cur.execute("""
        SELECT oi.item_id, oi.order_id, oi.total_price, oi.quantity, oi.status, oi.created_at,
               u.first_name AS buyer_first, u.last_name AS buyer_last,
               s.business_name, p.name AS product_name
        FROM order_items oi
        LEFT JOIN users u ON oi.user_id=u.user_id
        LEFT JOIN products p ON oi.product_id=p.product_id
        LEFT JOIN sellers s ON oi.seller_id=s.seller_id
        ORDER BY oi.created_at DESC
    """)
    detailed_reports = cur.fetchall()
    db_pool.putconn(conn)

    return render_template("admin_reports.html", admin_revenue=admin_revenue,
                           total_orders=total_orders, new_users=new_users,
                           active_drivers=active_drivers, chart_data=chart_data,
                           detailed_reports=detailed_reports)


@app.route("/admin_settings")
def admin_settings():
    return render_template("admin_settings.html")


@app.route("/seller_dashboard")
def seller_dashboard():
    seller_id = session.get('seller_id')
    conn = get_db()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cursor.execute("SELECT first_name, business_name, store_logo FROM sellers WHERE seller_id=%s", (seller_id,))
    seller = cursor.fetchone()

    cursor.execute("SELECT COUNT(*) AS count FROM products WHERE seller_id=%s", (seller_id,))
    products_count = cursor.fetchone()['count']

    # PostgreSQL: use COALESCE instead of IFNULL
    cursor.execute("SELECT COALESCE(SUM(total_price),0) AS total FROM order_items WHERE seller_id=%s AND status='Delivered'", (seller_id,))
    total_sales = cursor.fetchone()['total']

    cursor.execute("SELECT COUNT(*) AS count FROM order_items WHERE seller_id=%s", (seller_id,))
    orders_count = cursor.fetchone()['count']

    cursor.execute("SELECT COALESCE(SUM(amount),0) AS total FROM sellers_earnings WHERE seller_id=%s AND payout_status='Paid'", (seller_id,))
    total_earnings = cursor.fetchone()['total']

    cursor.execute("""
        SELECT o.order_id, p.name AS product_name, u.first_name AS customer_name,
               o.created_at, oi.status, oi.total_price
        FROM order_items oi
        JOIN orders o ON oi.order_id=o.order_id
        JOIN products p ON oi.product_id=p.product_id
        JOIN users u ON o.user_id=u.user_id
        WHERE oi.seller_id=%s ORDER BY o.created_at DESC LIMIT 5
    """, (seller_id,))
    recent_orders = cursor.fetchall()

    # PostgreSQL: use TO_CHAR instead of DATE_FORMAT
    cursor.execute("""
        SELECT TO_CHAR(o.created_at, 'YYYY-MM') AS month, COALESCE(SUM(oi.total_price),0) AS sales
        FROM order_items oi JOIN orders o ON oi.order_id=o.order_id
        WHERE oi.seller_id=%s AND oi.status='Delivered'
        GROUP BY TO_CHAR(o.created_at, 'YYYY-MM')
        ORDER BY month ASC
    """, (seller_id,))
    sales_chart = cursor.fetchall()
    db_pool.putconn(conn)

    months = [row['month'] for row in sales_chart]
    sales = [float(row['sales']) for row in sales_chart]

    return render_template("seller_dashboard.html", seller=seller, products_count=products_count,
                           total_sales=total_sales, orders_count=orders_count, total_earnings=total_earnings,
                           recent_orders=recent_orders, chart_months=months, chart_sales=sales)


@app.route("/seller_products", methods=["GET"])
def seller_products():
    if "seller_id" not in session:
        flash("Please login first", "error")
        return redirect(url_for("login"))

    conn = get_db()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    
    # DEBUG: Check seller_id
    print(f"DEBUG - seller_id from session: {session.get('seller_id')}")
    
    raw_search = request.args.get("search", "") or ""
    search_query = raw_search.strip()
    sq = search_query.lower()

    status_in_keywords = {"in stock", "instock", "available"}
    status_out_keywords = {"out of stock", "outofstock", "none", "out_of_stock"}

    if sq in status_in_keywords or sq in status_out_keywords:
        query = """
            SELECT p.*, c.main_category, c.category_name, c.sub_category
            FROM products p LEFT JOIN categories c ON p.category_id=c.category_id
            WHERE p.seller_id=%s
        """
        params = (session['seller_id'],)
    else:
        like_query = f"%{search_query}%"
        query = """
            SELECT p.*, c.main_category, c.category_name, c.sub_category
            FROM products p LEFT JOIN categories c ON p.category_id=c.category_id
            WHERE p.seller_id=%s AND (
                p.name ILIKE %s OR p.description ILIKE %s OR
                c.category_name ILIKE %s OR c.sub_category ILIKE %s OR
                CAST(p.stock AS TEXT) ILIKE %s OR CAST(p.color_stock AS TEXT) ILIKE %s
            )
        """
        params = (session['seller_id'], like_query, like_query, like_query, like_query, like_query, like_query)
    
    # DEBUG: Print query and params
    print(f"DEBUG - Query: {query}")
    print(f"DEBUG - Params: {params}")
    
    cursor.execute(query, params)
    products = cursor.fetchall()
    
    # DEBUG: Check what was retrieved
    print(f"DEBUG - Number of products found: {len(products)}")
    if products:
        print(f"DEBUG - First product: {products[0]}")
    
    cursor.execute("SELECT category_id, main_category, category_name, sub_category FROM categories")
    categories = cursor.fetchall()
    main_categories = sorted({c["main_category"] for c in categories})

    for p in products:
        # DEBUG: Check raw values
        print(f"DEBUG - Product {p.get('product_id')}: color_stock={p.get('color_stock')}, colors={p.get('colors')}")
        
        # Supabase returns JSONB as dict already - check if it's already a dict
        if isinstance(p.get("color_stock"), dict):
            p["color_stock"] = p.get("color_stock", {})
        else:
            try:
                p["color_stock"] = json.loads(p.get("color_stock", "{}") or "{}")
            except Exception as e:
                print(f"ERROR parsing color_stock: {e}")
                p["color_stock"] = {}
        
        if isinstance(p.get("color_images"), dict):
            p["color_images"] = p.get("color_images", {})
        else:
            try:
                p["color_images"] = json.loads(p.get("color_images", "{}") or "{}")
            except Exception as e:
                print(f"ERROR parsing color_images: {e}")
                p["color_images"] = {}
        
        if isinstance(p.get("color_price"), dict):
            p["color_price"] = p.get("color_price", {})
        else:
            try:
                p["color_price"] = json.loads(p.get("color_price", "{}") or "{}")
            except Exception as e:
                print(f"ERROR parsing color_price: {e}")
                p["color_price"] = {}
        
        if isinstance(p.get("color_original_price"), dict):
            p["color_original_price"] = p.get("color_original_price", {})
        else:
            try:
                p["color_original_price"] = json.loads(p.get("color_original_price", "{}") or "{}")
            except Exception as e:
                print(f"ERROR parsing color_original_price: {e}")
                p["color_original_price"] = {}

        # Parse colors string
        p["colors"] = [c.strip() for c in (p.get("colors") or "").split(",") if c.strip()]
        
        for color in p["colors"]:
            p["color_stock"].setdefault(color, 0)
            p["color_price"].setdefault(color, 0)
            p["color_original_price"].setdefault(color, "")
        
        p["total_stock"] = sum(p["color_stock"].values())
        print(f"DEBUG - Product {p.get('product_id')}: total_stock={p['total_stock']}")

    if sq in status_in_keywords:
        products = [p for p in products if p["total_stock"] > 0]
    elif sq in status_out_keywords:
        products = [p for p in products if p["total_stock"] == 0]

    db_pool.putconn(conn)
    return render_template("seller_products.html", products=products, categories=categories,
                           main_categories=main_categories, search_query=search_query)


@app.route("/get_product_image/<path:filename>")
def get_product_image(filename):
    """Generate Supabase Storage URL"""
    if filename.startswith('http'):
        return redirect(filename)  # Already a full URL
    
    # Generate Supabase Storage public URL
    supabase_url = SUPABASE_URL.replace('/rest/v1', '')
    return redirect(f"{supabase_url}/storage/v1/object/public/products/{filename}")

@app.route("/insert_product", methods=["POST"])
def insert_product():
    if "seller_id" not in session:
        flash("Please login first", "error")
        return redirect(url_for("login"))

    seller_id = session["seller_id"]
    name = request.form.get("product_name")
    description = request.form.get("description")
    main_category = request.form.get("main_category")
    sub_category = request.form.get("sub_category")
    colors = request.form.get("colors")
    specifications = request.form.get("specifications")

    conn = get_db()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cursor.execute("SELECT category_id FROM categories WHERE main_category=%s AND sub_category=%s",
                   (main_category, sub_category))
    cat_result = cursor.fetchone()
    category_id = cat_result['category_id'] if cat_result else None

    # ============================================
    # UPLOAD MAIN IMAGE TO SUPABASE
    # ============================================
    main_image = request.files.get("main_image")
    if not main_image or main_image.filename == "":
        flash("Main image is required.", "error")
        db_pool.putconn(conn)
        return redirect(url_for("seller_products"))

    if not allowed_file(main_image.filename):
        flash("Invalid main image format.", "error")
        db_pool.putconn(conn)
        return redirect(url_for("seller_products"))

    # Upload to Supabase instead of local storage
    main_image_path = upload_to_supabase_storage(main_image, "products")

    # ============================================
    # PROCESS COLOR IMAGES
    # ============================================
    color_stock_dict = {}
    color_images_dict = {}
    color_price_dict = {}
    color_original_price_dict = {}

    if colors:
        for color in [c.strip() for c in colors.split(",") if c.strip()]:
            img = request.files.get(f"color_images_{color}")
            if img and allowed_file(img.filename):
                # Upload to Supabase
                color_images_dict[color] = upload_to_supabase_storage(img, "products")
            
            stock_value = request.form.get(f"stock_{color}")
            color_stock_dict[color] = int(stock_value) if stock_value else 0
            
            price_value = request.form.get(f"price_{color}")
            color_price_dict[color] = f"{float(price_value):.2f}" if price_value else "0.00"
            
            orig_price_value = request.form.get(f"original_price_{color}")
            color_original_price_dict[color] = f"{float(orig_price_value):.2f}" if orig_price_value else None

    stock = sum(color_stock_dict.values())

    # ============================================
    # UPLOAD GALLERY IMAGES TO SUPABASE
    # ============================================
    gallery_files = [f for f in request.files.getlist("gallery_images[]") if f and f.filename != ""]
    gallery_paths = []
    for img in gallery_files:
        if allowed_file(img.filename):
            # Upload to Supabase
            gallery_paths.append(upload_to_supabase_storage(img, "products"))
    gallery_images_str = ",".join(gallery_paths)

    # ============================================
    # UPLOAD VIDEO TO SUPABASE
    # ============================================
    video_path = None
    video_file = request.files.get("product_video")
    ALLOWED_VIDEO = {"mp4", "mov", "webm", "mkv"}
    if video_file and video_file.filename != "":
        ext = video_file.filename.rsplit(".", 1)[1].lower()
        if ext in ALLOWED_VIDEO:
            # Upload to Supabase
            video_path = upload_to_supabase_storage(video_file, "products")

    try:
        cursor.execute("""
            INSERT INTO products (seller_id, name, description, category_id, color_original_price,
                colors, stock, specifications, main_image, gallery_images, video,
                color_images, color_stock, color_price, status)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (seller_id, name, description, category_id, json.dumps(color_original_price_dict),
              colors, stock, specifications, main_image_path, gallery_images_str, video_path,
              json.dumps(color_images_dict), json.dumps(color_stock_dict), json.dumps(color_price_dict), "pending"))
        conn.commit()
        flash("Product added successfully!", "success")
    except Exception as e:
        conn.rollback()
        flash("Database error! Check console.", "error")
        print("DB ERROR:", e)
    finally:
        db_pool.putconn(conn)

    return redirect(url_for("seller_products"))


@app.route("/update_product", methods=["POST"])
def update_product():
    product_id = request.form.get("product_id")
    conn = get_db()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    name = request.form.get("name")
    description = request.form.get("description")
    category_id = request.form.get("category_id")
    discount = request.form.get("discount")
    specifications = request.form.get("specifications")

    ALLOWED_IMAGE = {"jpg", "jpeg", "png", "gif", "webp"}
    ALLOWED_VIDEO = {"mp4", "mov", "webm", "mkv"}

    main_image_path = None
    main_image_file = request.files.get("main_imageEdit")
    if main_image_file and main_image_file.filename != "":
        ext = main_image_file.filename.rsplit(".", 1)[1].lower()
        if ext in ALLOWED_VIDEO:
            db_pool.putconn(conn)
            return jsonify({"success": False, "message": "Main image cannot be a video."})
        if ext not in ALLOWED_IMAGE:
            db_pool.putconn(conn)
            return jsonify({"success": False, "message": "Invalid main image format."})
        filename = secure_filename(main_image_file.filename)
        unique = str(uuid.uuid4()) + "_" + filename
        main_image_file.save(os.path.join(UPLOAD_FOLDER, unique))
        main_image_path = unique

    gallery_paths = []
    for g in request.files.getlist("gallery_imagesEdit[]"):
        if g and g.filename != "":
            ext = g.filename.rsplit(".", 1)[1].lower()
            if ext in ALLOWED_VIDEO or ext not in ALLOWED_IMAGE:
                db_pool.putconn(conn)
                return jsonify({"success": False, "message": f"Invalid gallery file: {g.filename}"})
            filename = secure_filename(g.filename)
            unique = str(uuid.uuid4()) + "_" + filename
            g.save(os.path.join(UPLOAD_FOLDER, unique))
            gallery_paths.append(unique)
    gallery_str = ",".join(gallery_paths) if gallery_paths else None

    video_path = None
    video_file = request.files.get("video")
    if video_file and video_file.filename != "":
        ext = video_file.filename.rsplit(".", 1)[1].lower()
        if ext in ALLOWED_IMAGE or ext not in ALLOWED_VIDEO:
            db_pool.putconn(conn)
            return jsonify({"success": False, "message": "Invalid video format."})
        filename = secure_filename(video_file.filename)
        unique = str(uuid.uuid4()) + "_" + filename
        video_file.save(os.path.join(UPLOAD_FOLDER, unique))
        video_path = unique

    cursor.execute("UPDATE products SET name=%s, description=%s, category_id=%s, discount=%s, specifications=%s WHERE product_id=%s",
                   (name, description, category_id, discount, specifications, product_id))
    if main_image_path:
        cursor.execute("UPDATE products SET main_image=%s WHERE product_id=%s", (main_image_path, product_id))
    if gallery_str:
        cursor.execute("UPDATE products SET gallery_images=%s WHERE product_id=%s", (gallery_str, product_id))
    if video_path:
        cursor.execute("UPDATE products SET video=%s WHERE product_id=%s", (video_path, product_id))

    conn.commit()
    db_pool.putconn(conn)
    return jsonify({"success": True})


@app.route("/update_product_colors/<int:product_id>", methods=["POST"])
def update_product_colors(product_id):
    conn = get_db()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cursor.execute("SELECT * FROM products WHERE product_id=%s AND seller_id=%s", (product_id, session['seller_id']))
    product = cursor.fetchone()
    if not product:
        db_pool.putconn(conn)
        return jsonify({"success": False, "message": "Product not found."})

    color_stock = json.loads(product.get("color_stock") or "{}")
    color_price = json.loads(product.get("color_price") or "{}")
    color_original_price = json.loads(product.get("color_original_price") or "{}")
    color_images = json.loads(product.get("color_images") or "{}")

    updated_color_stock = dict(color_stock)
    updated_color_price = dict(color_price)
    updated_color_original_price = dict(color_original_price)
    updated_color_images = dict(color_images)

    deleted_colors = [key.replace("delete_color_", "") for key in request.form if key.startswith("delete_color_")]
    for color in deleted_colors:
        for d in [updated_color_stock, updated_color_price, updated_color_original_price, updated_color_images]:
            d.pop(color, None)

    for key in request.form:
        if key.startswith("color_name_"):
            old_color = key.replace("color_name_", "")
            if old_color in deleted_colors:
                continue
            new_color = request.form[key].strip()
            stock = int(request.form.get(f"stock_{old_color}", 0))
            price = float(request.form.get(f"price_{old_color}", 0))
            orig_val = request.form.get(f"original_price_{old_color}", "")
            original_price = float(orig_val) if orig_val else None

            updated_color_stock[new_color] = stock
            updated_color_price[new_color] = price
            updated_color_original_price[new_color] = original_price
            updated_color_images[new_color] = color_images.get(old_color)

            if old_color != new_color:
                for d in [updated_color_stock, updated_color_price, updated_color_original_price, updated_color_images]:
                    d.pop(old_color, None)

    for file_key, file in request.files.items():
        if file_key.startswith("color_images_") and file.filename:
            color = file_key.replace("color_images_", "")
            filename = f"{product_id}_{color}_{secure_filename(file.filename)}"
            file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
            updated_color_images[color] = filename

    for key in request.form:
        if key.startswith("new_color_name_"):
            uid = key.replace("new_color_name_", "")
            new_color = request.form[key].strip()
            stock = int(request.form.get(f"new_stock_{uid}", 0))
            price = float(request.form.get(f"new_price_{uid}", 0))
            orig_val = request.form.get(f"new_original_price_{uid}", "")
            original_price = float(orig_val) if orig_val else None
            img_file = request.files.get(f"new_color_image_{uid}")
            if img_file and img_file.filename:
                filename = f"{product_id}_{uid}_{secure_filename(img_file.filename)}"
                img_file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
                updated_color_images[new_color] = filename
            else:
                updated_color_images[new_color] = None
            updated_color_stock[new_color] = stock
            updated_color_price[new_color] = price
            updated_color_original_price[new_color] = original_price

    cursor.execute("""
        UPDATE products SET color_stock=%s, color_price=%s, color_original_price=%s, color_images=%s, colors=%s
        WHERE product_id=%s
    """, (json.dumps(updated_color_stock), json.dumps(updated_color_price),
          json.dumps(updated_color_original_price), json.dumps(updated_color_images),
          ",".join(updated_color_stock.keys()), product_id))
    conn.commit()
    db_pool.putconn(conn)
    return jsonify({"success": True})


@app.route('/delete_product/<int:product_id>', methods=['GET', 'POST'])
def delete_product(product_id):
    if 'seller_id' not in session:
        flash("Please log in first.", "error")
        return redirect(url_for('seller_dashboard'))

    conn = get_db()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cursor.execute("SELECT * FROM products WHERE product_id=%s AND seller_id=%s", (product_id, session['seller_id']))
    product = cursor.fetchone()

    if not product:
        flash("Product not found or access denied.", "error")
        db_pool.putconn(conn)
        return redirect(url_for('seller_products'))

    for path in [product.get('main_image'), product.get('video')]:
        if path:
            full_path = os.path.join(app.root_path, 'static/uploads/sellers', path)
            if os.path.exists(full_path):
                os.remove(full_path)

    if product.get('gallery_images'):
        for img in product['gallery_images'].split(","):
            full_path = os.path.join(app.root_path, 'static/uploads/sellers', img)
            if os.path.exists(full_path):
                os.remove(full_path)

    cursor.execute("DELETE FROM products WHERE product_id=%s", (product_id,))
    conn.commit()
    db_pool.putconn(conn)
    flash("Product deleted successfully!", "success")
    return redirect(url_for('seller_products'))


@app.route("/seller/orders")
def seller_orders():
    if "seller_id" not in session:
        return redirect(url_for("login"))

    seller_id = session["seller_id"]
    search_query = request.args.get("search", "").strip()
    conn = get_db()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    def build_order_query(extra_condition=""):
        return f"""
            SELECT oi.item_id, oi.order_id, oi.product_id, oi.variation, oi.quantity,
                   oi.price AS unit_price, oi.total_price AS item_total, oi.status AS item_status,
                   o.subtotal, o.shipping_fee, o.total AS order_total, o.created_at,
                   o.payment_method, o.order_status,
                   s.full_name, s.mobile, s.region, s.province, s.city, s.barangay, s.postal, s.street,
                   p.name AS product_name, p.main_image
            FROM order_items oi
            JOIN orders o ON oi.order_id=o.order_id
            LEFT JOIN order_shipping s ON s.order_id=o.order_id
            LEFT JOIN products p ON oi.product_id=p.product_id
            WHERE oi.seller_id=%s {extra_condition}
            ORDER BY o.created_at DESC
        """

    active_condition = "AND o.order_status != 'Delivered' AND oi.status != 'Cancelled'"
    params_active = [seller_id]
    if search_query:
        active_condition += " AND (CAST(o.order_id AS TEXT) LIKE %s OR s.full_name LIKE %s OR p.name LIKE %s)"
        params_active.extend([f"%{search_query}%"] * 3)

    cursor.execute(build_order_query(active_condition), tuple(params_active))
    rows_active = cursor.fetchall()

    history_condition = "AND (o.order_status='Delivered' OR oi.status='Cancelled')"
    params_history = [seller_id]
    if search_query:
        history_condition += " AND (CAST(o.order_id AS TEXT) LIKE %s OR s.full_name LIKE %s OR p.name LIKE %s)"
        params_history.extend([f"%{search_query}%"] * 3)

    cursor.execute(build_order_query(history_condition), tuple(params_history))
    rows_history = cursor.fetchall()
    db_pool.putconn(conn)

    def group_orders(rows):
        grouped = {}
        for r in rows:
            key = f"{r['order_id']}_{seller_id}"
            if key not in grouped:
                grouped[key] = {
                    "order_meta": {
                        "order_id": r["order_id"], "created_at": r["created_at"],
                        "subtotal": r["subtotal"], "shipping_fee": r["shipping_fee"],
                        "order_total": r["order_total"], "payment_method": r["payment_method"],
                        "order_status": r["order_status"], "buyer_name": r["full_name"],
                        "buyer_mobile": r["mobile"],
                        "shipping_address": ", ".join(filter(None, [r.get("street"), r.get("barangay"),
                                                                     r.get("city"), r.get("province"),
                                                                     r.get("region"), r.get("postal")]))
                    },
                    "items": []
                }
            grouped[key]["items"].append({
                "item_id": r["item_id"], "product_id": r["product_id"],
                "product_name": r["product_name"], "main_image": r["main_image"],
                "variation": r["variation"], "quantity": r["quantity"],
                "unit_price": float(r["unit_price"]), "item_total": float(r["item_total"]),
                "item_status": r["item_status"]
            })
        return {k: v for k, v in grouped.items() if v["items"]}

    return render_template("seller_orders.html", active_orders=group_orders(rows_active),
                           delivered_orders=group_orders(rows_history))


@app.route("/seller/orders/search")
def seller_orders_search():
    if "seller_id" not in session:
        return jsonify({"error": "Not logged in"}), 403

    seller_id = session["seller_id"]
    search_query = request.args.get("q", "").strip()
    conn = get_db()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    query = """
        SELECT oi.item_id, oi.order_id, oi.product_id, oi.variation, oi.quantity,
               oi.price AS unit_price, oi.total_price AS item_total, oi.status AS item_status,
               o.subtotal, o.shipping_fee, o.total AS order_total, o.created_at,
               o.payment_method, o.order_status,
               s.full_name, s.mobile, s.region, s.province, s.city, s.barangay, s.postal, s.street,
               p.name AS product_name, p.main_image
        FROM order_items oi JOIN orders o ON oi.order_id=o.order_id
        LEFT JOIN order_shipping s ON s.order_id=o.order_id
        LEFT JOIN products p ON oi.product_id=p.product_id
        WHERE oi.seller_id=%s
    """
    params = [seller_id]
    if search_query:
        query += " AND (CAST(o.order_id AS TEXT) LIKE %s OR s.full_name LIKE %s OR p.name LIKE %s)"
        params.extend([f"%{search_query}%"] * 3)
    query += " ORDER BY o.created_at DESC"
    cursor.execute(query, tuple(params))
    rows = cursor.fetchall()
    db_pool.putconn(conn)

    orders = {}
    for r in rows:
        key = f"{r['order_id']}_{seller_id}"
        if key not in orders:
            orders[key] = {
                "order_meta": {"order_id": r["order_id"], "created_at": str(r["created_at"]),
                               "subtotal": str(r["subtotal"]), "shipping_fee": str(r["shipping_fee"]),
                               "order_total": str(r["order_total"]), "payment_method": r["payment_method"],
                               "order_status": r["order_status"], "buyer_name": r["full_name"],
                               "buyer_mobile": r["mobile"],
                               "shipping_address": ", ".join(filter(None, [r.get("street"), r.get("barangay"),
                                                                            r.get("city"), r.get("province"),
                                                                            r.get("region"), r.get("postal")]))},
                "items": []
            }
        orders[key]["items"].append({
            "item_id": r["item_id"], "product_id": r["product_id"],
            "product_name": r["product_name"], "main_image": r["main_image"],
            "variation": r["variation"], "quantity": r["quantity"],
            "unit_price": float(r["unit_price"]), "item_total": float(r["item_total"]),
            "item_status": r["item_status"]
        })
    return jsonify({"orders": orders})


@app.route("/seller/order/<int:order_id>/json")
def seller_order_detail_json(order_id):
    seller_id = session["seller_id"]
    conn = get_db()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cursor.execute("""
        SELECT o.order_id, o.user_id, o.created_at, o.subtotal, o.shipping_fee, o.total,
               o.payment_method, o.order_status,
               s.full_name, s.mobile, s.street, s.barangay, s.city, s.province, s.region, s.postal
        FROM orders o LEFT JOIN order_shipping s ON s.order_id=o.order_id
        WHERE o.order_id=%s LIMIT 1
    """, (order_id,))
    order_meta = cursor.fetchone()
    if not order_meta:
        db_pool.putconn(conn)
        return jsonify({"error": "Order not found"}), 404

    cursor.execute("""
        SELECT oi.item_id, oi.product_id, oi.variation, oi.quantity, oi.price, oi.total_price, oi.status,
               p.name AS product_name, p.main_image
        FROM order_items oi LEFT JOIN products p ON oi.product_id=p.product_id
        WHERE oi.order_id=%s AND oi.seller_id=%s
    """, (order_id, seller_id))
    items = cursor.fetchall()
    db_pool.putconn(conn)

    if not items:
        return jsonify({"error": "No items for this seller"}), 403

    shipping_address = ", ".join(filter(None, [order_meta.get(k, "") for k in ["street","barangay","city","province","region","postal"]]))
    return jsonify({"order_meta": order_meta, "items": items, "shipping_address": shipping_address})


@app.route("/seller/order/update_status", methods=["POST"])
def update_order_item_status():
    if "seller_id" not in session:
        return jsonify({"error": "Unauthorized"}), 403

    data = request.get_json()
    item_id = data.get("item_id")
    new_status = data.get("status")
    seller_id = session["seller_id"]

    if not item_id or not new_status:
        return jsonify({"error": "Invalid"}), 400

    conn = get_db()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    try:
        cursor.execute("SELECT order_id, status FROM order_items WHERE item_id=%s AND seller_id=%s", (item_id, seller_id))
        item = cursor.fetchone()
        if not item:
            return jsonify({"error": "Item not found"}), 404

        order_id = item["order_id"]
        current_status = item["status"]

        if current_status == "Shipped" and new_status == "Preparing":
            return jsonify({"error": "Cannot revert shipped item back to preparing."}), 403

        cursor.execute("SELECT order_status FROM orders WHERE order_id=%s", (order_id,))
        order_row = cursor.fetchone()
        if order_row and order_row.get("order_status") == "Delivered":
            return jsonify({"error": "Order already delivered."}), 403

        cursor.execute("UPDATE order_items SET status=%s WHERE item_id=%s AND seller_id=%s", (new_status, item_id, seller_id))

        rider_id = None
        if new_status == "Shipped":
            cursor.execute("SELECT COUNT(*) AS pending FROM order_items WHERE order_id=%s AND status NOT IN ('Shipped','Cancelled')", (order_id,))
            pending = cursor.fetchone()["pending"]

            if pending == 0:
                cursor.execute("SELECT region, province, city FROM order_shipping WHERE order_id=%s LIMIT 1", (order_id,))
                adr = cursor.fetchone()

                if not adr or not adr.get("region"):
                    conn.commit()
                    db_pool.putconn(conn)
                    return jsonify({"success": True, "rider_assigned": False})

                cursor.execute("""
                    SELECT rider_id FROM riders
                    WHERE region=%s AND province=%s AND city=%s AND verified='approved' LIMIT 1
                """, (adr["region"], adr["province"], adr["city"]))
                rider = cursor.fetchone()
                rider_id = rider["rider_id"] if rider else None

                if rider_id:
                    cursor.execute("UPDATE orders SET rider_id=%s, order_status='Shipped' WHERE order_id=%s", (rider_id, order_id))
                else:
                    cursor.execute("UPDATE orders SET rider_id=NULL WHERE order_id=%s", (order_id,))

        conn.commit()
        return jsonify({"success": True, "rider_assigned": bool(rider_id)})

    except Exception as e:
        conn.rollback()
        print("ERROR update_order_item_status:", e)
        return jsonify({"error": "Could not update status"}), 500
    finally:
        cursor.close()
        db_pool.putconn(conn)


@app.route("/seller/order/accept_item", methods=["POST"])
def accept_item():
    data = request.get_json()
    item_id = data["item_id"]
    conn = get_db()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cursor.execute("UPDATE order_items SET status='Accepted' WHERE item_id=%s", (item_id,))
    conn.commit()
    db_pool.putconn(conn)
    return jsonify({"success": True})


@app.route("/seller/order/reject_item", methods=["POST"])
def reject_item():
    data = request.get_json()
    item_id = data["item_id"]
    reason = data["reason"]

    conn = get_db()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    try:
        cursor.execute("SELECT product_id, variation, quantity FROM order_items WHERE item_id=%s", (item_id,))
        item = cursor.fetchone()
        if not item:
            return jsonify({"error": "Item not found"}), 404

        cursor.execute("SELECT color_stock, stock FROM products WHERE product_id=%s", (item["product_id"],))
        product = cursor.fetchone()
        if not product:
            return jsonify({"error": "Product not found"}), 404

        color_stock = json.loads(product["color_stock"])
        variation = item["variation"]
        quantity = item["quantity"]
        color_stock[variation] = color_stock.get(variation, 0) + quantity

        cursor.execute("UPDATE products SET color_stock=%s, stock=%s WHERE product_id=%s",
                       (json.dumps(color_stock), sum(color_stock.values()), item["product_id"]))
        cursor.execute("UPDATE order_items SET status='Cancelled', cancel_reason=%s WHERE item_id=%s", (reason, item_id))

        conn.commit()
        return jsonify({"success": True})

    except Exception as e:
        conn.rollback()
        return jsonify({"error": "Could not cancel item"}), 500
    finally:
        cursor.close()
        db_pool.putconn(conn)


@app.route("/seller_earnings")
def seller_earnings():
    seller_id = session.get("seller_id")
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute("SELECT * FROM sellers_earnings WHERE seller_id=%s ORDER BY created_at DESC", (seller_id,))
    earnings = cur.fetchall()

    total_earnings = sum(e["amount"] for e in earnings if e["payout_status"] == "paid")
    pending_payout = sum(1 for e in earnings if e["payout_status"] == "pending")
    completed_payout = sum(1 for e in earnings if e["payout_status"] == "paid")

    # PostgreSQL: use TO_CHAR instead of DATE_FORMAT
    cur.execute("""
        SELECT DATE(paid_at) AS label, SUM(amount) AS total
        FROM sellers_earnings WHERE seller_id=%s AND payout_status='paid'
        GROUP BY DATE(paid_at) ORDER BY label ASC
    """, (seller_id,))
    daily = cur.fetchall()

    cur.execute("""
        SELECT TO_CHAR(paid_at, 'YYYY-MM') AS label, SUM(amount) AS total
        FROM sellers_earnings WHERE seller_id=%s AND payout_status='paid'
        GROUP BY TO_CHAR(paid_at, 'YYYY-MM') ORDER BY label ASC
    """, (seller_id,))
    monthly = cur.fetchall()

    # PostgreSQL: use EXTRACT instead of YEAR()
    cur.execute("""
        SELECT EXTRACT(YEAR FROM paid_at) AS label, SUM(amount) AS total
        FROM sellers_earnings WHERE seller_id=%s AND payout_status='paid'
        GROUP BY EXTRACT(YEAR FROM paid_at) ORDER BY label ASC
    """, (seller_id,))
    yearly = cur.fetchall()
    db_pool.putconn(conn)

    return render_template("seller_earnings.html", earnings=earnings, total_earnings=total_earnings,
                           pending_payout=pending_payout, completed_payout=completed_payout,
                           daily_labels=[str(r["label"]) for r in daily],
                           daily_data=[r["total"] for r in daily],
                           monthly_labels=[str(r["label"]) for r in monthly],
                           monthly_data=[r["total"] for r in monthly],
                           yearly_labels=[str(r["label"]) for r in yearly],
                           yearly_data=[r["total"] for r in yearly])


@app.route("/seller_messages")
def seller_messages():
    return render_template("seller_messages.html")


@app.route("/seller_settings")
def seller_settings():
    return render_template("seller_settings.html")


@app.route("/driver_dashboard")
def driver_dashboard():
    if "rider_id" not in session:
        return redirect(url_for("login"))

    rider_id = session["rider_id"]
    conn = get_db()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cursor.execute("""
        SELECT o.order_id, o.created_at, o.shipping_fee, os.full_name,
               CONCAT_WS(', ', os.street, os.barangay, os.city, os.province) AS address,
               oi.status AS item_status
        FROM orders o JOIN order_shipping os ON os.order_id=o.order_id
        JOIN order_items oi ON oi.order_id=o.order_id
        WHERE o.rider_id=%s ORDER BY o.created_at DESC
    """, (rider_id,))
    deliveries = cursor.fetchall()

    current_deliveries = [d for d in deliveries if d['item_status'] not in ('Delivered', 'Cancelled')]
    active_count = len(current_deliveries)
    completed_count = sum(1 for d in deliveries if d['item_status'] == 'Delivered')
    today = datetime.now().date()
    today_earnings = sum(d['shipping_fee'] for d in deliveries
                         if d['item_status'] == 'Delivered' and d['created_at'].date() == today)

    cursor.execute("SELECT AVG(rating) AS avg_rating FROM rider_reviews WHERE rider_id=%s", (rider_id,))
    avg_rating = cursor.fetchone()['avg_rating'] or 0
    db_pool.putconn(conn)

    addresses_with_coords = []
    for d in current_deliveries:
        lat, lon = geocode_address(d['address'])
        if lat and lon:
            addresses_with_coords.append({"address": d['address'], "lat": lat, "lon": lon})
        time.sleep(1)

    return render_template("driver_dashboard.html", active_deliveries=active_count,
                           completed_deliveries=completed_count, addresses_with_coords=addresses_with_coords,
                           today_earnings=today_earnings, overall_rating=round(avg_rating, 1),
                           current_deliveries=current_deliveries)


@app.route("/driver/deliveries")
def driver_deliveries():
    if "rider_id" not in session:
        return redirect(url_for("login"))

    rider_id = session["rider_id"]
    conn = get_db()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cursor.execute("""
        SELECT o.order_id, o.created_at, o.shipping_fee, os.full_name,
               CONCAT_WS(', ', os.street, os.barangay, os.city, os.province) AS address,
               oi.item_id, oi.status AS item_status, p.name AS product_name, oi.variation
        FROM orders o JOIN order_shipping os ON os.order_id=o.order_id
        JOIN order_items oi ON oi.order_id=o.order_id
        LEFT JOIN products p ON oi.product_id=p.product_id
        WHERE o.rider_id=%s ORDER BY o.created_at DESC, oi.item_id ASC
    """, (rider_id,))
    rows = cursor.fetchall()
    db_pool.putconn(conn)

    deliveries_dict = {}
    for row in rows:
        oid = row["order_id"]
        if oid not in deliveries_dict:
            deliveries_dict[oid] = {"order_id": oid, "full_name": row["full_name"],
                                    "address": row["address"], "created_at": row["created_at"],
                                    "shipping_fee": row["shipping_fee"], "order_items": []}
        if row["item_status"] != "Cancelled":
            deliveries_dict[oid]["order_items"].append({"item_id": row["item_id"],
                                                        "product_name": row["product_name"],
                                                        "variation": row["variation"],
                                                        "item_status": row["item_status"]})

    deliveries = list(deliveries_dict.values())
    for d in deliveries:
        statuses = [i["item_status"] for i in d["order_items"]]
        if not statuses:
            d["item_status"] = "Cancelled"
        elif all(s == "Delivered" for s in statuses):
            d["item_status"] = "Delivered"
        elif all(s == "Shipped" for s in statuses):
            d["item_status"] = "Shipped"
        elif any(s == "Delivery" for s in statuses):
            d["item_status"] = "Delivery"
        else:
            d["item_status"] = "Pending"

    active_deliveries = []
    delivery_history = []
    for d in deliveries:
        if d["item_status"] in ("Delivered", "Cancelled"):
            delivery_history.append({"order_id": d["order_id"], "full_name": d["full_name"],
                                     "delivered_at": d["created_at"], "payment_method": "COD",
                                     "status": d["item_status"],
                                     "earning": d.get("shipping_fee", 0) if d["item_status"] == "Delivered" else 0})
        else:
            active_deliveries.append(d)

    active_deliveries = [d for d in active_deliveries if d["order_items"]]
    return render_template("driver_deliveries.html", deliveries=active_deliveries, delivery_history=delivery_history)


@app.route("/driver/order/<int:order_id>/update_status", methods=["POST"])
def update_delivery_status(order_id):
    if "rider_id" not in session:
        return jsonify({"error": "Unauthorized"}), 403

    data = request.json
    new_status = data.get("status")
    item_id = data.get("item_id")
    rider_id = session["rider_id"]

    conn = get_db()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # PostgreSQL: no JOIN in UPDATE, use subquery
    cursor.execute("""
        UPDATE order_items SET status=%s
        WHERE item_id=%s AND order_id=%s
          AND order_id IN (SELECT order_id FROM orders WHERE rider_id=%s)
    """, (new_status, item_id, order_id, rider_id))
    conn.commit()

    cursor.execute("SELECT status FROM order_items WHERE order_id=%s", (order_id,))
    statuses = [row["status"] for row in cursor.fetchall()]
    if all(s == "Delivered" for s in statuses):
        cursor.execute("UPDATE orders SET order_status='Delivered' WHERE order_id=%s", (order_id,))
        conn.commit()

    db_pool.putconn(conn)
    return jsonify({"success": True})


@app.route("/driver/order/<int:order_id>/report_issue", methods=["POST"])
def report_delivery_issue(order_id):
    if "rider_id" not in session:
        return jsonify({"error": "Unauthorized"}), 403

    data = request.json
    rider_id = session["rider_id"]
    conn = get_db()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cursor.execute("INSERT INTO delivery_reports (order_id, rider_id, reason, note) VALUES (%s,%s,%s,%s)",
                   (order_id, rider_id, data.get("reason"), data.get("note", "")))
    conn.commit()
    db_pool.putconn(conn)
    return jsonify({"success": True, "message": "Issue reported successfully"})


@app.route("/driver/order/<int:order_id>")
def driver_order_details(order_id):
    if "rider_id" not in session:
        return redirect(url_for("login"))

    rider_id = session["rider_id"]
    conn = get_db()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cursor.execute("""
        SELECT o.order_id, o.created_at, os.full_name,
               CONCAT_WS(', ', os.street, os.barangay, os.city, os.province) AS address,
               oi.item_id, oi.status AS item_status, p.name AS product_name, oi.variation
        FROM orders o JOIN order_shipping os ON os.order_id=o.order_id
        JOIN order_items oi ON oi.order_id=o.order_id
        LEFT JOIN products p ON oi.product_id=p.product_id
        WHERE o.order_id=%s AND o.rider_id=%s AND oi.status != 'Cancelled'
    """, (order_id, rider_id))

    order_rows = cursor.fetchall()
    db_pool.putconn(conn)

    if not order_rows:
        return "Order not found or unauthorized", 404

    order_info = {
        "order_id": order_rows[0]["order_id"], "full_name": order_rows[0]["full_name"],
        "address": order_rows[0]["address"], "created_at": order_rows[0]["created_at"],
        "order_items": [{"item_id": r["item_id"], "product_name": r["product_name"],
                         "variation": r["variation"], "item_status": r["item_status"]} for r in order_rows]
    }
    return render_template("driver_order_details.html", order=order_info)


@app.route("/driver_earnings")
def driver_earnings():
    if "rider_id" not in session:
        return redirect(url_for("login"))

    rider_id = session["rider_id"]
    conn = get_db()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cursor.execute("""
        SELECT o.order_id, oi.created_at AS delivered_at, os.full_name,
               o.payment_method, o.shipping_fee AS earning
        FROM orders o JOIN order_shipping os ON os.order_id=o.order_id
        JOIN order_items oi ON oi.order_id=o.order_id
        WHERE oi.status='Delivered' AND o.rider_id=%s ORDER BY oi.created_at DESC
    """, (rider_id,))
    history = cursor.fetchall()
    db_pool.putconn(conn)

    total_earnings = sum(h["earning"] for h in history)
    today = datetime.now().date()
    today_earnings = sum(h["earning"] for h in history if h["delivered_at"].date() == today)
    today_deliveries = sum(1 for h in history if h["delivered_at"].date() == today)
    this_month = datetime.now().month
    this_year = datetime.now().year
    monthly_earnings = sum(h["earning"] for h in history
                           if h["delivered_at"].month == this_month and h["delivered_at"].year == this_year)

    earnings_by_day = {}
    for h in history:
        date_key = h["delivered_at"].strftime("%Y-%m-%d")
        earnings_by_day[date_key] = earnings_by_day.get(date_key, 0) + h["earning"]

    return render_template("driver_earnings.html", total_earnings=total_earnings,
                           today_earnings=today_earnings, today_deliveries=today_deliveries,
                           monthly_earnings=monthly_earnings, earnings_history=history,
                           earnings_by_day=earnings_by_day)


@app.route("/driver_performance")
def driver_performance():
    if "rider_id" not in session:
        return redirect(url_for("login"))

    rider_id = session["rider_id"]
    conn = get_db()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cursor.execute("""
        SELECT COUNT(*) AS total FROM order_items oi JOIN orders o ON oi.order_id=o.order_id
        WHERE oi.status='Delivered' AND o.rider_id=%s
    """, (rider_id,))
    total_deliveries = cursor.fetchone()["total"]

    cursor.execute("SELECT AVG(rating) AS avg_rating FROM rider_reviews WHERE rider_id=%s", (rider_id,))
    avg_rating = round(cursor.fetchone()["avg_rating"] or 0, 1)

    cursor.execute("""
        SELECT COUNT(*) AS c FROM order_items oi JOIN orders o ON oi.order_id=o.order_id
        WHERE o.rider_id=%s AND oi.status='Cancelled'
    """, (rider_id,))
    cancelled_orders = cursor.fetchone()["c"]

    cursor.execute("""
        SELECT DATE(o.created_at) AS ordered_day, DATE(oi.received_at) AS delivered_day
        FROM order_items oi JOIN orders o ON o.order_id=oi.order_id
        WHERE oi.status='Delivered' AND o.rider_id=%s
    """, (rider_id,))
    rows = cursor.fetchall()
    on_time = sum(1 for r in rows if r["ordered_day"] == r["delivered_day"])
    on_time_rate = round((on_time / len(rows) * 100), 2) if rows else 0

    # PostgreSQL: INTERVAL '7 days' syntax
    cursor.execute("""
        SELECT DATE(oi.received_at) AS day, COUNT(*) AS delivered
        FROM order_items oi JOIN orders o ON oi.order_id=o.order_id
        WHERE o.rider_id=%s AND oi.status='Delivered'
          AND DATE(oi.received_at) >= CURRENT_DATE - INTERVAL '7 days'
        GROUP BY DATE(oi.received_at) ORDER BY day ASC
    """, (rider_id,))
    weekly = cursor.fetchall()

    weekly_labels = [w["day"].strftime("%b %d") for w in weekly]
    weekly_values = [w["delivered"] for w in weekly]

    cursor.execute("""
        SELECT rr.review, rr.rating, u.first_name, u.last_name
        FROM rider_reviews rr JOIN users u ON rr.user_id=u.user_id
        WHERE rr.rider_id=%s ORDER BY rr.created_at DESC
    """, (rider_id,))
    reviews = cursor.fetchall()
    db_pool.putconn(conn)

    return render_template("driver_performance.html", total_deliveries=total_deliveries,
                           avg_rating=avg_rating, on_time_rate=on_time_rate, cancelled_orders=cancelled_orders,
                           weekly_labels=weekly_labels, weekly_values=weekly_values, reviews=reviews)


@app.route("/driver_messages")
def driver_messages():
    return render_template("driver_messages.html")


@app.route("/driver_settings")
def driver_settings():
    return render_template("driver_settings.html")


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('homepage'))


@app.route('/logout_seller')
def logout_seller():
    session.clear()
    return redirect(url_for('login'))


@app.route('/logout_admin')
def logout_admin():
    session.clear()
    return redirect(url_for('login_admin'))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
