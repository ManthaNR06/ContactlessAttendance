import os
import cv2
import dlib
import numpy as np
import base64
import io
import uuid
import math
import pyodbc
import qrcode
from functools import wraps # Added for the decorator
from flask import Flask, render_template, request, jsonify, session, redirect, url_for

# --- CONFIGURATION ---
# IMPORTANT: Update this every time you restart ngrok!
PUBLIC_URL = "https://uncomprehended-apollo-outfly.ngrok-free.dev" 

# --- DLIB ENGINE SETUP ---
model_dir = r'C:\Users\manth\AppData\Local\Programs\Python\Python312\Lib\site-packages\face_recognition_models\models'
face_detector = dlib.get_frontal_face_detector()
shape_predictor = dlib.shape_predictor(os.path.join(model_dir, "shape_predictor_68_face_landmarks.dat"))
face_encoder = dlib.face_recognition_model_v1(os.path.join(model_dir, "dlib_face_recognition_resnet_model_v1.dat"))

def get_face_encoding_direct(image_path):
    if not os.path.exists(image_path):
        print(f"DEBUG: File MISSING at {image_path}")
        return None
    img = cv2.imread(image_path)
    if img is None: 
        print(f"DEBUG: CV2 failed to read {image_path}")
        return None
    img_rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    faces = face_detector(img_rgb, 1)
    if len(faces) == 0: 
        print(f"DEBUG: No face found in {image_path}. Check lighting!")
        return None
    shape = shape_predictor(img_rgb, faces[0])
    return np.array(face_encoder.compute_face_descriptor(img_rgb, shape))

app = Flask(__name__)
app.secret_key = "supersecretkey123"

recent_tokens = [] 

# --- SECURITY DECORATOR ---
# This defines the "login_required" logic used in your routes
def login_required(role="any"):
    def wrapper(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if 'user_role' not in session:
                return redirect(url_for('login'))
            if role != "any" and session.get('user_role') != role:
                return "Access Denied: Unauthorized Role", 403
            return f(*args, **kwargs)
        return decorated_function
    return wrapper

def get_db_connection():
    return pyodbc.connect(
        r'DRIVER={SQL Server};'
        r'SERVER=MANTHAN\SQLEXPRESS;' 
        r'DATABASE=ContactlessAttendanceDB;'
        r'Trusted_Connection=yes;'
    )

def calculate_distance(lat1, lon1, lat2, lon2):
    R = 6371000 
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2)**2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/teacher', methods=['GET', 'POST'])
@login_required(role="Teacher")
def teacher_page():
    if request.method == 'POST':
        subject = request.form.get('subject')
        return redirect(url_for('qr_display', subject=subject))
    return render_template('teacher_selection.html')

@app.route('/teacher/qr/<subject>')
@login_required(role="Teacher")
def qr_display(subject):
    return render_template('teacher_qr.html', subject=subject)

