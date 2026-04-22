from flask import Flask, request, jsonify, render_template, redirect, make_response, send_file
from flask_cors import CORS
from werkzeug.utils import secure_filename
import os
import tempfile
from datetime import datetime, timedelta
import cv2
import numpy as np
from PIL import Image, ImageEnhance
import pytesseract
from pdf2image import convert_from_path
import re
import json
import hashlib
import secrets
import io
import shutil
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image as ReportLabImage
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib import colors
from reportlab.lib.units import inch
import difflib
import sqlite3

app = Flask(__name__, static_folder='static', template_folder='templates')
CORS(app)

# Configuration
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, 'uploads')
REFERENCE_BASE_FOLDER = os.path.join(BASE_DIR, 'references')
REPORTS_FOLDER = os.path.join(BASE_DIR, 'reports')
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'pdf'}
MAX_FILE_SIZE = 5 * 1024 * 1024  # 5MB

# Supported document types
DOCUMENT_TYPES = ['drivers_license', 'national_id', 'passport']
DEFAULT_DOC_TYPE = 'drivers_license'

# Secret key for sessions (set FLASK_SECRET_KEY in production)
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'dev-change-this-secret')

# ============= SESSION MANAGEMENT =============
active_sessions = {}
SESSION_TIMEOUT = timedelta(hours=1)

# Role definitions
ROLES = {'admin', 'applicant'}

# Built-in bootstrap users. Passwords are hashed into the DB at startup.
DEFAULT_USERS = [
    {'username': 'admin', 'password': 'jethro123', 'role': 'admin', 'full_name': 'System Administrator'},
    {'username': 'officer', 'password': 'officer123', 'role': 'applicant', 'full_name': 'Loan Applicant'}
]

MODEL_DEFAULTS = {
    'version': 'risk_model_v1.0',
    'approval_threshold': 35.0,
    'reject_threshold': 65.0,
    'status': 'active'
}

def hash_password(password):
    return hashlib.sha256(password.encode('utf-8')).hexdigest()

def generate_session_token(username, role='applicant', full_name=''):
    """Generate a secure session token"""
    token = secrets.token_urlsafe(32)
    active_sessions[token] = {
        'username': username,
        'role': role,
        'full_name': full_name,
        'login_time': datetime.now(),
        'last_activity': datetime.now()
    }
    return token

def get_active_session(req):
    """Return session object if session token is valid, else None."""
    token = req.cookies.get('session_token')
    
    if token and token in active_sessions:
        session_data = active_sessions[token]
        
        # Check session timeout
        if datetime.now() - session_data['last_activity'] > SESSION_TIMEOUT:
            del active_sessions[token]
            return False
        
        # Update last activity
        session_data['last_activity'] = datetime.now()
        return session_data
    
    return None

def verify_session(req):
    """Verify if user has a valid session"""
    return get_active_session(req) is not None

def require_roles(req, allowed_roles):
    """Check whether the current session user has one of the required roles."""
    session_data = get_active_session(req)
    if not session_data:
        return False, jsonify({'success': False, 'message': 'Authentication required'}), 401
    if session_data.get('role') not in allowed_roles:
        return False, jsonify({'success': False, 'message': 'Insufficient permissions'}), 403
    return True, session_data, 200

def cleanup_expired_sessions():
    """Clean up expired sessions"""
    current_time = datetime.now()
    expired_tokens = []
    
    for token, session_data in active_sessions.items():
        if current_time - session_data['last_activity'] > SESSION_TIMEOUT:
            expired_tokens.append(token)
    
    for token in expired_tokens:
        del active_sessions[token]

# ============= HELPER FUNCTIONS =============
def allowed_file(filename):
    """Check if file extension is allowed"""
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def get_reference_folder(document_type):
    """Get the reference folder for a specific document type"""
    if document_type not in DOCUMENT_TYPES:
        return os.path.join(REFERENCE_BASE_FOLDER, DEFAULT_DOC_TYPE)
    return os.path.join(REFERENCE_BASE_FOLDER, document_type)

# Create necessary directories
for folder in [UPLOAD_FOLDER, REPORTS_FOLDER, os.path.join(BASE_DIR, 'static'), os.path.join(BASE_DIR, 'templates')]:
    os.makedirs(folder, exist_ok=True)

# Path for SQLite DB
DB_PATH = os.path.join(REPORTS_FOLDER, 'loan_applications.db')

def init_db():
    """Initialize SQLite database and create tables if missing."""
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute('''
            CREATE TABLE IF NOT EXISTS loan_applications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                full_name TEXT,
                contact TEXT,
                amount REAL,
                months INTEGER,
                interest_rate REAL,
                monthly_payment REAL,
                verification TEXT,
                start_date TEXT
            )
        ''')
        # Loan application schema migrations for enhanced underwriting features.
        loan_columns = [
            ('user_id', 'INTEGER'),
            ('start_date', 'TEXT'),
            ('employment_status', 'TEXT'),
            ('employer_name', 'TEXT'),
            ('monthly_income', 'REAL'),
            ('other_income', 'REAL'),
            ('credit_score', 'INTEGER'),
            ('existing_debt', 'REAL'),
            ('requested_documents', 'TEXT'),
            ('primary_id_type', 'TEXT'),
            ('primary_id_path', 'TEXT'),
            ('supporting_document_path', 'TEXT'),
            ('validation_issues', 'TEXT'),
            ('recommendation', 'TEXT'),
            ('confidence_score', 'REAL'),
            ('risk_level', 'TEXT'),
            ('risk_score', 'REAL'),
            ('fraud_flags', 'TEXT'),
            ('decision_explanation', 'TEXT'),
            ('model_version', 'TEXT'),
            ('model_raw_json', 'TEXT')
        ]
        for col_name, col_type in loan_columns:
            try:
                c.execute(f'ALTER TABLE loan_applications ADD COLUMN {col_name} {col_type}')
            except sqlite3.OperationalError:
                pass  # Column already exists
        
        # Payments table: store payments made against applications
        c.execute('''
            CREATE TABLE IF NOT EXISTS payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                application_id INTEGER,
                timestamp TEXT,
                months_paid INTEGER,
                amount_paid REAL,
                payer TEXT
            )
        ''')
        # Fully paid archive table: store completed loan records
        c.execute('''
            CREATE TABLE IF NOT EXISTS paid_loans_archive (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                original_id INTEGER,
                timestamp TEXT,
                full_name TEXT,
                contact TEXT,
                amount REAL,
                months INTEGER,
                interest_rate REAL,
                monthly_payment REAL,
                total_paid REAL,
                paid_date TEXT,
                verification TEXT
            )
        ''')

        # User accounts with role-based access.
        c.execute('''
            CREATE TABLE IF NOT EXISTS user_accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE,
                password_hash TEXT,
                role TEXT,
                full_name TEXT,
                is_active INTEGER DEFAULT 1,
                created_at TEXT,
                last_login TEXT
            )
        ''')

        # Ensure legacy databases have all user account columns required by admin pages.
        user_account_columns = [
            ('role', 'TEXT'),
            ('full_name', 'TEXT'),
            ('is_active', 'INTEGER DEFAULT 1'),
            ('created_at', 'TEXT'),
            ('last_login', 'TEXT')
        ]
        for col_name, col_type in user_account_columns:
            try:
                c.execute(f'ALTER TABLE user_accounts ADD COLUMN {col_name} {col_type}')
            except sqlite3.OperationalError:
                pass  # Column already exists

        # Model registry for lightweight administration and retraining metadata.
        c.execute('''
            CREATE TABLE IF NOT EXISTS model_registry (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                version TEXT,
                status TEXT,
                trained_at TEXT,
                notes TEXT,
                approval_threshold REAL,
                reject_threshold REAL,
                training_samples INTEGER DEFAULT 0
            )
        ''')

        # User profiles: extended user information for customers.
        c.execute('''
            CREATE TABLE IF NOT EXISTS user_profiles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER UNIQUE,
                email TEXT,
                phone TEXT,
                address TEXT,
                created_at TEXT,
                updated_at TEXT,
                FOREIGN KEY (user_id) REFERENCES user_accounts(id)
            )
        ''')

        # User documents: track document uploads per user for admin review.
        c.execute('''
            CREATE TABLE IF NOT EXISTS user_documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                document_type TEXT,
                file_path TEXT,
                uploaded_at TEXT,
                verified_by_admin INTEGER DEFAULT 0,
                verification_notes TEXT,
                FOREIGN KEY (user_id) REFERENCES user_accounts(id)
            )
        ''')

        # Normalize legacy roles so the system only keeps admin and applicant roles.
        c.execute("DELETE FROM user_accounts WHERE role = 'analyst'")
        c.execute("UPDATE user_accounts SET role = 'applicant' WHERE role = 'loan_officer'")
        c.execute("UPDATE user_accounts SET role = 'applicant', full_name = 'Loan Applicant' WHERE username = 'officer'")

        # Seed default users if missing.
        for user in DEFAULT_USERS:
            c.execute('SELECT id FROM user_accounts WHERE username = ?', (user['username'],))
            if not c.fetchone():
                c.execute(
                    '''INSERT INTO user_accounts (username, password_hash, role, full_name, is_active, created_at)
                       VALUES (?, ?, ?, ?, 1, ?)''',
                    (
                        user['username'],
                        hash_password(user['password']),
                        user['role'],
                        user['full_name'],
                        datetime.now().isoformat()
                    )
                )

        # Ensure an active model configuration exists.
        c.execute("SELECT id FROM model_registry WHERE status = 'active' ORDER BY id DESC LIMIT 1")
        if not c.fetchone():
            c.execute(
                '''INSERT INTO model_registry (version, status, trained_at, notes, approval_threshold, reject_threshold, training_samples)
                   VALUES (?, 'active', ?, ?, ?, ?, ?)''',
                (
                    MODEL_DEFAULTS['version'],
                    datetime.now().isoformat(),
                    'Bootstrap model configuration',
                    MODEL_DEFAULTS['approval_threshold'],
                    MODEL_DEFAULTS['reject_threshold'],
                    0
                )
            )
        conn.commit()
        conn.close()
    except Exception as e:
        print('Failed to initialize database:', e)

def get_model_config():
    """Return currently active model configuration."""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("SELECT * FROM model_registry WHERE status = 'active' ORDER BY id DESC LIMIT 1")
        row = c.fetchone()
        conn.close()
        if not row:
            return MODEL_DEFAULTS.copy()
        return {
            'version': row['version'] or MODEL_DEFAULTS['version'],
            'approval_threshold': float(row['approval_threshold'] if row['approval_threshold'] is not None else MODEL_DEFAULTS['approval_threshold']),
            'reject_threshold': float(row['reject_threshold'] if row['reject_threshold'] is not None else MODEL_DEFAULTS['reject_threshold']),
            'status': row['status'] or 'active'
        }
    except Exception:
        return MODEL_DEFAULTS.copy()

def authenticate_user(username, password):
    """Authenticate user against user_accounts table."""
    if not username or not password:
        return None
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute('''
            SELECT id, username, password_hash, role, full_name, is_active
            FROM user_accounts
            WHERE username = ?
        ''', (username,))
        user = c.fetchone()
        if not user:
            conn.close()
            return None
        if not int(user['is_active']):
            conn.close()
            return None
        if user['password_hash'] != hash_password(password):
            conn.close()
            return None
        if user['role'] not in ROLES:
            conn.close()
            return None
        c.execute('UPDATE user_accounts SET last_login = ? WHERE id = ?', (datetime.now().isoformat(), user['id']))
        conn.commit()
        conn.close()
        return {
            'id': user['id'],
            'username': user['username'],
            'role': user['role'] if user['role'] in ROLES else 'applicant',
            'full_name': user['full_name'] or user['username']
        }
    except Exception:
        return None

def verify_admin_password(password):
    """Validate a password against any active admin account."""
    if not password:
        return False
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute(
            "SELECT id FROM user_accounts WHERE role = 'admin' AND is_active = 1 AND password_hash = ? LIMIT 1",
            (hash_password(password),)
        )
        row = c.fetchone()
        conn.close()
        return row is not None
    except Exception:
        return False

def register_user(username, password, full_name):
    """Register a new user account."""
    if not username or not password or not full_name:
        return {'success': False, 'message': 'Missing required fields'}
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        
        # Check if username exists
        c.execute('SELECT id FROM user_accounts WHERE username = ?', (username,))
        if c.fetchone():
            conn.close()
            return {'success': False, 'message': 'Username already exists'}
        
        # Insert user as pending approval by default
        c.execute('''
            INSERT INTO user_accounts (username, password_hash, role, full_name, is_active, created_at)
            VALUES (?, ?, ?, ?, 0, ?)
        ''', (username, hash_password(password), 'applicant', full_name, datetime.now().isoformat()))
        
        user_id = c.lastrowid
        
        # Create empty user profile
        c.execute('''
            INSERT INTO user_profiles (user_id, created_at, updated_at)
            VALUES (?, ?, ?)
        ''', (user_id, datetime.now().isoformat(), datetime.now().isoformat()))
        
        conn.commit()
        conn.close()
        return {'success': True, 'message': 'Registration successful', 'user_id': user_id}
    except Exception as e:
        return {'success': False, 'message': f'Registration failed: {str(e)}'}

def get_user_id_from_session(req):
    """Get user_id from active session."""
    session_data = get_active_session(req)
    if not session_data:
        return None
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute('SELECT id FROM user_accounts WHERE username = ?', (session_data['username'],))
        row = c.fetchone()
        conn.close()
        return row[0] if row else None
    except Exception:
        return None

def save_user_document(user_id, document_type, file_path):
    """Record a user document upload."""
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute('''
            INSERT INTO user_documents (user_id, document_type, file_path, uploaded_at)
            VALUES (?, ?, ?, ?)
        ''', (user_id, document_type, file_path, datetime.now().isoformat()))
        conn.commit()
        document_id = c.lastrowid
        conn.close()
        return {'success': True, 'document_id': document_id}
    except Exception as e:
        return {'success': False, 'message': str(e)}

# initialize DB
init_db()

# Create reference folders for each document type
for doc_type in DOCUMENT_TYPES:
    folder_path = os.path.join(REFERENCE_BASE_FOLDER, doc_type)
    os.makedirs(folder_path, exist_ok=True)

# Set Tesseract path conditionally based on the operating system
import sys
if sys.platform == 'win32':
    pytesseract.pytesseract.tesseract_cmd = r'C:\Program Files\Tesseract-OCR\tesseract.exe'

# ============= GLOBAL REFERENCE STORAGE =============
reference_data = {
    'drivers_license': {'image': None, 'features': None},
    'national_id': {'image': None, 'features': None},
    'passport': {'image': None, 'features': None}
}

# ============= LOGIN & AUTHENTICATION ROUTES =============
@app.route('/')
def index():
    """Serve the main HTML page (protected)"""
    if not verify_session(request):
        return redirect('/login')
    session_data = get_active_session(request) or {}
    return render_template(
        'index.html',
        current_role=session_data.get('role', ''),
        current_full_name=session_data.get('full_name', '')
    )

@app.route('/login', methods=['GET', 'POST'])
def login():
    """Handle user login"""
    if request.method == 'GET':
        # Check if already logged in
        if verify_session(request):
            return redirect('/')
        return render_template('login.html')
    
    elif request.method == 'POST':
        data = request.get_json() or {}
        username = data.get('username', '').strip()
        password = data.get('password', '')
        user = authenticate_user(username, password)

        if user:
            # Create session
            session_token = generate_session_token(user['username'], user['role'], user['full_name'])
            redirect_target = '/dashboard' if user['role'] == 'admin' else '/user-dashboard'
            
            response = jsonify({
                'success': True,
                'message': 'Login successful',
                'redirect': redirect_target,
                'role': user['role'],
                'full_name': user['full_name']
            })
            
            # Set secure cookie
            response.set_cookie(
                'session_token',
                session_token,
                httponly=True,
                secure=False,  # Set to True in production with HTTPS
                samesite='Strict',
                max_age=3600  # 1 hour
            )
            
            # Cleanup expired sessions
            cleanup_expired_sessions()
            
            return response
        else:
            try:
                conn = sqlite3.connect(DB_PATH)
                conn.row_factory = sqlite3.Row
                c = conn.cursor()
                c.execute('SELECT is_active FROM user_accounts WHERE username = ?', (username,))
                row = c.fetchone()
                conn.close()
                if row and int(row['is_active']) == 0:
                    return jsonify({
                        'success': False,
                        'message': 'Your account is pending admin approval. Please wait for activation.'
                    }), 403
            except Exception:
                pass

            return jsonify({
                'success': False,
                'message': 'Invalid credentials'
            }), 401

@app.route('/logout', methods=['POST'])
def logout():
    """Handle user logout"""
    token = request.cookies.get('session_token')
    if token and token in active_sessions:
        del active_sessions[token]

    # If client expects JSON, return JSON. If it's a browser form, redirect to login.
    is_json = request.is_json or request.headers.get('Accept', '').find('application/json') != -1
    if is_json:
        response = jsonify({'success': True, 'message': 'Logged out successfully'})
    else:
        response = redirect('/login')

    response.set_cookie('session_token', '', expires=0)
    return response