@app.route('/generate_qr_api')
def generate_qr_api():
    global recent_tokens
    subject = request.args.get('subject', 'General')
    new_token = f"{uuid.uuid4()}|{subject}"
    recent_tokens.append(new_token)
    if len(recent_tokens) > 5:
        recent_tokens.pop(0)
    
    full_url = f"{PUBLIC_URL}/auto_verify/{new_token}"
    
    qr = qrcode.QRCode(version=1, box_size=10, border=5)
    qr.add_data(full_url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buffered = io.BytesIO()
    img.save(buffered)
    qr_img_data = base64.b64encode(buffered.getvalue()).decode()
    return jsonify({"qr_code": qr_img_data})

@app.route('/auto_verify/<token>')
def auto_verify(token):
    return render_template('auto_verify.html', token=token)

@app.route('/register_face', methods=['GET', 'POST'])
def register_face():
    if request.method == 'GET':
        return render_template('register_face.html')

    data = request.json
    fullname = data.get('fullname')
    prn = data.get('prn')
    rollno = data.get('rollno')
    password = data.get('password')
    fingerprint = data.get('device_fingerprint')
    image_data = data.get('image')

    try:
        header, encoded = image_data.split(",", 1)
        face_path = f"static/faces/{prn}.jpg"
        if not os.path.exists('static/faces'):
            os.makedirs('static/faces')
        with open(face_path, "wb") as f:
            f.write(base64.b64decode(encoded))

        conn = get_db_connection()
        cursor = conn.cursor()
        # Added 'Student' as the default role for new registrations
        cursor.execute("""
            INSERT INTO Students (StudentID, Password, StudentName, DeviceID, RollNo, PRN, Role) 
            VALUES (?, ?, ?, ?, ?, ?, ?)""", 
            (prn, password, fullname, fingerprint, rollno, prn, 'Student'))
        conn.commit()
        conn.close()
        return jsonify({"status": "success", "message": "Account created!"})
    except pyodbc.IntegrityError:
        return jsonify({"status": "error", "message": "Roll Number or PRN already registered!"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})

@app.route('/verify_attendance', methods=['POST'])
def verify_attendance():
    global recent_tokens
    data = request.json
    student_id = session.get('student_id') or data.get('prn')
    current_device = data.get('device_fingerprint')
    full_token = data.get('token')
    lat = data.get('latitude')
    lon = data.get('longitude')

    if not student_id:
        return jsonify({"status": "fail", "message": "Identity missing! Please login first."})

    if full_token not in recent_tokens:
        return jsonify({"status": "fail", "message": "QR Expired!"})

    scanned_token, subject = full_token.split("|", 1) if "|" in full_token else (full_token, "General")

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT StudentName, DeviceID FROM Students WHERE StudentID = ?", (student_id,))
    user = cursor.fetchone()
    if not user:
        conn.close()
        return jsonify({"status": "fail", "message": "PRN not registered!"})
    
    student_name, stored_device = user[0], user[1]

    if stored_device and stored_device != current_device:
        conn.close()
        return jsonify({"status": "fail", "message": "Security Alert: Unauthorized Device!"})

    try:
        header, encoded = data.get('image').split(",", 1)
        image_bytes = base64.b64decode(encoded)
        face_path = f"static/faces/{student_id}.jpg"
        
        if not os.path.exists(face_path):
            conn.close()
            return jsonify({"status": "fail", "message": "Registered photo missing!"})
            
        student_master_encoding = get_face_encoding_direct(face_path)
        
        if student_master_encoding is None:
            conn.close()
            return jsonify({"status": "fail", "message": "Face Engine Error: Could not read master photo."})

        img_np = np.frombuffer(image_bytes, dtype=np.uint8)
        img = cv2.imdecode(img_np, cv2.IMREAD_COLOR)
        img_rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        faces = face_detector(img_rgb, 1)

        if len(faces) == 0:
            conn.close()
            return jsonify({"status": "fail", "message": "No face detected in camera!"})

        shape = shape_predictor(img_rgb, faces[0])
        current_encoding = np.array(face_encoder.compute_face_descriptor(img_rgb, shape))
        dist = np.linalg.norm(student_master_encoding - current_encoding)
        if dist > 0.48:
            conn.close()
            return jsonify({"status": "fail", "message": "Face does not match registered photo!"})
            
    except Exception as e:
        conn.close()
        return jsonify({"status": "error", "message": f"Face Engine Error: {str(e)}"})

    class_lat, class_lon = 19.197777803978884, 72.82653160301494
    distance = calculate_distance(lat, lon, class_lat, class_lon)
    status = "Present" if distance <= 200 else "Too Far"
    
    try:
        cursor.execute("""
            INSERT INTO AttendanceLogs (StudentName, TokenUsed, Latitude, Longitude, Status, SubjectName) 
            VALUES (?, ?, ?, ?, ?, ?)""",
            (student_name, scanned_token, lat, lon, status, subject))
        conn.commit()
        conn.close()
        return jsonify({"status": "success" if status == "Present" else "fail", "message": f"Attendance {status}!"})
    except Exception as e: 
        return jsonify({"status": "error", "message": f"DB Error: {str(e)}"})

@app.route('/login_teacher')
def login_teacher_page():
    return render_template('login_teacher.html')

@app.route('/login_student')
def login_student_page():
    return render_template('login_student.html')

@app.route('/login', methods=['POST'])
def login():
    user_id = request.form.get('prn') # This is either StaffID or PRN
    password = request.form.get('password')
    role_type = request.form.get('role_type')
    subject = request.form.get('subject')

    conn = get_db_connection()
    cursor = conn.cursor()

    if role_type == 'Teacher':
        # Search ONLY in Teachers table
        cursor.execute("SELECT TeacherName, StaffID FROM Teachers WHERE StaffID = ? AND Password = ?", (user_id, password))
        user = cursor.fetchone()
        if user:
            session['student_id'], session['student_name'], session['user_role'] = user[1], user[0], 'Teacher'
            return redirect(url_for('qr_display', subject=subject))

    else:
        # Search ONLY in Students table
        cursor.execute("SELECT StudentName, StudentID FROM Students WHERE StudentID = ? AND Password = ?", (user_id, password))
        user = cursor.fetchone()
        if user:
            session['student_id'], session['student_name'], session['user_role'] = user[1], user[0], 'Student'
            return redirect(url_for('stats_page'))

    conn.close()
    return "Invalid Credentials for this Portal", 401

@app.route('/logout')
def logout():
    role = session.get('user_role')
    session.clear() # This removes the StaffID and Subject from memory
    
    # Professional Touch: Redirect them back to their specific portal
    if role == 'Teacher':
        return redirect(url_for('login_teacher_page'))
    else:
        return redirect(url_for('login_student_page'))

@app.route('/stats', methods=['GET', 'POST']) # Added methods here
@login_required(role="Student") # Added security for consistency
def stats_page():
    prn = session.get('student_id')
    name = session.get('student_name')
    if not prn: 
        return redirect(url_for('login_student_page'))

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT ScanTime, SubjectName, Status, Latitude, Longitude 
        FROM AttendanceLogs WHERE StudentName = ? ORDER BY ScanTime DESC""", (name,))
    rows = cursor.fetchall()
    logs = [{"time": r[0].strftime("%d-%m-%Y %H:%M"), "subject": r[1], "status": r[2], "lat": r[3], "lon": r[4]} for r in rows]

    cursor.execute("""
        SELECT SubjectName, COUNT(*) as Total, 
               SUM(CASE WHEN Status = 'Present' THEN 1 ELSE 0 END) as Present
        FROM AttendanceLogs WHERE StudentName = ? GROUP BY SubjectName""", (name,))
    stats_rows = cursor.fetchall()
    subject_stats = {r[0]: {"total": r[1], "present": r[2], "percentage": round((r[2]/r[1])*100) if r[1]>0 else 0} for r in stats_rows}
    conn.close()
    return render_template('stats.html', name=name, logs=logs, subject_stats=subject_stats)

@app.route('/report')
def report_page():
    # 1. SECURITY CHECK: Ensure only Teachers can enter
    if session.get('user_role') != 'Teacher':
        # If not a teacher, block access and send them away
        return "Access Denied: You do not have permission to view this page.", 403

    # 2. DATABASE LOGIC: Get the logs
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT StudentName, ScanTime, Status, Latitude, Longitude FROM AttendanceLogs ORDER BY ScanTime DESC")
    rows = cursor.fetchall()
    
    # 3. DATA FORMATTING: Prepare for the HTML table
    attendance_data = [{
        "name": r[0], 
        "date": r[1].strftime("%d-%b-%Y"),
        "time": r[1].strftime("%I:%M %p"),
        "status": r[2], 
        "lat": r[3], 
        "lon": r[4]
    } for r in rows]
    
    conn.close()
    
    # 4. RENDER: Send formatted data to the report template
    return render_template('report.html', logs=attendance_data)

@app.route('/api/attendance_data')
def get_attendance_api():
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT StudentName, ScanTime, Status, Latitude, Longitude FROM AttendanceLogs ORDER BY ScanTime DESC")
        rows = cursor.fetchall()
        conn.close()

        logs = []
        for r in rows:
            logs.append({
                "name": r[0],
                "date": r[1].strftime("%d-%b-%Y"),
                "time": r[1].strftime("%I:%M %p"),
                "status": r[2],
                "lat": r[3],
                "lon": r[4]
            })
        return jsonify(logs)
    except Exception as e:
        return jsonify({"error": str(e)}), 500  

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)