@app.route('/register', methods=['GET', 'POST'])
def register():
    """Handle user registration"""
    if request.method == 'GET':
        # Check if already logged in
        if verify_session(request):
            return redirect('/')
        return render_template('register.html')
    
    elif request.method == 'POST':
        data = request.get_json() or {}
        username = data.get('username', '').strip()
        password = data.get('password', '').strip()
        full_name = data.get('full_name', '').strip()
        
        # Validation
        if not username or len(username) < 3:
            return jsonify({'success': False, 'message': 'Username must be at least 3 characters'}), 400
        
        if not password or len(password) < 6:
            return jsonify({'success': False, 'message': 'Password must be at least 6 characters'}), 400
        
        if not full_name or len(full_name) < 2:
            return jsonify({'success': False, 'message': 'Full name required'}), 400

        if not re.fullmatch(r"[A-Za-z]+(?:[\s\-'][A-Za-z]+)*", full_name):
            return jsonify({'success': False, 'message': 'Full name must contain letters only'}), 400
        
        # Register user
        result = register_user(username, password, full_name)
        
        if result['success']:
            return jsonify({
                'success': True,
                'message': 'Registration submitted. Your account is waiting for admin approval.',
                'redirect': '/login'
            }), 201
        else:
            return jsonify({'success': False, 'message': result['message']}), 400

@app.route('/check-auth', methods=['GET'])
def check_auth():
    """Check if user is authenticated"""
    session_data = get_active_session(request)
    if session_data:
        return jsonify({
            'authenticated': True,
            'username': session_data.get('username'),
            'role': session_data.get('role'),
            'full_name': session_data.get('full_name')
        })
    return jsonify({'authenticated': False}), 401

@app.route('/session-info', methods=['GET'])
def session_info():
    """Get session information"""
    session_data = get_active_session(request)
    if session_data:
        time_remaining = SESSION_TIMEOUT - (datetime.now() - session_data['last_activity'])
        return jsonify({
            'authenticated': True,
            'username': session_data['username'],
            'role': session_data.get('role'),
            'full_name': session_data.get('full_name'),
            'login_time': session_data['login_time'].isoformat(),
            'session_timeout_minutes': int(time_remaining.total_seconds() / 60)
        })
    
    return jsonify({'authenticated': False}), 401

# ============= FILE PROCESSING FUNCTIONS =============
def load_reference_license(document_type='drivers_license'):
    """Load reference for specific document type"""
    try:
        folder_path = get_reference_folder(document_type)
        reference_files = [f for f in os.listdir(folder_path) 
                          if f.lower().endswith(('.png', '.jpg', '.jpeg', '.pdf'))]
        
        if not reference_files:
            print(f"No reference found for {document_type}")
            return False
        
        # Use the first reference file found
        reference_path = os.path.join(folder_path, reference_files[0])
        print(f"Loading {document_type} reference from: {reference_path}")
        
        # Load reference image
        if reference_path.lower().endswith('.pdf'):
            images = convert_from_path(reference_path)
            if images:
                temp_img_path = os.path.join(tempfile.gettempdir(), 'temp_reference.png')
                images[0].save(temp_img_path, 'PNG')
                reference_data[document_type]['image'] = cv2.imread(temp_img_path)
                # Clean up temp file
                if os.path.exists(temp_img_path):
                    os.remove(temp_img_path)
            else:
                print(f"Could not process PDF reference for {document_type}")
                return False
        else:
            reference_data[document_type]['image'] = cv2.imread(reference_path)
        
        if reference_data[document_type]['image'] is None:
            print(f"Could not read {document_type} reference image")
            return False
        
        # Extract features from reference
        reference_text = extract_text_from_file(reference_path)
        reference_data[document_type]['features'] = extract_detailed_features(
            reference_data[document_type]['image'], 
            reference_text,
            document_type
        )
        
        print(f"{document_type} reference loaded successfully. Size: {reference_data[document_type]['image'].shape}")
        return True
        
    except Exception as e:
        print(f"Error loading {document_type} reference: {e}")
        return False

def detect_document_type_from_text(text):
    """
    Detect the actual document type from extracted text.
    Returns a dictionary with detected type and confidence scores for each type.
    """
    if not text:
        print("\n=== DOCUMENT TYPE DETECTION: NO TEXT ===\n")
        return {'detected_type': None, 'scores': {}, 'confidence': 0}
    
    text_lower = text.lower()
    
    # Print extracted text for debugging
    print(f"\n=== EXTRACTED TEXT (first 500 chars) ===")
    print(text_lower[:500])
    print("==========================================\n")
    
    # Define EXCLUSIVE keywords that are unique to each document type
    # These keywords should NOT appear on other document types
    # IMPORTANT: Keywords must be UNIQUE to each document type to avoid false matches
    document_signatures = {
        'drivers_license': {
            # Keywords that ONLY appear on driver's licenses - MADE MORE SPECIFIC
            # Avoid single words like 'license' that might appear elsewhere
            'exclusive': ["driver's license", 'drivers license', 'driving license',
                         'land transportation', 'lto', 'motor vehicle', 'non-professional', 
                         'professional driver', 'operate motor', 'restriction code',
                         'dl no', 'license no', 'agency code', 'dl code',
                         'licensing', 'motor vehicles', 'transportation office',
                         'license number', 'vehicle type', 'non-pro', 'non pro',
                         'land transportation office', 'transportation', 'licensee'],
            'strong': ['endorsement', 'weight', 'height', 'restrictions', 'blood type'],
            'moderate': ['permit'],
            # These terms DISQUALIFY this document type (subtract points)
            'negative': ['philsys', 'psa', 'statistics authority', 'passport', 'pasaporte', 
                        'foreign affairs', 'dfa', 'crn', 'pcn', 'philid', 'philippine identification',
                        'national id', 'national identification'],
        },
        'national_id': {
            # Keywords that ONLY appear on Philippine National ID (PhilSys)
            # Expanded list with more terms found on actual PhilSys cards
            'exclusive': ['philsys', 'philippine statistics authority', 'psa', 'pcn', 
                         'common reference number', 'crn', 'philid', 'phil id',
                         'philippine identification system', 'national id', 'philsys id',
                         'philippine identification', 'statistics authority', 'phl id',
                         'national identification', 'phil-id', 'phil sys', 'philsys card',
                         'identification card', 'psa id', 'republika ng pilipinas',
                         'philippine statistics', 'philippine identification card',
                         # Additional terms commonly found on National IDs
                         'i.d. card', 'i.d card', 'id no', 'identification no',
                         'registered at', 'civil registry', 'civil registrar',
                         'date of registration', 'date registered', 'front/back',
                         'psn', 'tin', 'sss', 'gsis', 'prc id', 'voter', 'umid',
                         'multipurpose', 'unified', 'social security'],
            'strong': ['identification system', 'card number', 'identification no',
                      'lugar ng kapanganakan', 'petsa ng kapanganakan', 'kasarian'],
            'moderate': ['citizen', 'filipino', 'residence', 'address', 'munisipyo',
                        'probinsya', 'barangay', 'registered', 'civil'],
            # These terms DISQUALIFY this document type
            'negative': ['driver', 'lto', 'passport', 'pasaporte',
                        'foreign affairs', 'dfa', 'transportation', 'motor vehicle'],
        },
        'passport': {
            # Keywords that ONLY appear on passports - must be PASSPORT-SPECIFIC ONLY
            # REMOVED common Filipino words that appear on all PH government IDs
            'exclusive': ['passport', 'pasaporte', 'department of foreign affairs', 'dfa',
                         'machine readable', 'mrz', 'type/uri', 'code/kowd', 
                         'nationality/nasyonalidad', 'p<phl', 'pasaporteng', 'travel document',
                         'foreign affairs', 'passport no', 'passeport', 'reisepass',
                         'issuing country', 'country of birth', 'philippines passport',
                         # Passport-specific OCR variations
                         'passpo', 'passp', 'pasaport', 'passaport', 'passpor',
                         'surname/apelyido', 'type p', 'type/tipo',
                         'date of expiry', 'petsa ng pagkawalang-bisa', 'pagkawalang',
                         'country code', 'kowd ng bansa', 'phlp',
                         'secretary', 'kalihim', 'issuing authority'],
            'strong': ['visa', 'immigration', 'place of issue'],
            'moderate': ['given names', 'surname', 'nationality', 'nasyonalidad'],
            # These terms DISQUALIFY this document type
            'negative': ['driver', 'lto', 'philsys', 'psa',
                        'statistics authority', 'motor vehicle', 'transportation',
                        'national id', 'national identification', 'philippine identification',
                        'sss', 'gsis', 'umid', 'voter', 'philhealth'],
        }
    }
    
    # Calculate scores for each document type
    scores = {}
    for doc_type, keywords in document_signatures.items():
        score = 0
        matched_keywords = []
        negative_matches = []
        
        # Exclusive keywords (5 points each) - these definitively identify the document
        for kw in keywords.get('exclusive', []):
            if kw in text_lower:
                score += 5
                matched_keywords.append(f"[EXCLUSIVE] {kw}")
        
        # Strong keywords (2 points each)
        for kw in keywords.get('strong', []):
            if kw in text_lower:
                score += 2
                matched_keywords.append(f"[STRONG] {kw}")
        
        # Moderate keywords (1 point each)
        for kw in keywords.get('moderate', []):
            if kw in text_lower:
                score += 1
                matched_keywords.append(f"[MOD] {kw}")
        
        # Negative keywords (subtract 8 points each) - these DISQUALIFY the document type
        for kw in keywords.get('negative', []):
            if kw in text_lower:
                score -= 8
                negative_matches.append(f"[NEGATIVE] {kw}")
        
        # Don't let score go below 0
        score = max(0, score)
        
        scores[doc_type] = {'score': score, 'matched': matched_keywords, 'negative': negative_matches}
    
    # Debug output
    print(f"\n=== DOCUMENT TYPE DETECTION ===")
    for doc_type, data in scores.items():
        print(f"{doc_type}: score={data['score']}")
        if data['matched']:
            print(f"  Matched: {data['matched']}")
        if data.get('negative'):
            print(f"  Negative: {data['negative']}")
    
    # Determine the detected type
    max_score = 0
    detected_type = None
    for doc_type, data in scores.items():
        if data['score'] > max_score:
            max_score = data['score']
            detected_type = doc_type
    
    # Calculate confidence (difference between top score and second highest)
    sorted_scores = sorted([s['score'] for s in scores.values()], reverse=True)
    if len(sorted_scores) >= 2 and sorted_scores[0] > 0:
        confidence = ((sorted_scores[0] - sorted_scores[1]) / sorted_scores[0]) * 100
    elif sorted_scores[0] > 0:
        confidence = 100
    else:
        confidence = 0
    
    return {
        'detected_type': detected_type,
        'scores': {k: v['score'] for k, v in scores.items()},
        'matched_keywords': {k: v['matched'] for k, v in scores.items()},
        'confidence': confidence,
        'max_score': max_score
    }

def validate_document_type_match(text, expected_type):
    """
    Validate if the uploaded document matches the expected document type.
    Returns (is_valid, detected_type, message)
    """
    detection = detect_document_type_from_text(text)
    detected_type = detection['detected_type']
    scores = detection['scores']
    max_score = detection['max_score']
    
    expected_score = scores.get(expected_type, 0)
    detected_score = scores.get(detected_type, 0) if detected_type else 0
    
    # Document type names for messages
    type_names = {
        'drivers_license': "Driver's License",
        'national_id': "National ID",
        'passport': "Passport"
    }
    
    expected_name = type_names.get(expected_type, expected_type)
    detected_name = type_names.get(detected_type, detected_type) if detected_type else "Unknown"
    
    # Debug output
    print(f"\n=== DOCUMENT TYPE VALIDATION ===")
    print(f"Expected: {expected_type} (score: {expected_score})")
    print(f"Detected: {detected_type} (score: {detected_score})")
    print(f"Max score: {max_score}")
    print("================================\n")
    
    # If no text was detected at all
    if max_score == 0:
        # If no keywords matched at all, it could be poor OCR - be more lenient
        print(f"\n=== NO KEYWORDS MATCHED - Checking if text exists ===")
        if text and len(text.strip()) > 50:
            # There is text but no keywords matched - might be OCR issues
            # Allow it to proceed with a warning
            return (True, expected_type, 
                    f"Document text detected but type keywords unclear. Proceeding with {type_names.get(expected_type, expected_type)} verification.")
        return (False, None, "Could not identify document type. Please upload a clearer image.")
    
    # STRICT CHECK: If detected type doesn't match expected type, REJECT
    # Only allow if the expected type has a SIGNIFICANTLY higher score than detected
    if detected_type != expected_type:
        # Only accept if expected_score is at least 60% of detected_score AND expected_score >= 3
        # This handles edge cases where OCR might miss some keywords
        if expected_score >= 3 and expected_score >= (detected_score * 0.6):
            return (True, expected_type, f"Document appears to be a {expected_name}")
        else:
            return (False, detected_type, 
                    f"Document mismatch: You uploaded what appears to be a {detected_name}, "
                    f"but you selected {expected_name}. Please upload the correct document type.")
    
    # Detected type matches expected type - verify minimum confidence
    # Lowered threshold from 5 to 3 for better tolerance
    if expected_score < 3:
        return (False, detected_type, 
                f"Could not confirm this is a valid {expected_name}. Please upload a clearer image.")
    
    return (True, expected_type, f"Document verified as {expected_name}")

def extract_text_from_file(file_path):
    """Extract text from image or PDF file with enhanced OCR processing"""
    try:
        if file_path.lower().endswith('.pdf'):
            images = convert_from_path(file_path)
            if images:
                text = pytesseract.image_to_string(images[0])
                print(f"\n=== PDF OCR TEXT ===\n{text[:500]}...\n==================\n")
                return text
        else:
            image = Image.open(file_path)
            
            # Try multiple OCR processing methods for better results
            extracted_texts = []
            
            # Method 1: Enhanced contrast
            enhancer = ImageEnhance.Contrast(image)
            enhanced = enhancer.enhance(1.5)
            text1 = pytesseract.image_to_string(enhanced)
            extracted_texts.append(text1)
            
            # Method 2: Grayscale with sharpening
            gray_image = image.convert('L')
            sharpener = ImageEnhance.Sharpness(gray_image)
            sharp = sharpener.enhance(2.0)
            text2 = pytesseract.image_to_string(sharp)
            extracted_texts.append(text2)
            
            # Method 3: Original image with different PSM modes
            # PSM 3 = Fully automatic page segmentation
            # PSM 6 = Assume a single uniform block of text
            try:
                text3 = pytesseract.image_to_string(image, config='--psm 6')
                extracted_texts.append(text3)
            except:
                pass
            
            # Combine all extracted texts (remove duplicates by using the longest one or combining unique parts)
            combined_text = ' '.join(set(' '.join(extracted_texts).split()))
            
            print(f"\n=== IMAGE OCR TEXT ===\n{combined_text[:800]}...\n=====================\n")
            return combined_text
    except Exception as e:
        print(f"Text extraction error: {e}")
        return ""

def extract_detailed_features(img, text, document_type='drivers_license'):
    """Extract detailed features from image and text"""
    features = {}
    
    try:
        # 1. Image features (common for all document types)
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        
        # Basic image properties
        features['height'] = int(img.shape[0])
        features['width'] = int(img.shape[1])
        features['aspect_ratio'] = float(img.shape[1] / img.shape[0])
        
        # Color features
        features['color_mean_b'] = float(np.mean(img[:,:,0]))
        features['color_mean_g'] = float(np.mean(img[:,:,1]))
        features['color_mean_r'] = float(np.mean(img[:,:,2]))
        
        # Texture features
        laplacian_var = cv2.Laplacian(gray, cv2.CV_64F).var()
        features['sharpness'] = float(laplacian_var) if not np.isnan(laplacian_var) else 0.0
        features['contrast'] = float(gray.std()) if not np.isnan(gray.std()) else 0.0
        features['brightness'] = float(np.mean(gray)) if not np.isnan(np.mean(gray)) else 0.0
        
        # 2. Text features
        if text:
            text_lower = text.lower()
            features['text_length'] = int(len(text))
            features['text'] = str(text)
            
            # Document type specific patterns
            if document_type == 'drivers_license':
                patterns = {
                    'license_number': r'\b[A-Z]{1,2}\d{6,9}\b',
                    'dates': r'\b\d{1,2}[-/]\d{1,2}[-/]\d{4}\b',
                    'zip_code': r'\b\d{5}(?:-\d{4})?\b',
                }
            elif document_type == 'national_id':
                patterns = {
                    'id_number': r'\b\d{8,12}\b',
                    'dates': r'\b\d{1,2}[-/]\d{1,2}[-/]\d{4}\b',
                    'national_id_keywords': r'\b(national|id|identification|republic|philippines|ph)\b',
                }
            elif document_type == 'passport':
                patterns = {
                    'passport_number': r'\b[A-Z]{1,2}\d{6,9}\b',
                    'dates': r'\b\d{1,2}[-/]\d{1,2}[-/]\d{4}\b',
                    'passport_keywords': r'\b(passport|p[.]?no|republic|philippines|ph|diplomatic|official|ordinary|type)\b',
                    'mrz_code': r'\b[A-Z0-9<]{9,50}\b',  # Machine Readable Zone patterns
                }
            else:
                patterns = {}
            
            for name, pattern in patterns.items():
                matches = re.findall(pattern, text, re.IGNORECASE)
                features[f'{name}_count'] = int(len(matches))
        
        return features
        
    except Exception as e:
        print(f"Feature extraction error: {e}")
        return features

def compare_with_reference(img, text, document_type='drivers_license'):
    """Compare uploaded document with reference document"""
    comparison_results = {
        'similarity_score': 0.0,
        'differences': [],
        'details': {}
    }
    
    try:
        ref_data = reference_data[document_type]
        if ref_data['image'] is None or ref_data['features'] is None:
            if not load_reference_license(document_type):
                comparison_results['differences'].append(f'No {document_type} reference available for comparison')
                return comparison_results
        
        # Extract features from uploaded image
        uploaded_features = extract_detailed_features(img, text, document_type)
        
        # 1. Compare image properties
        img_similarities = []
        ref_features = ref_data['features']
        
        # Size comparison (more lenient - 50% difference threshold)
        if 'height' in ref_features and 'height' in uploaded_features:
            height_diff = abs(ref_features['height'] - uploaded_features['height']) / max(ref_features['height'], 1) * 100
            if height_diff > 50:  # Increased from 30%
                comparison_results['differences'].append(f'Height difference: {height_diff:.1f}%')
            img_similarities.append(max(0.0, 100.0 - min(height_diff, 100.0)))
        
        # Aspect ratio comparison (more lenient - 30% difference threshold)
        if 'aspect_ratio' in ref_features and 'aspect_ratio' in uploaded_features:
            ref_ratio = ref_features['aspect_ratio']
            upload_ratio = uploaded_features['aspect_ratio']
            ratio_diff = abs(ref_ratio - upload_ratio) / max(ref_ratio, 0.01) * 100
            if ratio_diff > 30:  # Increased from 20%
                comparison_results['differences'].append(f'Aspect ratio difference: {ratio_diff:.1f}%')
            img_similarities.append(max(0.0, 100.0 - min(ratio_diff, 100.0)))
        
        # Color comparison (more lenient - 60% difference threshold)
        color_similarity = 50.0  # Default
        if all(k in ref_features for k in ['color_mean_b', 'color_mean_g', 'color_mean_r']) and \
           all(k in uploaded_features for k in ['color_mean_b', 'color_mean_g', 'color_mean_r']):
            
            ref_color = np.array([ref_features['color_mean_b'], 
                                  ref_features['color_mean_g'], 
                                  ref_features['color_mean_r']])
            upload_color = np.array([uploaded_features['color_mean_b'], 
                                     uploaded_features['color_mean_g'], 
                                     uploaded_features['color_mean_r']])
            
            color_diff = np.mean(np.abs(ref_color - upload_color))
            color_similarity = max(0.0, 100.0 - color_diff)
            if color_diff > 60:  # Increased from 40%
                comparison_results['differences'].append('Significant color difference detected')
        
        img_similarities.append(color_similarity)
        
        # Sharpness comparison (more lenient)
        if 'sharpness' in ref_features and 'sharpness' in uploaded_features:
            ref_sharp = ref_features['sharpness']
            upload_sharp = uploaded_features['sharpness']
            if ref_sharp > 0 and upload_sharp > 0:
                sharpness_diff = abs(ref_sharp - upload_sharp) / max(ref_sharp, upload_sharp) * 100
                if sharpness_diff > 80:  # Increased from 60%
                    comparison_results['differences'].append(f'Sharpness difference: {sharpness_diff:.1f}%')
                img_similarities.append(max(0.0, 100.0 - min(sharpness_diff, 100.0)))
        
        # 2. Compare text
        text_similarities = []
        
        if 'text' in ref_features and 'text' in uploaded_features:
            ref_text = ref_features['text'].lower()
            upload_text = uploaded_features['text'].lower()
            
            # Document type specific keywords (expanded lists)
            if document_type == 'drivers_license':
                common_keywords = ['driver', 'license', 'licence', 'state', 'expires', 'expiration',
                                  'birth', 'dob', 'date of birth', 'issued', 'height', 'weight', 'driving',
                                  'class', 'restriction', 'endorsement', 'address']
            elif document_type == 'national_id':
                common_keywords = ['national', 'id', 'identification', 'republic', 'philippines',
                                  'birth', 'dob', 'date of birth', 'address', 'sex', 'gender',
                                  'civil status', 'blood type', 'signature', 'citizen', 'phil']
            elif document_type == 'passport':
                common_keywords = ['passport', 'republic', 'philippines', 'ph', 'type', 
                                  'country code', 'surname', 'given names', 'nationality',
                                  'date of birth', 'sex', 'place of birth', 'date of issue',
                                  'authority', 'date of expiry', 'mrz', 'machine readable', 'travel']
            else:
                common_keywords = []
            
            # More flexible keyword matching
            ref_keywords = []
            upload_keywords = []
            
            for kw in common_keywords:
                if kw in ref_text:
                    ref_keywords.append(kw)
                # Check for partial matches in uploaded text
                for word in upload_text.split():
                    if kw in word or word in kw:
                        if len(kw) > 3 and len(word) > 3:
                            upload_keywords.append(kw)
                            break
            
            if ref_keywords:
                # Calculate intersection (allowing partial matches)
                intersection = set(ref_keywords) & set(upload_keywords)
                keyword_similarity = len(intersection) / max(len(ref_keywords), 1) * 100
                text_similarities.append(keyword_similarity)
                
                if keyword_similarity < 40:  # Reduced from 60%
                    comparison_results['differences'].append(f'Missing important {document_type} keywords')
            
            # Text length comparison (more lenient)
            if 'text_length' in ref_features and 'text_length' in uploaded_features:
                ref_len = ref_features['text_length']
                upload_len = uploaded_features['text_length']
                if ref_len > 0 and upload_len > 0:
                    length_similarity = min(ref_len, upload_len) / max(ref_len, upload_len) * 100
                    text_similarities.append(length_similarity)
                    if length_similarity < 40:  # Reduced from 50%
                        comparison_results['differences'].append(f'Text length significantly different')
            
            # Add simple text overlap score
            if len(ref_text) > 10 and len(upload_text) > 10:
                # Calculate word overlap
                ref_words = set(ref_text.split())
                upload_words = set(upload_text.split())
                overlap = len(ref_words & upload_words) / max(len(ref_words), 1) * 100
                text_similarities.append(min(overlap, 100.0))
        
        # 3. Calculate overall similarity score with better weighting
        if img_similarities:
            # Give more weight to color and aspect ratio
            weights = [0.2, 0.3, 0.3, 0.2]  # height, aspect, color, sharpness
            if len(weights) == len(img_similarities):
                img_score = float(np.average(img_similarities, weights=weights[:len(img_similarities)]))
            else:
                img_score = float(np.mean(img_similarities))
        else:
            img_score = 50.0
        
        if text_similarities:
            text_score = float(np.mean(text_similarities))
        else:
            text_score = 50.0
        
        # Weighted average (50% image, 50% text) - more balanced
        overall_similarity = img_score * 0.5 + text_score * 0.5
        comparison_results['similarity_score'] = float(overall_similarity)
        
        # Add detailed comparison
        comparison_results['details'] = {
            'image_similarity': f"{img_score:.1f}%",
            'text_similarity': f"{text_score:.1f}%" if text_similarities else "N/A",
            'overall_similarity': f"{overall_similarity:.1f}%",
            'document_type': document_type.replace('_', ' ').title()
        }
        
        return comparison_results
        
    except Exception as e:
        print(f"Comparison error for {document_type}: {e}")
        comparison_results['differences'].append(f'Comparison error: {str(e)}')
        return comparison_results

def analyze_with_comparison(file_path, document_type='drivers_license'):
    """Main analysis function using reference comparison"""
    
    result = {
        'is_authentic': False,
        'confidence': 0.0,
        'similarity_score': 0.0,
        'issues': [],
        'analysis': {},
        'method': 'reference_comparison',
        'comparison_details': {},
        'has_reference': False,
        'document_type': document_type
    }
    
    try:
        # Load image
        if file_path.lower().endswith('.pdf'):
            images = convert_from_path(file_path)
            if images:
                temp_img_path = os.path.join(tempfile.gettempdir(), 'temp_image.png')
                images[0].save(temp_img_path, 'PNG')
                img = cv2.imread(temp_img_path)
                # Clean up temp file
                if os.path.exists(temp_img_path):
                    os.remove(temp_img_path)
            else:
                result['issues'].append('Could not process PDF file')
                return result
        else:
            img = cv2.imread(file_path)
        
        if img is None:
            result['issues'].append('Could not read image file')
            return result
        
        # Extract text for analysis
        text = extract_text_from_file(file_path)
        text_lower = text.lower() if text else ""
        
        # ====== DOCUMENT TYPE VALIDATION ======
        # Check if the uploaded document matches the expected document type
        type_valid, detected_type, type_message = validate_document_type_match(text, document_type)
        
        # Document type names for display
        type_names = {
            'drivers_license': "Driver's License",
            'national_id': "National ID",
            'passport': "Passport"
        }
        doc_name = type_names.get(document_type, document_type)
        detected_name = type_names.get(detected_type, "Unknown") if detected_type else "Unknown"
        
        # If document type doesn't match, reject immediately
        if not type_valid:
            result['is_authentic'] = False
            result['confidence'] = 0.0
            result['issues'].append(type_message)
            result['analysis'] = {
                'document_type_check': 'FAILED',
                'expected_type': doc_name,
                'detected_type': detected_name,
                'message': type_message
            }
            
            # Debug output
            print(f"\n=== DOCUMENT TYPE MISMATCH ===")
            print(f"Expected: {document_type}")
            print(f"Detected: {detected_type}")
            print(f"Message: {type_message}")
            print("==============================\n")
            
            return result
        
        # ====== CONTINUE WITH NORMAL VERIFICATION ======
        # Document type specific validation with more flexible keywords
        if document_type == 'drivers_license':
            keywords = ['driver', 'license', 'licence', 'permit', 'dl', 'driving', 'licensee', 'lic', 'drivers', 'identification']
        elif document_type == 'national_id':
            keywords = ['national', 'id', 'identification', 'republic', 'philippines', 'ph', 'filipino', 'citizen', 'card']
        elif document_type == 'passport':
            keywords = ['passport', 'republic', 'philippines', 'ph', 'type', 'country', 'code', 'travel', 'document', 'book']
        else:
            keywords = []
        
        # More flexible keyword matching (partial matches)
        keyword_count = 0
        for kw in keywords:
            if kw in text_lower:
                keyword_count += 1
            # Also check for similar words
            elif len(kw) > 4:
                # Check for partial matches (OCR errors might miss characters)
                words_in_text = text_lower.split()
                for word in words_in_text:
                    if len(word) > 3 and difflib.SequenceMatcher(None, kw, word).ratio() > 0.7:
                        keyword_count += 0.5  # Partial match
        
        # Check if we have a reference document
        result['has_reference'] = reference_data[document_type]['image'] is not None
        
        if result['has_reference']:
            # 1. Compare with reference document
            comparison = compare_with_reference(img, text, document_type)
            result['similarity_score'] = float(comparison['similarity_score'])
            result['comparison_details'] = comparison['details']
            
            # Filter out minor differences for the issues list
            significant_issues = []
            for diff in comparison['differences']:
                # Only include significant issues
                if 'difference' in diff.lower():
                    # Extract percentage from difference message
                    import re
                    perc_match = re.search(r'(\d+\.?\d*)%', diff)
                    if perc_match:
                        perc = float(perc_match.group(1))
                        if perc > 50:  # Only show differences > 50%
                            significant_issues.append(diff)
                    else:
                        significant_issues.append(diff)
                elif 'significant' in diff.lower() or 'missing' in diff.lower():
                    significant_issues.append(diff)
            
            result['issues'].extend(significant_issues)
            
            # 2. Run anomaly detection as secondary check
            features = extract_detailed_features(img, text, document_type)
            anomaly_score = 0.0
            
            # Check image quality (more lenient)
            if 'sharpness' in features:
                if features['sharpness'] < 20:
                    anomaly_score += 30.0
                    result['issues'].append('Very low image sharpness')
                elif features['sharpness'] < 40:
                    anomaly_score += 15.0
                    result['issues'].append('Low image sharpness')
                elif features['sharpness'] > 200:
                    anomaly_score += 10.0  # Too sharp might indicate digital manipulation
            
            if 'contrast' in features:
                if features['contrast'] < 15:
                    anomaly_score += 20.0
                    result['issues'].append('Very low contrast')
                elif features['contrast'] < 25:
                    anomaly_score += 8.0
                    # Don't add to issues for minor contrast problems
            
            # 3. Calculate overall confidence with more balanced weights
            similarity_confidence = float(comparison['similarity_score'])
            anomaly_adjustment = max(0.0, 100.0 - anomaly_score)
            
            # Calculate keyword presence score (0-100)
            keyword_score = min(100.0, (keyword_count / max(len(keywords) * 0.5, 1)) * 100)
            
            # Combined confidence with more weight on similarity
            # 50% similarity, 30% keywords, 20% anomaly adjustment
            if keyword_count >= 1:  # Reduced from 2
                final_confidence = (similarity_confidence * 0.5 + 
                                  keyword_score * 0.3 + 
                                  anomaly_adjustment * 0.2)
            else:
                # If no keywords at all, be more strict
                final_confidence = similarity_confidence * 0.3 + anomaly_adjustment * 0.2
            
            result['confidence'] = float(final_confidence)
            
            # 4. Determine authenticity with more reasonable thresholds
            if keyword_count < 0.5:  # Almost no keywords
                result['is_authentic'] = False
                result['issues'].append(f'Document lacks key {doc_name} identifiers')
            elif similarity_confidence >= 60.0 and final_confidence >= 55.0:
                result['is_authentic'] = True
                if similarity_confidence < 70.0:
                    result['issues'].append(f'Acceptable similarity to {doc_name} reference')
            elif similarity_confidence >= 50.0 and final_confidence >= 50.0 and keyword_count >= 2:
                result['is_authentic'] = True
                result['issues'].append(f'Moderate similarity to {doc_name} reference')
            else:
                result['is_authentic'] = False
            
            # 5. Detailed analysis
            result['analysis'] = {
                'similarity_to_reference': f"{comparison['similarity_score']:.1f}%",
                'image_quality': f"{features.get('sharpness', 0)/10:.1f}/10" if 'sharpness' in features else "N/A",
                'text_analysis': f"{len(text)} characters, {keyword_count:.1f} {doc_name} keywords found",
                'aspect_ratio': f"{features.get('aspect_ratio', 0):.2f}" if 'aspect_ratio' in features else "N/A",
                'document_type': doc_name,
                'keyword_score': f"{keyword_score:.1f}%"
            }
            
        else:
            # No reference available, use basic validation
            if keyword_count < 1:
                result['issues'].append(f'This does not appear to be a {doc_name} document')
                result['confidence'] = 10.0
                return result
            
            features = extract_detailed_features(img, text, document_type)
            
            # Simple quality scoring
            quality_score = 0.0
            
            if 'sharpness' in features:
                if features['sharpness'] > 40:
                    quality_score += 40.0
                elif features['sharpness'] > 20:
                    quality_score += 25.0
                else:
                    quality_score += 10.0
            
            if 'contrast' in features:
                if features['contrast'] > 25:
                    quality_score += 30.0
                elif features['contrast'] > 15:
                    quality_score += 20.0
                else:
                    quality_score += 10.0
            
            if 'aspect_ratio' in features:
                aspect = features['aspect_ratio']
                # Accept wider range for different document formats
                if 1.3 <= aspect <= 2.0:
                    quality_score += 30.0
                elif 1.2 <= aspect <= 2.2:
                    quality_score += 20.0
                else:
                    quality_score += 5.0
            
            # Add text score (more weight on keywords)
            text_score = min(100.0, (keyword_count / max(len(keywords) * 0.3, 1)) * 100)
            quality_score = quality_score * 0.4 + text_score * 0.6
            
            result['confidence'] = min(100.0, quality_score)
            
            # Without reference, be more lenient
            result['is_authentic'] = result['confidence'] >= 50.0 and keyword_count >= 1
            
            result['analysis'] = {
                'image_quality': f"{features.get('sharpness', 0)/10:.1f}/10" if 'sharpness' in features else "N/A",
                'confidence_score': f"{result['confidence']:.1f}%",
                'text_analysis': f"{len(text)} characters, {keyword_count:.1f} {doc_name} keywords found",
                'method': f'Basic Validation (No {doc_name} reference)',
                'keyword_score': f"{text_score:.1f}%"
            }
        
        # Only add "No significant issues" if confidence is good AND no other issues
        if not result['issues'] and result['confidence'] > 60.0:
            result['issues'].append('No significant issues detected')
        elif result['confidence'] > 70.0 and result['is_authentic']:
            # If authentic with high confidence but has minor issues, add positive note
            if len(result['issues']) <= 2:
                result['issues'].append('Minor variations detected, but document appears authentic')
        
    except Exception as e:
        result['issues'].append(f'Analysis error: {str(e)}')
        result['confidence'] = 0.0
        print(f"Analysis error: {e}")
    
    return result

# ============= REPORT GENERATION =============
@app.route('/generate-report', methods=['POST'])
def generate_report():
    """Generate and download PDF report of verification results"""
    if not verify_session(request):
        return jsonify({'success': False, 'message': 'Authentication required'}), 401
    
    try:
        data = request.get_json()
        if not data:
            return jsonify({'success': False, 'message': 'No data provided'}), 400
        
        # Create PDF in memory
        buffer = io.BytesIO()
        
        # Create the PDF document
        doc = SimpleDocTemplate(
            buffer,
            pagesize=letter,
            rightMargin=72,
            leftMargin=72,
            topMargin=72,
            bottomMargin=72
        )
        
        # Container for the 'Flowable' objects
        story = []
        styles = getSampleStyleSheet()
        
        # Add custom styles
        title_style = ParagraphStyle(
            'CustomTitle',
            parent=styles['Heading1'],
            fontSize=24,
            textColor=colors.HexColor('#00f3ff'),
            spaceAfter=30,
            alignment=1  # Center aligned
        )
        
        heading_style = ParagraphStyle(
            'CustomHeading',
            parent=styles['Heading2'],
            fontSize=14,
            textColor=colors.HexColor('#2563eb'),
            spaceAfter=12
        )
        
        normal_style = ParagraphStyle(
            'CustomNormal',
            parent=styles['Normal'],
            fontSize=10,
            spaceAfter=6
        )
        
        # Title
        story.append(Paragraph("CYBER-ID VERIFIER v3.1 - ANALYSIS REPORT", title_style))
        story.append(Spacer(1, 20))
        
        # Report Metadata
        report_id = hashlib.md5(str(datetime.now()).encode()).hexdigest()[:8].upper()
        story.append(Paragraph(f"Report ID: {report_id}", normal_style))
        story.append(Paragraph(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", normal_style))
        story.append(Paragraph(f"System: Multi-Document Verification System", normal_style))
        story.append(Spacer(1, 20))
        
        # Document Information
        story.append(Paragraph("DOCUMENT INFORMATION", heading_style))
        
        doc_type = data.get('document_type', 'Unknown').replace('_', ' ').title()
        doc_info = [
            ["Document Type:", doc_type],
            ["Analysis Date:", datetime.now().strftime('%Y-%m-%d')],
            ["Analysis Time:", datetime.now().strftime('%H:%M:%S')],
            ["Reference Used:", "Yes" if data.get('has_reference') else "No"],
            ["Method:", data.get('method', 'Reference Comparison')]
        ]
        
        doc_table = Table(doc_info, colWidths=[2*inch, 3*inch])
        doc_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#f0f9ff')),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.HexColor('#1e40af')),
            ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, -1), 10),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
            ('TOPPADDING', (0, 0), (-1, -1), 6),
            ('GRID', (0, 0), (-1, -1), 1, colors.HexColor('#e5e7eb'))
        ]))
        story.append(doc_table)
        story.append(Spacer(1, 20))
        
        # Verification Results
        story.append(Paragraph("VERIFICATION RESULTS", heading_style))
        
        is_authentic = data.get('is_authentic', False)
        authenticity = "✓ AUTHENTIC" if is_authentic else "✗ SUSPICIOUS"
        authenticity_color = colors.HexColor('#059669') if is_authentic else colors.HexColor('#dc2626')
        
        confidence = data.get('confidence', 0)
        similarity = data.get('similarity_score', 0)
        
        results_info = [
            ["Status:", authenticity],
            ["Confidence Score:", f"{confidence:.1f}%"],
            ["Similarity Score:", f"{similarity:.1f}%"],
            ["Overall Verdict:", "DOCUMENT AUTHENTIC" if is_authentic else "DOCUMENT SUSPICIOUS"]
        ]
        
        results_table = Table(results_info, colWidths=[2*inch, 3*inch])
        results_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#f0fdf4') if is_authentic else colors.HexColor('#fef2f2')),
            ('TEXTCOLOR', (1, 0), (1, 0), authenticity_color),
            ('TEXTCOLOR', (1, 3), (1, 3), authenticity_color),
            ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, -1), 10),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
            ('TOPPADDING', (0, 0), (-1, -1), 6),
            ('GRID', (0, 0), (-1, -1), 1, colors.HexColor('#e5e7eb'))
        ]))
        story.append(results_table)
        story.append(Spacer(1, 20))
        
        # Detailed Analysis
        if 'analysis' in data and data['analysis']:
            story.append(Paragraph("DETAILED ANALYSIS", heading_style))
            
            analysis_items = []
            for key, value in data['analysis'].items():
                analysis_items.append([key.replace('_', ' ').title() + ":", str(value)])
            
            if analysis_items:
                analysis_table = Table(analysis_items, colWidths=[2*inch, 3*inch])
                analysis_table.setStyle(TableStyle([
                    ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
                    ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                    ('FONTSIZE', (0, 0), (-1, -1), 9),
                    ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
                    ('TOPPADDING', (0, 0), (-1, -1), 4),
                    ('GRID', (0, 0), (-1, -1), 1, colors.HexColor('#f1f5f9'))
                ]))
                story.append(analysis_table)
                story.append(Spacer(1, 20))
        
        # Issues/Anomalies
        if 'issues' in data and data['issues']:
            story.append(Paragraph("DETECTED ISSUES & ANOMALIES", heading_style))
            
            for i, issue in enumerate(data['issues'], 1):
                if issue != 'No significant issues detected':
                    story.append(Paragraph(f"{i}. {issue}", normal_style))
            
            story.append(Spacer(1, 20))
        
        # Comparison Details
        if 'comparison_details' in data and data['comparison_details']:
            story.append(Paragraph("COMPARISON METRICS", heading_style))
            
            comparison_items = []
            for key, value in data['comparison_details'].items():
                comparison_items.append([key.replace('_', ' ').title() + ":", str(value)])
            
            if comparison_items:
                comparison_table = Table(comparison_items, colWidths=[2*inch, 3*inch])
                comparison_table.setStyle(TableStyle([
                    ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
                    ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                    ('FONTSIZE', (0, 0), (-1, -1), 9),
                    ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
                    ('TOPPADDING', (0, 0), (-1, -1), 4),
                    ('GRID', (0, 0), (-1, -1), 1, colors.HexColor('#f1f5f9'))
                ]))
                story.append(comparison_table)
                story.append(Spacer(1, 20))
        
        # System Information
        story.append(Paragraph("SYSTEM INFORMATION", heading_style))
        system_info = [
            ["Software Version:", "CYBER-ID VERIFIER v3.1"],
            ["Report Format:", "Official Verification Document"],
            ["Generated By:", "Administrator"],
            ["Purpose:", "Document Authentication Verification"],
            ["Confidentiality:", "Level 3 - Restricted"]
        ]
        
        system_table = Table(system_info, colWidths=[2*inch, 3*inch])
        system_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#f8fafc')),
            ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
            ('FONTSIZE', (0, 0), (-1, -1), 9),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
            ('TOPPADDING', (0, 0), (-1, -1), 4),
            ('GRID', (0, 0), (-1, -1), 1, colors.HexColor('#e2e8f0'))
        ]))
        story.append(system_table)
        story.append(Spacer(1, 30))
        
        # Footer/Disclaimer
        disclaimer = """
        <b>DISCLAIMER:</b> This report is generated automatically by the Cyber-ID Verifier system. 
        The results are based on computer analysis and should be used as a reference only. 
        Final authentication decisions should be made by trained personnel. 
        This document is confidential and intended for authorized personnel only.
        """
        story.append(Paragraph(disclaimer, ParagraphStyle(
            'Disclaimer',
            parent=styles['Normal'],
            fontSize=8,
            textColor=colors.grey,
            alignment=1
        )))
        
        # Build PDF
        doc.build(story)
        
        # Get PDF data
        pdf_data = buffer.getvalue()
        buffer.close()
        
        # Save to reports folder
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        doc_type = data.get('document_type', 'document').replace('_', '')
        status = "authentic" if is_authentic else "suspicious"
        filename = f"cyberid_verification_{doc_type}_{status}_{timestamp}.pdf"
        filepath = os.path.join(REPORTS_FOLDER, filename)
        
        # Save file to reports folder
        with open(filepath, 'wb') as f:
            f.write(pdf_data)
        
        # Create response
        response = make_response(pdf_data)
        response.headers['Content-Type'] = 'application/pdf'
        response.headers['Content-Disposition'] = f'attachment; filename="{filename}"'
        
        # Log the report generation
        print(f"Report generated: {filename} - {doc_type} - Authentic: {is_authentic}")

        return response

    except Exception as e:
        print(f"Report generation error: {e}")
        return jsonify({'success': False, 'message': f'Report generation failed: {str(e)}'}), 500

def calculate_monthly_payment(amount, months, annual_rate):
    """Calculate monthly payment using annuity formula."""
    if months <= 0:
        return 0.0
    r = annual_rate / 100.0 / 12.0
    if r > 0:
        return (amount * r) / (1 - (1 + r) ** (-months))
    return amount / months

def preprocess_and_validate_application(form_data, files_data):
    """Clean and validate loan application fields before model inference."""
    cleaned = {}
    issues = []
    errors = []

    cleaned['full_name'] = re.sub(r'\s+', ' ', (form_data.get('full_name', '') or '').strip())
    cleaned['contact'] = re.sub(r'\s+', '', (form_data.get('contact', '') or '').strip())
    cleaned['employment_status'] = (form_data.get('employment_status', '') or '').strip().lower()
    cleaned['employer_name'] = re.sub(r'\s+', ' ', (form_data.get('employer_name', '') or '').strip())
    cleaned['requested_documents'] = form_data.getlist('requested_documents') if hasattr(form_data, 'getlist') else []
    cleaned['primary_id_type'] = (form_data.get('primary_id_type', '') or '').strip().lower()

    try:
        cleaned['amount'] = max(0.0, float(form_data.get('amount', '0')))
    except Exception:
        cleaned['amount'] = 0.0
        errors.append('Loan amount must be numeric.')

    try:
        cleaned['months'] = int(form_data.get('months', '0'))
    except Exception:
        cleaned['months'] = 0
        errors.append('Repayment months must be an integer.')

    try:
        cleaned['interest_rate'] = float(form_data.get('interest_rate', '0'))
    except Exception:
        cleaned['interest_rate'] = 0.0
        errors.append('Interest rate must be numeric.')

    try:
        cleaned['monthly_income'] = max(0.0, float(form_data.get('monthly_income', '0')))
    except Exception:
        cleaned['monthly_income'] = 0.0
        errors.append('Monthly income must be numeric.')

    try:
        cleaned['other_income'] = max(0.0, float(form_data.get('other_income', '0')))
    except Exception:
        cleaned['other_income'] = 0.0
        errors.append('Other income must be numeric.')

    try:
        cleaned['credit_score'] = int(form_data.get('credit_score', '0'))
    except Exception:
        cleaned['credit_score'] = 0
        errors.append('Credit score must be an integer.')

    try:
        cleaned['existing_debt'] = max(0.0, float(form_data.get('existing_debt', '0')))
    except Exception:
        cleaned['existing_debt'] = 0.0
        errors.append('Existing debt must be numeric.')

    if not cleaned['full_name']:
        errors.append('Full name is required.')
    if not re.match(r'^(\+63|09)\d{9}$', cleaned['contact']):
        errors.append('Contact number must be a valid Philippine number.')
    if cleaned['amount'] < 1000:
        errors.append('Minimum loan amount is PHP 1,000.')
    if cleaned['months'] <= 0:
        errors.append('Repayment period is required.')
    if cleaned['interest_rate'] < 0 or cleaned['interest_rate'] > 60:
        errors.append('Interest rate must be between 0 and 60%.')
    if cleaned['employment_status'] not in {'employed', 'self_employed', 'contract', 'unemployed'}:
        errors.append('Employment status is required.')
    if cleaned['monthly_income'] <= 0:
        errors.append('Monthly income must be greater than zero.')
    if cleaned['credit_score'] and (cleaned['credit_score'] < 300 or cleaned['credit_score'] > 850):
        errors.append('Credit score must be between 300 and 850.')
    if cleaned['primary_id_type'] not in {'passport', 'national_id', 'drivers_license'}:
        errors.append('Primary ID must be passport, national ID, or driver license.')

    monthly_payment = calculate_monthly_payment(cleaned['amount'], cleaned['months'], cleaned['interest_rate'])
    cleaned['monthly_payment'] = monthly_payment
    disposable_income = (cleaned['monthly_income'] + cleaned['other_income']) - cleaned['existing_debt']

    if disposable_income < monthly_payment:
        issues.append('Estimated monthly payment is higher than disposable income.')
    if cleaned['credit_score'] and cleaned['credit_score'] < 600:
        issues.append('Applicant credit score is below preferred threshold (600).')
    if cleaned['employment_status'] == 'unemployed':
        issues.append('Applicant is currently unemployed and requires manual review.')
    if len(cleaned['requested_documents']) == 0:
        issues.append('No supporting document checklist selected.')

    primary_id_file = None
    if files_data:
        primary_id_file = files_data.get('primary_id_document')
    if primary_id_file and primary_id_file.filename:
        cleaned['has_primary_id_document'] = True
    else:
        cleaned['has_primary_id_document'] = False
        errors.append('Primary ID document is required.')

    if files_data and files_data.get('supporting_document') and files_data.get('supporting_document').filename:
        cleaned['has_supporting_document'] = True
    else:
        cleaned['has_supporting_document'] = False
        issues.append('No supporting document file uploaded.')

    return cleaned, issues, errors

def run_loan_risk_model(cleaned, issues):
    """Simple scoring model that emulates deep-learning risk output for decision support."""
    cfg = get_model_config()

    total_income = cleaned['monthly_income'] + cleaned['other_income']
    monthly_payment = cleaned['monthly_payment']
    dti = ((cleaned['existing_debt'] + monthly_payment) / max(total_income, 1.0))
    income_to_loan = total_income / max(cleaned['amount'], 1.0)
    credit_norm = ((cleaned['credit_score'] or 600) - 300) / 550.0
    employment_map = {
        'employed': 1.0,
        'self_employed': 0.75,
        'contract': 0.6,
        'unemployed': 0.2
    }
    employment_norm = employment_map.get(cleaned['employment_status'], 0.5)

    raw_score = (
        (1.15 * dti)
        - (0.90 * income_to_loan)
        - (0.80 * credit_norm)
        - (0.55 * employment_norm)
        + (0.08 * len(issues))
    )
    sigmoid = 1.0 / (1.0 + np.exp(-raw_score))
    risk_score = float(sigmoid * 100.0)

    approve_threshold = float(cfg.get('approval_threshold', 35.0))
    reject_threshold = float(cfg.get('reject_threshold', 65.0))

    if risk_score <= approve_threshold:
        recommendation = 'approved'
    elif risk_score >= reject_threshold:
        recommendation = 'rejected'
    else:
        recommendation = 'requires_further_review'

    if risk_score < 35:
        risk_level = 'low'
    elif risk_score < 65:
        risk_level = 'medium'
    else:
        risk_level = 'high'

    midpoint = (approve_threshold + reject_threshold) / 2.0
    confidence = min(99.0, max(50.0, abs(risk_score - midpoint) * 1.6 + 55.0))

    explanation_items = [
        f"Debt-to-income ratio assessed at {dti:.2f}",
        f"Credit score considered at {cleaned['credit_score'] or 'not provided'}",
        f"Employment stability class: {cleaned['employment_status'].replace('_', ' ')}",
        f"Income to loan ratio: {income_to_loan:.3f}"
    ]

    if issues:
        explanation_items.append(f"Validation raised {len(issues)} cautionary issue(s)")

    return {
        'model_version': cfg.get('version', MODEL_DEFAULTS['version']),
        'risk_score': round(risk_score, 2),
        'risk_level': risk_level,
        'confidence_score': round(confidence, 2),
        'recommendation': recommendation,
        'explanation': explanation_items,
        'features': {
            'dti': round(dti, 4),
            'income_to_loan': round(income_to_loan, 4),
            'credit_norm': round(credit_norm, 4),
            'employment_norm': round(employment_norm, 4)
        }
    }

def detect_fraud_signals(cleaned):
    """Identify suspicious patterns for fraud/risk escalation."""
    flags = []
    total_income = cleaned['monthly_income'] + cleaned['other_income']
    if cleaned['amount'] > total_income * 40:
        flags.append('Requested amount is unusually high versus declared income.')
    if cleaned['credit_score'] and cleaned['credit_score'] < 500 and cleaned['amount'] > 300000:
        flags.append('Large loan requested with very low credit score.')
    if cleaned['employment_status'] == 'unemployed' and cleaned['amount'] > 50000:
        flags.append('Unemployed applicant requesting high amount.')
    if any(char.isdigit() for char in cleaned['full_name']):
        flags.append('Applicant name contains numeric characters.')
    if not cleaned.get('has_supporting_document'):
        flags.append('No supporting document uploaded for verification.')

    fraud_probability = min(95.0, 20.0 + len(flags) * 15.0)
    return {
        'flags': flags,
        'high_risk': len(flags) >= 2,
        'fraud_probability': round(fraud_probability, 2)
    }

@app.route('/loan-application', methods=['GET', 'POST'])
def loan_application():
    """Render loan application form (GET) and handle submission (POST)."""
    if not verify_session(request):
        return redirect('/login')

    session_data = get_active_session(request) or {}
    user_id = get_user_id_from_session(request)

    if not user_id:
        return redirect('/login')

    role = session_data.get('role')

    def get_latest_application_prefill(uid):
        """Load latest loan application values to prefill form fields."""
        try:
            conn = sqlite3.connect(DB_PATH)
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            c.execute(
                '''SELECT full_name, contact, amount, months, interest_rate, employment_status,
                          employer_name, monthly_income, other_income, primary_id_type
                   FROM loan_applications
                   WHERE user_id = ?
                   ORDER BY COALESCE(timestamp, start_date) DESC
                   LIMIT 1''',
                (uid,)
            )
            row = c.fetchone()
            conn.close()
            return dict(row) if row else {}
        except Exception:
            return {}

    def get_pending_applicant_context(applicant_id):
        """Return selected applicant context when admin chooses a pending applicant."""
        try:
            conn = sqlite3.connect(DB_PATH)
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            c.execute(
                '''SELECT id, username, COALESCE(full_name, username) AS full_name
                   FROM user_accounts
                   WHERE id = ? AND role = 'applicant' ''',
                (applicant_id,)
            )
            user_row = c.fetchone()
            if not user_row:
                conn.close()
                return None

            c.execute(
                """SELECT COUNT(*) FROM loan_applications
                       WHERE user_id = ? AND LOWER(COALESCE(recommendation, '')) IN ('pending', 'requires_further_review')""",
                (applicant_id,)
            )
            pending_count = int(c.fetchone()[0] or 0)

            is_pending_user = False
            c.execute('SELECT is_active FROM user_accounts WHERE id = ?', (applicant_id,))
            active_row = c.fetchone()
            if active_row:
                is_pending_user = int(active_row['is_active'] or 0) == 0

            if not is_pending_user and pending_count <= 0:
                conn.close()
                return None

            conn.close()
            return {
                'user_id': int(user_row['id']),
                'username': user_row['username'],
                'full_name': user_row['full_name'],
                'pending_count': pending_count
            }
        except Exception:
            return None

    selected_applicant_user_id = None
    selected_applicant_full_name = ''
    target_user_id = user_id

    selected_id_raw = (request.args.get('applicant_user_id') if request.method == 'GET' else request.form.get('applicant_user_id')) or ''
    selected_id_raw = str(selected_id_raw).strip()
    if role == 'admin' and selected_id_raw.isdigit():
        selected_ctx = get_pending_applicant_context(int(selected_id_raw))
        if selected_ctx:
            selected_applicant_user_id = selected_ctx['user_id']
            selected_applicant_full_name = selected_ctx['full_name']
            target_user_id = selected_applicant_user_id

    verified_from_analysis = False
    if request.method == 'GET':
        verified_from_analysis = (request.args.get('verified_from_analysis') or '').strip() == '1'
    else:
        verified_from_analysis = (request.form.get('verified_from_analysis') or '').strip() == '1'

    def has_active_loan(uid):
        try:
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute('SELECT COUNT(*) FROM loan_applications WHERE user_id = ?', (uid,))
            count = int(c.fetchone()[0] or 0)
            conn.close()
            return count > 0
        except Exception:
            return False

    # GET: render form
    if request.method == 'GET':
        prefill = get_latest_application_prefill(target_user_id)
        target_full_name = (
            selected_applicant_full_name
            or prefill.get('full_name')
            or session_data.get('full_name', '')
        )

        context = {
            'full_name': target_full_name,
            'contact': prefill.get('contact', ''),
            'amount': prefill.get('amount', ''),
            'months': prefill.get('months', ''),
            'interest_rate': prefill.get('interest_rate', 5),
            'employment_status': prefill.get('employment_status', ''),
            'employer_name': prefill.get('employer_name', ''),
            'monthly_income': prefill.get('monthly_income', ''),
            'other_income': prefill.get('other_income', ''),
            'primary_id_type': prefill.get('primary_id_type', ''),
            'selected_applicant_user_id': selected_applicant_user_id,
            'selected_applicant_full_name': selected_applicant_full_name,
            'verified_from_analysis': verified_from_analysis,
            'is_admin_view': role == 'admin'
        }

        if has_active_loan(target_user_id):
            context['error'] = 'You already have an active loan. Please settle your current loan before applying again.'
            context['can_apply'] = False
            return render_template('loan_application.html', **context)

        if role == 'admin' and selected_id_raw and not selected_applicant_user_id:
            context['error'] = 'Selected applicant is invalid or does not have a pending user/application record.'
            context['can_apply'] = False
            return render_template('loan_application.html', **context)

        context['can_apply'] = True
        return render_template('loan_application.html', **context)

    if has_active_loan(target_user_id):
        return render_template(
            'loan_application.html',
            full_name=selected_applicant_full_name or session_data.get('full_name', ''),
            error='You already have an active loan. Please settle your current loan before applying again.',
            can_apply=False,
            selected_applicant_user_id=selected_applicant_user_id,
            selected_applicant_full_name=selected_applicant_full_name,
            verified_from_analysis=verified_from_analysis,
            is_admin_view=role == 'admin'
        )

    # POST: process form submission
    cleaned, validation_issues, validation_errors = preprocess_and_validate_application(request.form, request.files)
    if validation_errors:
        return render_template(
            'loan_application.html',
            error='; '.join(validation_errors),
            full_name=cleaned.get('full_name', ''),
            contact=cleaned.get('contact', ''),
            amount=cleaned.get('amount', 0),
            months=cleaned.get('months', ''),
            interest_rate=cleaned.get('interest_rate', 0),
            employment_status=cleaned.get('employment_status', ''),
            employer_name=cleaned.get('employer_name', ''),
            monthly_income=cleaned.get('monthly_income', 0),
            other_income=cleaned.get('other_income', 0),
            credit_score=cleaned.get('credit_score', ''),
            existing_debt=cleaned.get('existing_debt', 0),
            primary_id_type=cleaned.get('primary_id_type', ''),
            can_apply=True,
            selected_applicant_user_id=selected_applicant_user_id,
            selected_applicant_full_name=selected_applicant_full_name,
            verified_from_analysis=verified_from_analysis,
            is_admin_view=role == 'admin'
        )

    full_name = cleaned['full_name']
    contact = cleaned['contact']
    amount = cleaned['amount']
    months = cleaned['months']
    annual_rate = cleaned['interest_rate']
    monthly_payment = cleaned['monthly_payment']
    monthly_payment_str = f"PHP {monthly_payment:,.2f}"

    model_output = run_loan_risk_model(cleaned, validation_issues)
    fraud_output = detect_fraud_signals(cleaned)

    # Loan decision workflow:
    # - default pending
    # - admin verify->proceed flow can auto-mark approved
    recommendation = 'pending'
    id_verification_result = {
        'is_authentic': False,
        'confidence': 0.0,
        'issues': ['Primary ID analysis was not completed']
    }

    # Persist application to reports folder (JSON) and save to SQLite
    try:
        os.makedirs(REPORTS_FOLDER, exist_ok=True)
        app_data = {
            'timestamp': datetime.now().isoformat(),
            'full_name': full_name,
            'contact': contact,
            'amount': amount,
            'months': months,
            'interest_rate': annual_rate,
            'monthly_payment': monthly_payment,
            'employment_status': cleaned.get('employment_status'),
            'employer_name': cleaned.get('employer_name'),
            'monthly_income': cleaned.get('monthly_income'),
            'other_income': cleaned.get('other_income'),
            'credit_score': cleaned.get('credit_score'),
            'existing_debt': cleaned.get('existing_debt'),
            'requested_documents': cleaned.get('requested_documents', []),
            'primary_id_type': cleaned.get('primary_id_type'),
            'validation_issues': validation_issues,
            'recommendation': recommendation,
            'confidence_score': model_output['confidence_score'],
            'risk_level': model_output['risk_level'],
            'risk_score': model_output['risk_score'],
            'fraud_flags': fraud_output['flags'],
            'fraud_probability': fraud_output['fraud_probability'],
            'model_version': model_output['model_version'],
            'decision_explanation': model_output['explanation']
        }

        supporting_doc_file = request.files.get('supporting_document')
        support_doc_path = None
        if supporting_doc_file and supporting_doc_file.filename:
            safe_name = secure_filename(supporting_doc_file.filename)
            doc_filename = f"loan_support_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{safe_name}"
            support_doc_path = os.path.join(UPLOAD_FOLDER, doc_filename)
            supporting_doc_file.save(support_doc_path)
            app_data['supporting_document_path'] = support_doc_path

        primary_id_file = request.files.get('primary_id_document')
        primary_id_path = None
        if primary_id_file and primary_id_file.filename:
            safe_name = secure_filename(primary_id_file.filename)
            id_filename = f"primary_id_{cleaned.get('primary_id_type', 'id')}_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{safe_name}"
            primary_id_path = os.path.join(UPLOAD_FOLDER, id_filename)
            primary_id_file.save(primary_id_path)
            app_data['primary_id_path'] = primary_id_path

            # Analyze uploaded primary ID to drive approval status.
            try:
                id_verification_result = analyze_with_comparison(
                    primary_id_path,
                    cleaned.get('primary_id_type') or DEFAULT_DOC_TYPE
                )
            except Exception as id_err:
                id_verification_result = {
                    'is_authentic': False,
                    'confidence': 0.0,
                    'issues': [f'Primary ID analysis failed: {str(id_err)}']
                }

            if not bool(id_verification_result.get('is_authentic')):
                issues = id_verification_result.get('issues') or ['Primary ID is not authentic']
                validation_issues.append(f"Primary ID verification pending: {issues[0]}")

        app_data['recommendation'] = recommendation
        app_data['id_verification'] = id_verification_result

        # include verification JSON if provided
        ver_json = request.form.get('verification_json')
        verification_text = None
        if ver_json:
            try:
                app_data['verification'] = {
                    'client_verification': json.loads(ver_json),
                    'system_id_verification': id_verification_result
                }
                verification_text = json.dumps(app_data['verification'], ensure_ascii=False)
            except:
                app_data['verification'] = {
                    'client_verification_raw': ver_json,
                    'system_id_verification': id_verification_result
                }
                verification_text = json.dumps(app_data['verification'], ensure_ascii=False)
        else:
            app_data['verification'] = {'system_id_verification': id_verification_result}
            verification_text = json.dumps(app_data['verification'], ensure_ascii=False)

        verified_from_analysis_post = (request.form.get('verified_from_analysis') or '').strip() == '1'
        if not verified_from_analysis_post and ver_json:
            try:
                parsed_verification = json.loads(ver_json)
                if isinstance(parsed_verification, list):
                    verified_from_analysis_post = any(bool(item.get('ok')) for item in parsed_verification if isinstance(item, dict))
            except Exception:
                pass

        if role == 'admin' and selected_applicant_user_id and verified_from_analysis_post:
            recommendation = 'approved'
            app_data['recommendation'] = recommendation

        # Save JSON copy (optional backup)
        try:
            filename = os.path.join(REPORTS_FOLDER, f"loan_application_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
            with open(filename, 'w', encoding='utf-8') as f:
                json.dump(app_data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print('Failed to save loan application JSON backup:', e)

        # Save to SQLite
        try:
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            start_date = datetime.now().isoformat()  # Set start date when loan is created
            
            c.execute('''INSERT INTO loan_applications
                         (user_id, timestamp, full_name, contact, amount, months, interest_rate, monthly_payment,
                          verification, start_date, employment_status, employer_name, monthly_income,
                          other_income, credit_score, existing_debt, requested_documents, primary_id_type,
                          primary_id_path, supporting_document_path, validation_issues, recommendation, confidence_score,
                          risk_level, risk_score, fraud_flags, decision_explanation, model_version, model_raw_json)
                         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                      (
                          target_user_id, app_data['timestamp'], full_name, contact, amount, months, annual_rate, monthly_payment,
                          verification_text, start_date, app_data.get('employment_status'), app_data.get('employer_name'),
                          app_data.get('monthly_income'), app_data.get('other_income'), app_data.get('credit_score'),
                          app_data.get('existing_debt'), json.dumps(app_data.get('requested_documents', []), ensure_ascii=False),
                          app_data.get('primary_id_type'), primary_id_path, support_doc_path,
                          json.dumps(validation_issues, ensure_ascii=False), recommendation,
                          model_output['confidence_score'], model_output['risk_level'], model_output['risk_score'],
                          json.dumps(fraud_output['flags'], ensure_ascii=False),
                          json.dumps(model_output['explanation'], ensure_ascii=False),
                          model_output['model_version'], json.dumps(model_output, ensure_ascii=False)
                      ))
            conn.commit()
            conn.close()
        except Exception as e:
            print('Failed to save loan application to SQLite:', e)
    except Exception as e:
        print('Failed to prepare loan application save:', e)

    if role == 'applicant':
        return redirect('/user-dashboard?loan_submitted=1')

    return render_template('loan_confirmation.html', full_name=full_name, contact=contact,
                           amount=f"PHP {amount:,.2f}", months=months,
                           interest_rate=annual_rate, monthly_payment=monthly_payment_str,
                           recommendation=recommendation.replace('_', ' ').title(),
                           risk_level=model_output['risk_level'].title(),
                           confidence_score=model_output['confidence_score'],
                           fraud_probability=fraud_output['fraud_probability'],
                           validation_issues=validation_issues,
                           decision_explanation=model_output['explanation'])

@app.route('/list-reports', methods=['GET'])
def list_reports():
    """List all generated reports"""
    if not verify_session(request):
        return jsonify({'success': False, 'message': 'Authentication required'}), 401
    
    try:
        reports = []
        for filename in os.listdir(REPORTS_FOLDER):
            if filename.endswith('.pdf'):
                filepath = os.path.join(REPORTS_FOLDER, filename)
                file_stats = os.stat(filepath)
                reports.append({
                    'filename': filename,
                    'size': file_stats.st_size,
                    'created': datetime.fromtimestamp(file_stats.st_ctime).isoformat(),
                    'path': f'/download-report/{filename}'
                })
        
        return jsonify({
            'success': True,
            'reports': sorted(reports, key=lambda x: x['created'], reverse=True),
            'count': len(reports)
        })
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500


@app.route('/dashboard', methods=['GET'])
def dashboard():
    """Show a dashboard of loan applications (requires auth)."""
    if not verify_session(request):
        return redirect('/login')

    session_data = get_active_session(request) or {}
    current_role = session_data.get('role', '')
    pending_users_count = 0
    pending_loan_count = 0

    apps = []
    fully_paid_ids = []  # Track fully paid applications for archiving
    
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute('''
            SELECT id,timestamp,full_name,contact,amount,months,interest_rate,monthly_payment,verification,start_date,
                   recommendation,risk_level,confidence_score,risk_score,fraud_flags
            FROM loan_applications
            ORDER BY id DESC
        ''')
        rows = c.fetchall()
        now = datetime.now()
        
        for r in rows:
            # compute total paid for this application
            try:
                c.execute('SELECT COALESCE(SUM(amount_paid), 0) as total_paid FROM payments WHERE application_id = ?', (r['id'],))
                srow = c.fetchone()
                total_paid = float(srow['total_paid']) if srow and 'total_paid' in srow.keys() else (float(srow[0]) if srow else 0.0)
            except Exception:
                total_paid = 0.0

            amount_val = r['amount'] if r['amount'] is not None else 0.0
            balance = float(amount_val) - float(total_paid)

            # Skip fully paid loans (balance <= 0) - archive them to paid_loans_archive
            if balance <= 0:
                fully_paid_ids.append(r['id'])
                # Archive the fully paid loan record
                try:
                    paid_date = datetime.now().isoformat()
                    c.execute('''INSERT INTO paid_loans_archive 
                                (original_id, timestamp, full_name, contact, amount, months, interest_rate, monthly_payment, total_paid, paid_date, verification)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                              (r['id'], r['timestamp'], r['full_name'], r['contact'], r['amount'], 
                               r['months'], r['interest_rate'], r['monthly_payment'], total_paid, paid_date,
                               r['verification'] if 'verification' in r.keys() else None))
                    # Delete from active tables
                    c.execute('DELETE FROM payments WHERE application_id = ?', (r['id'],))
                    c.execute('DELETE FROM loan_applications WHERE id = ?', (r['id'],))
                    print(f"Archived fully paid loan application ID: {r['id']} (Name: {r['full_name']}, Total Paid: {total_paid})")
                except Exception as arch_err:
                    print(f"Error archiving fully paid loan: {arch_err}")
                continue  # Don't add to display list

            # Calculate overdue status
            start_date_str = r['start_date'] if 'start_date' in r.keys() and r['start_date'] else r['timestamp']
            is_overdue = False
            months_overdue = 0
            try:
                if start_date_str:
                    start_date = datetime.fromisoformat(start_date_str.replace('Z', '+00:00').split('+')[0])
                    months_since_start = max(0, (now.year - start_date.year) * 12 + (now.month - start_date.month))
                    expected_payments = min(months_since_start, r['months'] or 0)
                    monthly_payment = float(r['monthly_payment']) if r['monthly_payment'] else 0.0
                    expected_amount = expected_payments * monthly_payment
                    
                    if total_paid < expected_amount and expected_payments > 0:
                        is_overdue = True
                        months_overdue = max(1, int((expected_amount - total_paid) / monthly_payment)) if monthly_payment > 0 else 0
            except Exception as e:
                print(f"Error calculating overdue status: {e}")

            try:
                fraud_flag_count = len(json.loads(r['fraud_flags'])) if r['fraud_flags'] else 0
            except Exception:
                fraud_flag_count = 0

            apps.append({
                'id': r['id'],
                'timestamp': r['timestamp'],
                'full_name': r['full_name'],
                'contact': r['contact'],
                'amount': r['amount'],
                'months': r['months'],
                'interest_rate': r['interest_rate'],
                'monthly_payment': r['monthly_payment'],
                'total_paid': total_paid,
                'balance': balance,
                'start_date': start_date_str,
                'is_overdue': is_overdue,
                'months_overdue': months_overdue,
                'recommendation': r['recommendation'] or 'pending',
                'risk_level': r['risk_level'] or 'medium',
                'confidence_score': float(r['confidence_score']) if r['confidence_score'] is not None else 0.0,
                'risk_score': float(r['risk_score']) if r['risk_score'] is not None else 0.0,
                'fraud_flag_count': fraud_flag_count
            })
        
        # Commit the archiving
        if fully_paid_ids:
            conn.commit()
            print(f"Archived {len(fully_paid_ids)} fully paid loan(s)")

        if current_role == 'admin':
            c.execute("SELECT COUNT(*) FROM user_accounts WHERE is_active = 0 AND role = 'applicant'")
            pending_users_count = int(c.fetchone()[0] or 0)

            c.execute("""
                SELECT COUNT(*)
                FROM loan_applications
                WHERE LOWER(COALESCE(recommendation, '')) IN ('pending', 'requires_further_review')
            """)
            pending_loan_count = int(c.fetchone()[0] or 0)
        
        conn.close()
    except Exception as e:
        print('Failed to load applications for dashboard:', e)

    return render_template(
        'dashboard.html',
        applications=apps,
        current_role=current_role,
        pending_users_count=pending_users_count,
        pending_loan_count=pending_loan_count,
    )


@app.route('/user-dashboard', methods=['GET'])
def user_dashboard():
    """Show dashboard filtered to user's own loan applications."""
    if not verify_session(request):
        return redirect('/login')

    loan_submitted = (request.args.get('loan_submitted') or '').strip() == '1'

    user_id = get_user_id_from_session(request)
    if not user_id:
        return render_template(
            'user_dashboard.html',
            applications=[],
            user_name='User',
            loan_submitted=loan_submitted,
            active_loan_count=0,
            pending_review_count=0,
        )

    apps = []
    user_name = 'User'
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        
        # Get user's name
        c.execute('SELECT full_name FROM user_accounts WHERE id = ?', (user_id,))
        user_row = c.fetchone()
        user_name = user_row['full_name'] if user_row else 'User'
        
        # Get user's applications
        c.execute('''
            SELECT id, timestamp, full_name, contact, amount, months, interest_rate, monthly_payment, verification, start_date,
                   recommendation, risk_level, confidence_score, risk_score, fraud_flags
            FROM loan_applications
            WHERE user_id = ?
            ORDER BY id DESC
        ''', (user_id,))
        rows = c.fetchall()
        now = datetime.now()
        
        for r in rows:
            try:
                c.execute('SELECT COALESCE(SUM(amount_paid), 0) as total_paid FROM payments WHERE application_id = ?', (r['id'],))
                srow = c.fetchone()
                total_paid = float(srow['total_paid']) if srow and 'total_paid' in srow.keys() else (float(srow[0]) if srow else 0.0)
            except Exception:
                total_paid = 0.0

            amount_val = r['amount'] if r['amount'] is not None else 0.0
            balance = float(amount_val) - float(total_paid)

            start_date_str = r['start_date'] if 'start_date' in r.keys() and r['start_date'] else r['timestamp']
            is_overdue = False
            months_overdue = 0
            try:
                if start_date_str:
                    start_date = datetime.fromisoformat(start_date_str.replace('Z', '+00:00').split('+')[0])
                    months_since_start = max(0, (now.year - start_date.year) * 12 + (now.month - start_date.month))
                    expected_payments = min(months_since_start, r['months'] or 0)
                    monthly_payment = float(r['monthly_payment']) if r['monthly_payment'] else 0.0
                    expected_amount = expected_payments * monthly_payment
                    
                    if total_paid < expected_amount and expected_payments > 0:
                        is_overdue = True
                        months_overdue = max(1, int((expected_amount - total_paid) / monthly_payment)) if monthly_payment > 0 else 0
            except Exception as e:
                print(f"Error calculating overdue status: {e}")

            try:
                fraud_flag_count = len(json.loads(r['fraud_flags'])) if r['fraud_flags'] else 0
            except Exception:
                fraud_flag_count = 0

            apps.append({
                'id': r['id'],
                'timestamp': r['timestamp'],
                'full_name': r['full_name'],
                'contact': r['contact'],
                'amount': r['amount'],
                'months': r['months'],
                'interest_rate': r['interest_rate'],
                'monthly_payment': r['monthly_payment'],
                'total_paid': total_paid,
                'balance': balance,
                'start_date': start_date_str,
                'is_overdue': is_overdue,
                'months_overdue': months_overdue,
                'recommendation': r['recommendation'] or 'pending',
                'risk_level': r['risk_level'] or 'medium',
                'confidence_score': float(r['confidence_score']) if r['confidence_score'] is not None else 0.0,
                'risk_score': float(r['risk_score']) if r['risk_score'] is not None else 0.0,
                'fraud_flag_count': fraud_flag_count
            })
        
        conn.close()
    except Exception as e:
        print('Failed to load user applications:', e)

    active_loan_count = sum(1 for app in apps if (app.get('recommendation') or '').lower() == 'approved')
    pending_review_count = sum(1 for app in apps if (app.get('recommendation') or '').lower() in {'pending', 'requires_further_review'})

    return render_template(
        'user_dashboard.html',
        applications=apps,
        user_name=user_name,
        loan_submitted=loan_submitted,
        active_loan_count=active_loan_count,
        pending_review_count=pending_review_count,
    )


@app.route('/user/documents-page', methods=['GET'])
def user_documents_page():
    """Render applicant document upload page."""
    if not verify_session(request):
        return redirect('/login')

    session_data = get_active_session(request) or {}
    return render_template('user_documents.html', user_name=session_data.get('full_name', 'Applicant'))


@app.route('/user/upload-document', methods=['POST'])
def user_upload_document():
    """Upload identity document to user profile."""
    if not verify_session(request):
        return jsonify({'success': False, 'message': 'Authentication required'}), 401

    user_id = get_user_id_from_session(request)
    if not user_id:
        return jsonify({'success': False, 'message': 'User not found'}), 400

    document_type = request.form.get('document_type', 'government_id')
    doc_file = request.files.get('document_file')

    if not doc_file or not doc_file.filename:
        return jsonify({'success': False, 'message': 'No file provided'}), 400

    if not allowed_file(doc_file.filename):
        return jsonify({'success': False, 'message': 'Invalid file type. Allowed: png, jpg, jpeg, pdf'}), 400

    try:
        # Create user-specific folder
        user_docs_folder = os.path.join('user_documents', f'user_{user_id}')
        os.makedirs(user_docs_folder, exist_ok=True)

        # Save file
        safe_name = secure_filename(doc_file.filename)
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = f"{document_type}_{timestamp}_{safe_name}"
        file_path = os.path.join(user_docs_folder, filename)
        doc_file.save(file_path)

        # Record in database
        result = save_user_document(user_id, document_type, file_path)
        
        if result['success']:
            return jsonify({
                'success': True,
                'message': 'Document uploaded successfully',
                'document_id': result['document_id'],
                'file_path': file_path
            }), 201
        else:
            return jsonify({'success': False, 'message': result['message']}), 500
    except Exception as e:
        return jsonify({'success': False, 'message': f'Upload failed: {str(e)}'}), 500


@app.route('/user/documents', methods=['GET'])
def user_get_documents():
    """Get user's uploaded documents."""
    if not verify_session(request):
        return jsonify({'success': False, 'message': 'Authentication required'}), 401

    user_id = get_user_id_from_session(request)
    if not user_id:
        return jsonify({'success': False, 'message': 'User not found'}), 400

    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute('''
            SELECT id, document_type, file_path, uploaded_at, verified_by_admin, verification_notes
            FROM user_documents
            WHERE user_id = ?
            ORDER BY uploaded_at DESC
        ''', (user_id,))
        rows = c.fetchall()
        conn.close()

        documents = []
        for r in rows:
            documents.append({
                'id': r['id'],
                'document_type': r['document_type'],
                'file_path': r['file_path'],
                'uploaded_at': r['uploaded_at'],
                'verified': r['verified_by_admin'] == 1,
                'verification_notes': r['verification_notes']
            })

        return jsonify({'success': True, 'documents': documents})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500


@app.route('/admin/documents', methods=['GET'])
def admin_documents():
    """Browse all user-uploaded documents (admin only)."""
    allowed, auth_result, status = require_roles(request, {'admin'})
    if not allowed:
        return auth_result, status

    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        
        # Get all user documents with user info
        c.execute('''
            SELECT ud.id, ud.user_id, ud.document_type, ud.file_path, ud.uploaded_at, 
                   ud.verified_by_admin, ud.verification_notes, ua.username, ua.full_name
            FROM user_documents ud
            JOIN user_accounts ua ON ud.user_id = ua.id
            ORDER BY ud.uploaded_at DESC
        ''')
        rows = c.fetchall()
        conn.close()

        documents = []
        for r in rows:
            documents.append({
                'id': r['id'],
                'user_id': r['user_id'],
                'username': r['username'],
                'full_name': r['full_name'],
                'document_type': r['document_type'],
                'file_path': r['file_path'],
                'uploaded_at': r['uploaded_at'],
                'verified': r['verified_by_admin'] == 1,
                'verification_notes': r['verification_notes']
            })

        return jsonify({'success': True, 'documents': documents, 'count': len(documents)})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500


@app.route('/admin/document/<int:doc_id>/verify', methods=['POST'])
def admin_verify_document(doc_id):
    """Admin verifies a user document."""
    allowed, auth_result, status = require_roles(request, {'admin'})
    if not allowed:
        return auth_result, status

    data = request.get_json() if request.is_json else request.form
    password = data.get('password', '')
    if not verify_admin_password(password):
        return jsonify({'success': False, 'message': 'Invalid admin password'}), 403

    verification_notes = data.get('verification_notes', '')

    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute('''
            UPDATE user_documents
            SET verified_by_admin = 1, verification_notes = ?
            WHERE id = ?
        ''', (verification_notes, doc_id))
        conn.commit()
        updated = c.rowcount
        conn.close()

        if updated:
            return jsonify({'success': True, 'message': 'Document verified'})
        else:
            return jsonify({'success': False, 'message': 'Document not found'}), 404
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500


@app.route('/pay-application/<int:app_id>', methods=['POST'])
def pay_application(app_id):
    """Record a payment for a loan application (saves to payments table)."""
    if not verify_session(request):
        return jsonify({'success': False, 'message': 'Authentication required'}), 401

    data = request.get_json() if request.is_json else request.form
    try:
        months = int(data.get('months', 1))
    except:
        months = 1
    try:
        amount = float(data.get('amount', 0))
    except:
        amount = 0.0
    payer = data.get('payer', '')
    timestamp = datetime.now().isoformat()

    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        
        # Record the payment
        c.execute('''INSERT INTO payments (application_id, timestamp, months_paid, amount_paid, payer)
                     VALUES (?, ?, ?, ?, ?)''', (app_id, timestamp, months, amount, payer))
        receipt_id = c.lastrowid  # Get the payment ID for receipt
        
        # Get full loan details for potential archiving
        c.execute('SELECT * FROM loan_applications WHERE id = ?', (app_id,))
        loan_row = c.fetchone()
        if not loan_row:
            conn.commit()
            conn.close()
            return jsonify({'success': False, 'message': 'Loan application not found'}), 404

        if (loan_row['recommendation'] or '').lower() != 'approved':
            conn.close()
            return jsonify({'success': False, 'message': 'Payments are only allowed after admin approval'}), 403
        
        loan_amount = float(loan_row['amount']) if loan_row['amount'] else 0.0
        
        c.execute('SELECT COALESCE(SUM(amount_paid), 0) FROM payments WHERE application_id = ?', (app_id,))
        total_paid_row = c.fetchone()
        total_paid = float(total_paid_row[0]) if total_paid_row else 0.0
        
        balance = loan_amount - total_paid
        fully_paid = balance <= 0
        
        # If fully paid, archive the record
        if fully_paid:
            paid_date = datetime.now().isoformat()
            # Archive to paid_loans_archive
            c.execute('''INSERT INTO paid_loans_archive 
                        (original_id, timestamp, full_name, contact, amount, months, interest_rate, monthly_payment, total_paid, paid_date, verification)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                      (loan_row['id'], loan_row['timestamp'], loan_row['full_name'], loan_row['contact'], 
                       loan_row['amount'], loan_row['months'], loan_row['interest_rate'], loan_row['monthly_payment'], 
                       total_paid, paid_date, loan_row['verification'] if 'verification' in loan_row.keys() else None))
            
            # Delete from active tables
            c.execute('DELETE FROM payments WHERE application_id = ?', (app_id,))
            c.execute('DELETE FROM loan_applications WHERE id = ?', (app_id,))
            print(f"Archived fully paid loan application ID: {app_id} (Name: {loan_row['full_name']}, Total Paid: {total_paid})")
            conn.commit()
            conn.close()
            return jsonify({
                'success': True, 
                'message': 'Payment recorded. Loan fully paid - record archived to Paid Records!', 
                'application_id': app_id, 
                'months': months, 
                'amount': amount,
                'fully_paid': True,
                'archived': True
            })
        
        conn.commit()
        conn.close()
        return jsonify({
            'success': True, 
            'message': 'Payment recorded', 
            'application_id': app_id, 
            'months': months, 
            'amount': amount,
            'fully_paid': False,
            'new_balance': balance,
            'receipt_id': receipt_id
        })
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500


@app.route('/payments/<int:app_id>', methods=['GET'])
def get_payments(app_id):
    """Return list of payments for a given application id."""
    if not verify_session(request):
        return jsonify({'success': False, 'message': 'Authentication required'}), 401

    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute('''SELECT id, application_id, timestamp, months_paid, amount_paid, payer
                     FROM payments WHERE application_id = ? ORDER BY id DESC''', (app_id,))
        rows = c.fetchall()
        payments = []
        for r in rows:
            payments.append({
                'id': r['id'],
                'application_id': r['application_id'],
                'timestamp': r['timestamp'],
                'months_paid': r['months_paid'],
                'amount_paid': r['amount_paid'],
                'payer': r['payer']
            })
        conn.close()
        return jsonify({'success': True, 'payments': payments})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500


@app.route('/paid-loans-archive', methods=['GET'])
def get_paid_loans_archive():
    """Return list of fully paid loans from archive."""
    if not verify_session(request):
        return jsonify({'success': False, 'message': 'Authentication required'}), 401

    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute('''SELECT id, original_id, timestamp, full_name, contact, amount, months, 
                     interest_rate, monthly_payment, total_paid, paid_date
                     FROM paid_loans_archive ORDER BY paid_date DESC''')
        rows = c.fetchall()
        archive = []
        for r in rows:
            archive.append({
                'id': r['id'],
                'original_id': r['original_id'],
                'timestamp': r['timestamp'],
                'full_name': r['full_name'],
                'contact': r['contact'],
                'amount': r['amount'],
                'months': r['months'],
                'interest_rate': r['interest_rate'],
                'monthly_payment': r['monthly_payment'],
                'total_paid': r['total_paid'],
                'paid_date': r['paid_date']
            })
        conn.close()
        return jsonify({'success': True, 'archive': archive, 'count': len(archive)})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500


@app.route('/delete-paid-loan/<int:archive_id>', methods=['DELETE'])
def delete_paid_loan(archive_id):
    """Delete a record from the paid loans archive."""
    if not verify_session(request):
        return jsonify({'success': False, 'message': 'Authentication required'}), 401

    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute('DELETE FROM paid_loans_archive WHERE id = ?', (archive_id,))
        conn.commit()
        conn.close()
        return jsonify({'success': True, 'message': 'Record deleted from archive'})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500


# ============= DATABASE BACKUP/RESTORE =============

@app.route('/backup-database', methods=['GET'])
def backup_database():
    """Create and download a backup of the database."""
    if not verify_session(request):
        return jsonify({'success': False, 'message': 'Authentication required'}), 401

    try:
        if not os.path.exists(DB_PATH):
            return jsonify({'success': False, 'message': 'Database file not found'}), 404

        # Create backup filename with timestamp
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        backup_filename = f'loan_backup_{timestamp}.db'

        # Send the database file as download
        return send_file(
            DB_PATH,
            mimetype='application/x-sqlite3',
            as_attachment=True,
            download_name=backup_filename
        )
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500


@app.route('/restore-database', methods=['POST'])
def restore_database():
    """Restore database from uploaded backup file."""
    allowed, auth_result, status = require_roles(request, {'admin'})
    if not allowed:
        return auth_result, status

    # Verify admin password
    password = request.form.get('password', '')
    if not verify_admin_password(password):
        return jsonify({'success': False, 'message': 'Invalid admin password'}), 403

    if 'backup_file' not in request.files:
        return jsonify({'success': False, 'message': 'No backup file provided'}), 400

    file = request.files['backup_file']
    if file.filename == '':
        return jsonify({'success': False, 'message': 'No file selected'}), 400

    # Check file extension
    if not file.filename.lower().endswith('.db'):
        return jsonify({'success': False, 'message': 'Invalid file type. Please upload a .db file'}), 400

    try:
        # Save uploaded file to temp location
        temp_path = os.path.join(tempfile.gettempdir(), 'temp_restore.db')
        file.save(temp_path)

        # Validate it's a valid SQLite database
        try:
            test_conn = sqlite3.connect(temp_path)
            test_cursor = test_conn.cursor()
            # Check if required tables exist
            test_cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='loan_applications'")
            if not test_cursor.fetchone():
                test_conn.close()
                os.remove(temp_path)
                return jsonify({'success': False, 'message': 'Invalid backup: missing loan_applications table'}), 400
            test_conn.close()
        except sqlite3.Error as e:
            if os.path.exists(temp_path):
                os.remove(temp_path)
            return jsonify({'success': False, 'message': f'Invalid database file: {str(e)}'}), 400

        # Create backup of current database before restoring
        if os.path.exists(DB_PATH):
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            auto_backup_path = os.path.join(REPORTS_FOLDER, f'pre_restore_backup_{timestamp}.db')
            shutil.copy2(DB_PATH, auto_backup_path)

        # Replace current database with restored one
        shutil.copy2(temp_path, DB_PATH)
        os.remove(temp_path)

        return jsonify({
            'success': True, 
            'message': 'Database restored successfully! The page will reload.',
            'auto_backup': auto_backup_path if os.path.exists(DB_PATH) else None
        })
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500


@app.route('/database-info', methods=['GET'])
def database_info():
    """Get database statistics for backup info."""
    if not verify_session(request):
        return jsonify({'success': False, 'message': 'Authentication required'}), 401

    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        
        # Count records in each table
        c.execute('SELECT COUNT(*) FROM loan_applications')
        active_loans = c.fetchone()[0]
        
        c.execute('SELECT COUNT(*) FROM payments')
        payments = c.fetchone()[0]
        
        c.execute('SELECT COUNT(*) FROM paid_loans_archive')
        archived = c.fetchone()[0]
        
        conn.close()

        # Get file info
        file_size = os.path.getsize(DB_PATH) if os.path.exists(DB_PATH) else 0
        file_modified = datetime.fromtimestamp(os.path.getmtime(DB_PATH)).strftime('%Y-%m-%d %H:%M:%S') if os.path.exists(DB_PATH) else 'N/A'

        return jsonify({
            'success': True,
            'info': {
                'active_loans': active_loans,
                'payments': payments,
                'archived_loans': archived,
                'file_size': file_size,
                'file_size_formatted': f'{file_size / 1024:.1f} KB' if file_size < 1024*1024 else f'{file_size / (1024*1024):.2f} MB',
                'last_modified': file_modified
            }
        })
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500


@app.route('/dashboard-stats', methods=['GET'])
def dashboard_stats():
    """Return statistics for dashboard charts."""
    if not verify_session(request):
        return jsonify({'success': False, 'message': 'Authentication required'}), 401

    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        
        now = datetime.now()
        
        # Get loan status counts (on-time vs overdue)
        c.execute('SELECT id, timestamp, months, monthly_payment, start_date FROM loan_applications')
        loans = c.fetchall()
        on_time_count = 0
        overdue_count = 0
        
        for loan in loans:
            # Calculate total paid
            c.execute('SELECT COALESCE(SUM(amount_paid), 0) FROM payments WHERE application_id = ?', (loan['id'],))
            total_paid = float(c.fetchone()[0] or 0)
            
            # Determine overdue status
            start_date_str = loan['start_date'] or loan['timestamp']
            try:
                if start_date_str:
                    start_date = datetime.fromisoformat(start_date_str.replace('Z', '+00:00').split('+')[0])
                    months_since = max(0, (now.year - start_date.year) * 12 + (now.month - start_date.month))
                    expected_payments = min(months_since, loan['months'] or 0)
                    expected_amount = expected_payments * (float(loan['monthly_payment']) if loan['monthly_payment'] else 0)
                    
                    if total_paid < expected_amount and expected_payments > 0:
                        overdue_count += 1
                    else:
                        on_time_count += 1
                else:
                    on_time_count += 1
            except:
                on_time_count += 1
        
        # Get monthly collections (last 6 months)
        collections = []
        for i in range(5, -1, -1):
            month_date = now - timedelta(days=i*30)
            month_start = month_date.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            if month_date.month == 12:
                month_end = month_date.replace(year=month_date.year+1, month=1, day=1)
            else:
                month_end = month_date.replace(month=month_date.month+1, day=1)
            
            c.execute('''SELECT COALESCE(SUM(amount_paid), 0) FROM payments 
                        WHERE timestamp >= ? AND timestamp < ?''',
                     (month_start.isoformat(), month_end.isoformat()))
            amount = float(c.fetchone()[0] or 0)
            collections.append({
                'month': month_start.strftime('%b'),
                'amount': amount
            })
        
        # Get payment trends (count of payments per month)
        trends = []
        for i in range(5, -1, -1):
            month_date = now - timedelta(days=i*30)
            month_start = month_date.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            if month_date.month == 12:
                month_end = month_date.replace(year=month_date.year+1, month=1, day=1)
            else:
                month_end = month_date.replace(month=month_date.month+1, day=1)
            
            c.execute('''SELECT COUNT(*) FROM payments 
                        WHERE timestamp >= ? AND timestamp < ?''',
                     (month_start.isoformat(), month_end.isoformat()))
            count = c.fetchone()[0] or 0
            trends.append({
                'month': month_start.strftime('%b'),
                'count': count
            })
        
        conn.close()
        
        return jsonify({
            'success': True,
            'data': {
                'status': {'on_time': on_time_count, 'overdue': overdue_count},
                'collections': collections,
                'trends': trends
            }
        })
    except Exception as e:
        print(f"Dashboard stats error: {e}")
        return jsonify({'success': False, 'message': str(e)}), 500


@app.route('/application-analysis/<int:app_id>', methods=['GET'])
def application_analysis(app_id):
    """Return decision explanation, recommendation, and risk/fraud indicators."""
    if not verify_session(request):
        return jsonify({'success': False, 'message': 'Authentication required'}), 401
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute('''
            SELECT id, full_name, recommendation, confidence_score, risk_level, risk_score,
                   validation_issues, fraud_flags, decision_explanation, model_version, model_raw_json
            FROM loan_applications WHERE id = ?
        ''', (app_id,))
        row = c.fetchone()
        conn.close()
        if not row:
            return jsonify({'success': False, 'message': 'Application not found'}), 404

        def parse_json_value(raw, fallback):
            try:
                return json.loads(raw) if raw else fallback
            except Exception:
                return fallback

        return jsonify({
            'success': True,
            'analysis': {
                'id': row['id'],
                'full_name': row['full_name'],
                'recommendation': row['recommendation'] or 'pending',
                'confidence_score': float(row['confidence_score']) if row['confidence_score'] is not None else 0.0,
                'risk_level': row['risk_level'] or 'medium',
                'risk_score': float(row['risk_score']) if row['risk_score'] is not None else 0.0,
                'validation_issues': parse_json_value(row['validation_issues'], []),
                'fraud_flags': parse_json_value(row['fraud_flags'], []),
                'decision_explanation': parse_json_value(row['decision_explanation'], []),
                'model_version': row['model_version'] or MODEL_DEFAULTS['version'],
                'model_raw': parse_json_value(row['model_raw_json'], {})
            }
        })
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500


@app.route('/model-performance', methods=['GET'])
def model_performance():
    """Return model monitoring metrics for approvals/rejections and consistency."""
    if not verify_session(request):
        return jsonify({'success': False, 'message': 'Authentication required'}), 401

    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()

        c.execute('SELECT recommendation, risk_score FROM loan_applications')
        rows = c.fetchall()
        total = len(rows)

        approved = sum(1 for r in rows if (r['recommendation'] or '') == 'approved')
        rejected = sum(1 for r in rows if (r['recommendation'] or '') == 'rejected')
        review = sum(1 for r in rows if (r['recommendation'] or '') in {'requires_further_review', 'pending'})

        c.execute('SELECT COUNT(*) FROM paid_loans_archive')
        paid_loans = int(c.fetchone()[0] or 0)

        consistency_hits = 0
        for r in rows:
            risk = float(r['risk_score'] or 0)
            rec = r['recommendation'] or 'pending'
            if risk <= 35 and rec == 'approved':
                consistency_hits += 1
            elif risk >= 65 and rec == 'rejected':
                consistency_hits += 1
            elif 35 < risk < 65 and rec in {'requires_further_review', 'pending'}:
                consistency_hits += 1

        consistency = (consistency_hits / total * 100.0) if total else 0.0
        approval_rate = (approved / total * 100.0) if total else 0.0
        rejection_rate = (rejected / total * 100.0) if total else 0.0
        review_rate = (review / total * 100.0) if total else 0.0
        resolved_total = paid_loans + rejected
        proxy_accuracy = (paid_loans / resolved_total * 100.0) if resolved_total else 0.0

        cfg = get_model_config()
        conn.close()

        return jsonify({
            'success': True,
            'metrics': {
                'total_active_predictions': total,
                'approval_rate': round(approval_rate, 2),
                'rejection_rate': round(rejection_rate, 2),
                'review_rate': round(review_rate, 2),
                'prediction_consistency': round(consistency, 2),
                'proxy_prediction_accuracy': round(proxy_accuracy, 2),
                'active_model_version': cfg.get('version')
            }
        })
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500


@app.route('/admin/users', methods=['GET', 'POST'])
def admin_users():
    """Manage system user accounts (admin only)."""
    allowed, auth_result, status = require_roles(request, {'admin'})
    if not allowed:
        return auth_result, status

    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()

        if request.method == 'GET':
            c.execute('''
                SELECT id, username, role, full_name, is_active, created_at, last_login
                FROM user_accounts ORDER BY id ASC
            ''')
            users = [dict(row) for row in c.fetchall()]
            conn.close()
            return jsonify({'success': True, 'users': users})

        data = request.get_json() or {}
        username = (data.get('username') or '').strip()
        password = data.get('password') or ''
        role = (data.get('role') or '').strip()
        full_name = (data.get('full_name') or '').strip() or username

        if not username or not password:
            conn.close()
            return jsonify({'success': False, 'message': 'Username and password are required'}), 400
        if role not in ROLES:
            conn.close()
            return jsonify({'success': False, 'message': 'Invalid role'}), 400

        c.execute('SELECT id FROM user_accounts WHERE username = ?', (username,))
        if c.fetchone():
            conn.close()
            return jsonify({'success': False, 'message': 'Username already exists'}), 409

        c.execute(
            '''INSERT INTO user_accounts (username, password_hash, role, full_name, is_active, created_at)
               VALUES (?, ?, ?, ?, 1, ?)''',
            (username, hash_password(password), role, full_name, datetime.now().isoformat())
        )
        conn.commit()
        new_id = c.lastrowid
        conn.close()
        return jsonify({'success': True, 'message': 'User created', 'id': new_id})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500


@app.route('/admin/users-page', methods=['GET'])
def admin_users_page():
    """Render admin user approval page."""
    allowed, auth_result, status = require_roles(request, {'admin'})
    if not allowed:
        return auth_result, status
    return render_template('admin_users.html')


@app.route('/admin/reports-page', methods=['GET'])
def admin_reports_page():
    """Render admin reports page."""
    allowed, auth_result, status = require_roles(request, {'admin'})
    if not allowed:
        return auth_result, status
    return render_template('admin_reports.html')


@app.route('/admin/users/<int:user_id>/approval', methods=['POST'])
def admin_user_approval(user_id):
    """Approve or reject a newly registered user account (admin only)."""
    allowed, auth_result, status = require_roles(request, {'admin'})
    if not allowed:
        return auth_result, status

    data = request.get_json() if request.is_json else request.form
    password = (data.get('password') or '').strip()
    action = (data.get('action') or '').strip().lower()

    if not verify_admin_password(password):
        return jsonify({'success': False, 'message': 'Invalid admin password'}), 403

    if action not in {'approve', 'reject'}:
        return jsonify({'success': False, 'message': 'Action must be approve or reject'}), 400

    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()

        c.execute('SELECT id, is_active, role FROM user_accounts WHERE id = ?', (user_id,))
        user = c.fetchone()
        if not user:
            conn.close()
            return jsonify({'success': False, 'message': 'User not found'}), 404

        if action == 'approve':
            c.execute('UPDATE user_accounts SET is_active = 1 WHERE id = ?', (user_id,))
            message = 'User account approved'
        else:
            c.execute('UPDATE user_accounts SET is_active = 0 WHERE id = ?', (user_id,))
            message = 'User account rejected'

        conn.commit()
        conn.close()
        return jsonify({'success': True, 'message': message})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500


@app.route('/admin/approved-loan-applicants', methods=['GET'])
def admin_approved_loan_applicants():
    """List approved-loan applicants with optional search (admin only)."""
    allowed, auth_result, status = require_roles(request, {'admin'})
    if not allowed:
        return auth_result, status

    search = (request.args.get('search') or '').strip().lower()

    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()

        base_sql = '''
            SELECT
                la.user_id,
                ua.username,
                COALESCE(ua.full_name, la.full_name, ua.username) AS full_name,
                COUNT(*) AS approved_count,
                MAX(COALESCE(la.timestamp, la.start_date)) AS latest_approved_at
            FROM loan_applications la
            JOIN user_accounts ua ON ua.id = la.user_id
            WHERE ua.role = 'applicant'
              AND ua.is_active = 1
              AND LOWER(COALESCE(la.recommendation, '')) = 'approved'
        '''
        params = []
        if search:
            base_sql += '''
              AND (
                    LOWER(COALESCE(ua.full_name, la.full_name, '')) LIKE ?
                 OR LOWER(COALESCE(ua.username, '')) LIKE ?
              )
            '''
            like_search = f"%{search}%"
            params.extend([like_search, like_search])

        base_sql += '''
            GROUP BY la.user_id, ua.username, ua.full_name
            ORDER BY latest_approved_at DESC
        '''

        c.execute(base_sql, params)
        applicants = [dict(row) for row in c.fetchall()]
        conn.close()

        return jsonify({'success': True, 'applicants': applicants})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500


@app.route('/admin/pending-applicants', methods=['GET'])
def admin_pending_applicants():
    """List pending applicants (pending user accounts or pending loan applications)."""
    allowed, auth_result, status = require_roles(request, {'admin'})
    if not allowed:
        return auth_result, status

    search = (request.args.get('search') or '').strip().lower()

    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()

        base_sql = '''
            SELECT
                ua.id AS user_id,
                ua.username,
                COALESCE(ua.full_name, ua.username) AS full_name,
                ua.is_active,
                SUM(CASE WHEN LOWER(COALESCE(la.recommendation, '')) IN ('pending', 'requires_further_review') THEN 1 ELSE 0 END) AS pending_count,
                MAX(CASE WHEN LOWER(COALESCE(la.recommendation, '')) IN ('pending', 'requires_further_review')
                         THEN COALESCE(la.timestamp, la.start_date)
                         ELSE NULL END) AS latest_pending_at
            FROM user_accounts ua
            LEFT JOIN loan_applications la ON la.user_id = ua.id
            WHERE ua.role = 'applicant'
        '''

        params = []
        if search:
            base_sql += '''
              AND (
                    LOWER(COALESCE(ua.full_name, '')) LIKE ?
                 OR LOWER(COALESCE(ua.username, '')) LIKE ?
              )
            '''
            like_search = f"%{search}%"
            params.extend([like_search, like_search])

        base_sql += '''
            GROUP BY ua.id, ua.username, ua.full_name, ua.is_active
            HAVING ua.is_active = 0 OR pending_count > 0
            ORDER BY latest_pending_at DESC, ua.id DESC
        '''

        c.execute(base_sql, params)
        applicants = [dict(row) for row in c.fetchall()]
        conn.close()

        return jsonify({'success': True, 'applicants': applicants})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500


@app.route('/admin/model-settings', methods=['GET', 'POST'])
def admin_model_settings():
    """View or update active model threshold settings (admin only)."""
    allowed, auth_result, status = require_roles(request, {'admin'})
    if not allowed:
        return auth_result, status

    if request.method == 'GET':
        return jsonify({'success': True, 'settings': get_model_config()})

    data = request.get_json() or {}
    try:
        approval_threshold = float(data.get('approval_threshold', MODEL_DEFAULTS['approval_threshold']))
        reject_threshold = float(data.get('reject_threshold', MODEL_DEFAULTS['reject_threshold']))
    except Exception:
        return jsonify({'success': False, 'message': 'Thresholds must be numeric'}), 400

    if approval_threshold >= reject_threshold:
        return jsonify({'success': False, 'message': 'Approval threshold must be below reject threshold'}), 400

    notes = (data.get('notes') or 'Manual threshold update').strip()

    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("UPDATE model_registry SET status = 'inactive' WHERE status = 'active'")
        c.execute(
            '''INSERT INTO model_registry (version, status, trained_at, notes, approval_threshold, reject_threshold, training_samples)
               VALUES (?, 'active', ?, ?, ?, ?, ?)''',
            (
                data.get('version') or get_model_config().get('version', MODEL_DEFAULTS['version']),
                datetime.now().isoformat(),
                notes,
                approval_threshold,
                reject_threshold,
                0
            )
        )
        conn.commit()
        conn.close()
        return jsonify({'success': True, 'message': 'Model settings updated', 'settings': get_model_config()})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500


@app.route('/admin/retrain-model', methods=['POST'])
def admin_retrain_model():
    """Register a model retraining event and activate the new version (admin only)."""
    allowed, auth_result, status = require_roles(request, {'admin'})
    if not allowed:
        return auth_result, status

    data = request.get_json() or {}
    samples = int(data.get('training_samples', 0) or 0)
    notes = (data.get('notes') or 'Retraining triggered by administrator').strip()
    cfg = get_model_config()

    version = data.get('version') or f"risk_model_v{datetime.now().strftime('%Y.%m.%d.%H%M')}"

    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("UPDATE model_registry SET status = 'inactive' WHERE status = 'active'")
        c.execute(
            '''INSERT INTO model_registry (version, status, trained_at, notes, approval_threshold, reject_threshold, training_samples)
               VALUES (?, 'active', ?, ?, ?, ?, ?)''',
            (
                version,
                datetime.now().isoformat(),
                notes,
                cfg.get('approval_threshold', MODEL_DEFAULTS['approval_threshold']),
                cfg.get('reject_threshold', MODEL_DEFAULTS['reject_threshold']),
                samples
            )
        )
        conn.commit()
        conn.close()
        return jsonify({
            'success': True,
            'message': 'Model retraining event recorded',
            'active_model': get_model_config()
        })
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500


@app.route('/authorize-admin', methods=['POST'])
def authorize_admin():
    """Verify admin password (used before sensitive actions)."""
    allowed, auth_result, status = require_roles(request, {'admin'})
    if not allowed:
        return jsonify({'authorized': False, 'message': auth_result.get_json().get('message', 'Authentication required')}), status

    data = {}
    if request.is_json:
        data = request.get_json()
    else:
        data = request.form

    password = data.get('password', '')
    if verify_admin_password(password):
        return jsonify({'authorized': True})
    return jsonify({'authorized': False, 'message': 'Invalid password'}), 403


@app.route('/delete-application/<int:app_id>', methods=['POST'])
def delete_application(app_id):
    """Delete a loan application by id (requires admin password)."""
    allowed, auth_result, status = require_roles(request, {'admin'})
    if not allowed:
        return auth_result, status

    data = request.get_json() if request.is_json else request.form
    password = data.get('password', '')
    if not verify_admin_password(password):
        return jsonify({'success': False, 'message': 'Invalid admin password'}), 403

    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute('DELETE FROM loan_applications WHERE id = ?', (app_id,))
        conn.commit()
        deleted = c.rowcount
        conn.close()
        if deleted:
            return jsonify({'success': True, 'deleted': deleted})
        else:
            return jsonify({'success': False, 'message': 'Not found'}), 404
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500


@app.route('/update-application/<int:app_id>', methods=['POST'])
def update_application(app_id):
    """Update loan application fields (requires admin password)."""
    allowed, auth_result, status = require_roles(request, {'admin'})
    if not allowed:
        return auth_result, status

    data = request.get_json() if request.is_json else request.form
    password = data.get('password', '')
    if not verify_admin_password(password):
        return jsonify({'success': False, 'message': 'Invalid admin password'}), 403

    # Allowed fields to update
    full_name = data.get('full_name')
    contact = data.get('contact')
    try:
        amount = float(data.get('amount')) if data.get('amount') is not None else None
    except:
        amount = None
    try:
        months = int(data.get('months')) if data.get('months') is not None else None
    except:
        months = None
    try:
        interest_rate = float(data.get('interest_rate')) if data.get('interest_rate') is not None else None
    except:
        interest_rate = None

    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        # Build dynamic update
        fields = []
        values = []
        if full_name is not None:
            fields.append('full_name = ?')
            values.append(full_name)
        if contact is not None:
            fields.append('contact = ?')
            values.append(contact)
        if amount is not None:
            fields.append('amount = ?')
            values.append(amount)
        if months is not None:
            fields.append('months = ?')
            values.append(months)
        if interest_rate is not None:
            fields.append('interest_rate = ?')
            values.append(interest_rate)

        if not fields:
            conn.close()
            return jsonify({'success': False, 'message': 'No fields to update'}), 400

        values.append(app_id)
        sql = f"UPDATE loan_applications SET {', '.join(fields)} WHERE id = ?"
        c.execute(sql, tuple(values))
        conn.commit()
        updated = c.rowcount
        conn.close()
        if updated:
            return jsonify({'success': True, 'updated': updated})
        else:
            return jsonify({'success': False, 'message': 'Not found or no change'}), 404
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500


@app.route('/admin/loan-applications/<int:app_id>/decision', methods=['POST'])
def admin_loan_application_decision(app_id):
    """Approve or reject a loan application (admin only)."""
    allowed, auth_result, status = require_roles(request, {'admin'})
    if not allowed:
        return auth_result, status

    data = request.get_json() if request.is_json else request.form
    password = (data.get('password') or '').strip()
    action = (data.get('action') or '').strip().lower()

    if not verify_admin_password(password):
        return jsonify({'success': False, 'message': 'Invalid admin password'}), 403

    if action not in {'approve', 'reject'}:
        return jsonify({'success': False, 'message': 'Action must be approve or reject'}), 400

    new_recommendation = 'approved' if action == 'approve' else 'rejected'

    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute('SELECT id, recommendation FROM loan_applications WHERE id = ?', (app_id,))
        application = c.fetchone()
        if not application:
            conn.close()
            return jsonify({'success': False, 'message': 'Loan application not found'}), 404

        current_recommendation = (application['recommendation'] or '').strip().lower()
        if current_recommendation in {'approved', 'rejected'}:
            conn.close()
            return jsonify({'success': False, 'message': 'Loan application already has a final decision'}), 409

        c.execute(
            'UPDATE loan_applications SET recommendation = ? WHERE id = ?',
            (new_recommendation, app_id)
        )
        conn.commit()
        conn.close()
        return jsonify({
            'success': True,
            'message': f'Loan application {new_recommendation}',
            'recommendation': new_recommendation,
        })
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/download-report/<filename>')
def download_report(filename):
    """Download a specific report"""
    if not verify_session(request):
        return jsonify({'success': False, 'message': 'Authentication required'}), 401
    
    try:
        # Security check to prevent directory traversal
        if '..' in filename or filename.startswith('/'):
            return jsonify({'success': False, 'message': 'Invalid filename'}), 400
        
        filepath = os.path.join(REPORTS_FOLDER, filename)
        
        if not os.path.exists(filepath):
            return jsonify({'success': False, 'message': 'Report not found'}), 404
        
        return send_file(
            filepath,
            as_attachment=True,
            download_name=filename,
            mimetype='application/pdf'
        )
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/cleanup-reports', methods=['POST'])
def cleanup_reports():
    """Clean up reports older than 30 days"""
    allowed, auth_result, status = require_roles(request, {'admin'})
    if not allowed:
        return auth_result, status
    
    try:
        cutoff_date = datetime.now() - timedelta(days=30)
        deleted_count = 0
        
        for filename in os.listdir(REPORTS_FOLDER):
            if filename.endswith('.pdf'):
                filepath = os.path.join(REPORTS_FOLDER, filename)
                file_mtime = datetime.fromtimestamp(os.path.getmtime(filepath))
                
                if file_mtime < cutoff_date:
                    os.remove(filepath)
                    deleted_count += 1
        
        return jsonify({
            'success': True,
            'message': f'Cleaned up {deleted_count} old reports',
            'deleted_count': deleted_count
        })
        
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

# ============= PROTECTED APPLICATION ROUTES =============
@app.route('/upload-reference', methods=['POST'])
def upload_reference():
    """Upload a reference document for specific document type"""
    if not verify_session(request):
        return jsonify({'success': False, 'message': 'Authentication required'}), 401
    
    try:
        document_type = request.form.get('document_type', DEFAULT_DOC_TYPE)
        
        if document_type not in DOCUMENT_TYPES:
            return jsonify({'success': False, 'message': 'Invalid document type'}), 400
        
        if 'reference_file' not in request.files:
            return jsonify({'success': False, 'message': 'No file uploaded'}), 400
        
        reference_file = request.files['reference_file']
        
        if reference_file.filename == '':
            return jsonify({'success': False, 'message': 'No file selected'}), 400
        
        if not allowed_file(reference_file.filename):
            return jsonify({'success': False, 'message': 'Invalid file type. Please upload JPG, PNG, or PDF.'}), 400
        
        # Check file size
        reference_file.seek(0, 2)
        file_size = reference_file.tell()
        reference_file.seek(0)
        
        if file_size > MAX_FILE_SIZE:
            return jsonify({'success': False, 'message': f'File too large ({file_size/1024/1024:.1f}MB > 5MB)'}), 400
        
        # Get the folder for this document type
        folder_path = get_reference_folder(document_type)
        
        # Clear existing reference files in this folder
        for f in os.listdir(folder_path):
            file_path = os.path.join(folder_path, f)
            try:
                if os.path.isfile(file_path):
                    os.unlink(file_path)
            except Exception as e:
                print(f"Error deleting file {file_path}: {e}")
        
        # Save reference file
        filename = secure_filename(reference_file.filename)
        filepath = os.path.join(folder_path, filename)
        reference_file.save(filepath)
        
        # Try to load the reference
        reference_data[document_type]['image'] = None
        reference_data[document_type]['features'] = None
        
        success = load_reference_license(document_type)
        
        if success:
            return jsonify({
                'success': True,
                'message': f'{document_type.replace("_", " ").title()} reference uploaded and loaded successfully',
                'filename': filename,
                'size': file_size,
                'has_reference': True,
                'document_type': document_type
            })
        else:
            # Delete the file if it couldn't be loaded
            if os.path.exists(filepath):
                os.remove(filepath)
            return jsonify({'success': False, 'message': f'Could not process {document_type} reference. Please check file format.'}), 400
        
    except Exception as e:
        print(f"Reference upload error: {str(e)}")
        return jsonify({'success': False, 'message': f'Internal server error: {str(e)}'}), 500

@app.route('/check-reference', methods=['GET'])
def check_reference():
    """Check if reference exists for a specific document type"""
    if not verify_session(request):
        return jsonify({'success': False, 'message': 'Authentication required'}), 401
    
    try:
        document_type = request.args.get('type', DEFAULT_DOC_TYPE)
        
        if document_type not in DOCUMENT_TYPES:
            return jsonify({'success': False, 'message': 'Invalid document type'}), 400
        
        folder_path = get_reference_folder(document_type)
        reference_files = [f for f in os.listdir(folder_path) 
                          if f.lower().endswith(('.png', '.jpg', '.jpeg', '.pdf'))]
        
        has_reference_file = len(reference_files) > 0
        
        # Always try to load reference if file exists
        if has_reference_file:
            reference_loaded = load_reference_license(document_type)
        else:
            reference_loaded = False
        
        return jsonify({
            'success': True,
            'has_reference': reference_loaded,
            'document_type': document_type,
            'reference_file': reference_files[0] if reference_files else None,
            'reference_loaded': reference_loaded
        })
        
    except Exception as e:
        print(f"Error checking reference: {e}")
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/verify', methods=['POST'])
def verify_license():
    """Verify document using reference comparison"""
    if not verify_session(request):
        return jsonify({'success': False, 'message': 'Authentication required'}), 401
    
    try:
        # Get document type from form or use default
        document_type = request.form.get('document_type', DEFAULT_DOC_TYPE)
        
        if document_type not in DOCUMENT_TYPES:
            return jsonify({'success': False, 'message': 'Invalid document type'}), 400
        
        if 'license_file' not in request.files:
            return jsonify({'success': False, 'message': 'No file uploaded'}), 400
        
        license_file = request.files['license_file']
        
        if license_file.filename == '':
            return jsonify({'success': False, 'message': 'No file selected'}), 400
        
        if not allowed_file(license_file.filename):
            return jsonify({'success': False, 'message': 'Invalid file type. Please upload JPG, PNG, or PDF.'}), 400
        
        # Check file size
        license_file.seek(0, 2)
        file_size = license_file.tell()
        license_file.seek(0)
        
        if file_size > MAX_FILE_SIZE:
            return jsonify({'success': False, 'message': f'File too large ({file_size/1024/1024:.1f}MB > 5MB)'}), 400
        
        # Save and process file
        with tempfile.TemporaryDirectory() as temp_dir:
            license_path = os.path.join(temp_dir, secure_filename(license_file.filename))
            license_file.save(license_path)
            
            # Analyze using reference comparison
            result = analyze_with_comparison(license_path, document_type)
            
            # Add metadata
            result['success'] = True
            result['timestamp'] = datetime.now().isoformat()
            
            # Provide helpful message based on result
            doc_name = document_type.replace('_', ' ').title()
            
            if result['has_reference']:
                if result['is_authentic']:
                    if result['confidence'] > 85:
                        result['message'] = f'High confidence - {doc_name} closely matches reference'
                    elif result['confidence'] > 75:
                        result['message'] = f'Good confidence - {doc_name} similar to reference'
                    elif result['confidence'] > 65:
                        result['message'] = f'Moderate confidence - Some differences from {doc_name.lower()} reference'
                    elif result['confidence'] > 55:
                        result['message'] = f'Low confidence - Multiple differences detected but appears authentic'
                    else:
                        result['message'] = f'Very low confidence - Significant differences detected'
                else:
                    if result['confidence'] < 40:
                        result['message'] = f'High suspicion - Significant differences from {doc_name.lower()} reference'
                    elif result['confidence'] < 55:
                        result['message'] = f'Moderate suspicion - Does not match {doc_name.lower()} pattern'
                    else:
                        result['message'] = f'Suspicious - Multiple issues compared to {doc_name.lower()} reference'
            else:
                if result['is_authentic']:
                    result['message'] = f'Basic check passed - No {doc_name.lower()} reference available for comparison'
                else:
                    result['message'] = f'Failed basic check - No {doc_name.lower()} reference available'
            
            # Debug information
            print(f"\n=== VERIFICATION RESULT ===")
            print(f"Document Type: {document_type}")
            print(f"Authentic: {result['is_authentic']}")
            print(f"Confidence: {result['confidence']:.1f}%")
            print(f"Similarity: {result['similarity_score']:.1f}%")
            print(f"Has Reference: {result['has_reference']}")
            print(f"Issues: {result['issues']}")
            print("=========================\n")
            
            return jsonify(result)
            
    except Exception as e:
        print(f"Verification error: {str(e)}")
        return jsonify({'success': False, 'message': f'Internal server error: {str(e)}'}), 500

@app.route('/get-document-types', methods=['GET'])
def get_document_types():
    """Get list of available document types"""
    if not verify_session(request):
        return jsonify({'success': False, 'message': 'Authentication required'}), 401
    
    return jsonify({
        'success': True,
        'document_types': DOCUMENT_TYPES,
        'active_types': {
            'drivers_license': True,
            'national_id': True,
            'passport': True
        }
    })

@app.route('/health', methods=['GET'])
def health_check():
    """Enhanced health check with document types"""
    if not verify_session(request):
        return jsonify({'success': False, 'message': 'Authentication required'}), 401
    
    status = {
        'status': 'healthy',
        'timestamp': datetime.now().isoformat(),
        'service': 'Multi-ID Verification System',
        'version': '3.1.0',
        'supported_documents': DOCUMENT_TYPES,
        'references_loaded': {}
    }
    
    # Check each document type
    for doc_type in DOCUMENT_TYPES:
        folder_path = get_reference_folder(doc_type)
        files = [f for f in os.listdir(folder_path) 
                if f.lower().endswith(('.png', '.jpg', '.jpeg', '.pdf'))]
        status['references_loaded'][doc_type] = len(files) > 0
    
    return jsonify(status)

@app.route('/reset-all', methods=['POST'])
def reset_all_references():
    """Reset all references (optional endpoint)"""
    if not verify_session(request):
        return jsonify({'success': False, 'message': 'Authentication required'}), 401
    
    try:
        for doc_type in DOCUMENT_TYPES:
            folder_path = get_reference_folder(doc_type)
            # Clear folder
            for filename in os.listdir(folder_path):
                file_path = os.path.join(folder_path, filename)
                try:
                    if os.path.isfile(file_path):
                        os.unlink(file_path)
                except Exception as e:
                    print(f"Error deleting {file_path}: {e}")
            
            # Reset in-memory data
            reference_data[doc_type] = {'image': None, 'features': None}
        
        return jsonify({
            'success': True,
            'message': 'All references cleared',
            'cleared_types': DOCUMENT_TYPES
        })
        
    except Exception as e:
        print(f"Reset error: {e}")
        return jsonify({'success': False, 'message': str(e)}), 500

# ============= ERROR HANDLERS =============
@app.errorhandler(404)
def not_found_error(error):
    return jsonify({'success': False, 'message': 'Page not found'}), 404

@app.errorhandler(500)
def internal_error(error):
    return jsonify({'success': False, 'message': 'Internal server error'}), 500

# ============= APPLICATION STARTUP =============
if __name__ == '__main__':
    print("=" * 70)
    print("MULTI-DOCUMENT VERIFICATION SYSTEM v3.1")
    print("IMPROVED DETECTION VERSION")
    print("=" * 70)
    print("Supported Document Types:")
    for doc_type in DOCUMENT_TYPES:
        print(f"  • {doc_type.replace('_', ' ').title()}")
    print("\nIMPROVEMENTS:")
    print("• Lowered similarity thresholds for better detection")
    print("• Added partial keyword matching")
    print("• More lenient image comparison")
    print("• Better OCR error handling")
    print("\nINSTRUCTIONS:")
    print("1. Place authentic documents in respective reference folders:")
    for doc_type in DOCUMENT_TYPES:
        folder = get_reference_folder(doc_type)
        print(f"   - {doc_type.replace('_', ' ').title()}: {folder}")
    print("2. Supported formats: JPG, PNG, PDF")
    print("3. Run the application")
    print("4. Open http://localhost:5000 in your browser")
    print("5. Login with any seeded account:")
    print("   - admin / jethro123 (admin)")
    print("   - officer / officer123 (applicant)")
    print("6. Select document type and upload documents to verify")
    print("7. Generate PDF reports for documentation")
    print("\n" + "-" * 70)
    
    # Auto-load references on startup
    for doc_type in DOCUMENT_TYPES:
        folder_path = get_reference_folder(doc_type)
        reference_files = [f for f in os.listdir(folder_path) 
                          if f.lower().endswith(('.png', '.jpg', '.jpeg', '.pdf'))]
        
        if reference_files:
            print(f"\n✓ Found {len(reference_files)} reference file(s) for {doc_type.replace('_', ' ').title()}")
            print(f"  Main reference: {reference_files[0]}")
            
            if load_reference_license(doc_type):
                print(f"  ✓ {doc_type.replace('_', ' ').title()} reference loaded successfully")
            else:
                print(f"  ✗ Could not load {doc_type.replace('_', ' ').title()} reference")
        else:
            print(f"\n⚠ No reference found for {doc_type.replace('_', ' ').title()}")
            print(f"  Add authentic document to: {folder_path}")
    
    print("\n" + "-" * 70)
    print("Starting server on http://localhost:5000")
    print("Login credentials: admin / jethro123")
    print("Press Ctrl+C to stop the server")
    print("=" * 70)
    
    app.run(debug=True, host='0.0.0.0', port=5000)