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
from flask import Flask, render_template, request, jsonify, session, redirect, url_for, flash
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, timedelta

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
    # We are removing 'global recent_tokens' because we use the DB now!
    subject = request.args.get('subject', 'General')
    class_year = session.get('selected_class')
    
    # 1. Generate the token
    new_token = f"{uuid.uuid4()}|{subject}"
    
    # 2. Set expiry (e.g., 2 minutes to handle the 15s refresh cycle safely)
    expires_at = datetime.now() + timedelta(minutes=2)

    conn = get_db_connection()
    cursor = conn.cursor()
    
    # 3. Save to your new database table
    cursor.execute("""
        INSERT INTO ActiveQRSessions (TokenValue, SubjectName, ClassYear, ExpiresAt)
        VALUES (?, ?, ?, ?)
    """, (new_token, subject, class_year, expires_at))
    
    # 4. Optional: Auto-cleanup old tokens while we are here
    cursor.execute("DELETE FROM ActiveQRSessions WHERE ExpiresAt < GETDATE()")
    
    conn.commit()
    conn.close()
    
    # 5. Generate the QR as usual
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
    class_year = data.get('class_year')
    password = data.get('password')
    hashed_password = generate_password_hash(password) # Scramble the password
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
    INSERT INTO Students (StudentID, Password, StudentName, DeviceID, RollNo, PRN, Role, ClassYear) 
    VALUES (?, ?, ?, ?, ?, ?, ?, ?)""", 
(prn, hashed_password, fullname, fingerprint, rollno, prn, 'Student', class_year))
        conn.commit()
        conn.close()
        return jsonify({"status": "success", "message": "Account created!"})
    except pyodbc.IntegrityError:
        return jsonify({"status": "error", "message": "Roll Number or PRN already registered!"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})

@app.route('/verify_attendance', methods=['POST'])
def verify_attendance():
    data = request.json
    student_id = session.get('student_id') or data.get('prn')
    current_device = data.get('device_fingerprint')
    full_token = data.get('token')
    lat = data.get('latitude')
    lon = data.get('longitude')

    if not student_id:
        return jsonify({"status": "fail", "message": "Identity missing! Please login first."})

    conn = get_db_connection()
    cursor = conn.cursor()

    # --- DATABASE TOKEN VALIDATION ---
    cursor.execute("""
        SELECT SubjectName, ClassYear FROM ActiveQRSessions 
        WHERE TokenValue = ? AND ExpiresAt > GETDATE()
    """, (full_token,))
    
    session_row = cursor.fetchone()

    if not session_row:
        conn.close()
        return jsonify({"status": "fail", "message": "QR Expired or Invalid!"})

    subject = session_row[0]
    class_year = session_row[1]
    scanned_token = full_token.split("|", 1)[0] if "|" in full_token else full_token

    # 1. STUDENT VALIDATION
    cursor.execute("SELECT StudentName, DeviceID, ClassYear FROM Students WHERE StudentID = ?", (student_id,))
    user = cursor.fetchone()
    
    if not user:
        conn.close()
        return jsonify({"status": "fail", "message": "PRN not registered!"})
    
    student_name, stored_device, student_registered_year = user[0], user[1], user[2]

    # --- ACADEMIC YEAR RESTRICTION ---
    if student_registered_year != class_year:
        conn.close()
        return jsonify({
            "status": "fail", 
            "message": f"Access Denied! You are a {student_registered_year} student. This QR is for {class_year}."
        })

    # --- UPDATED: DEVICE BINDING SECURITY ---
    # If a teacher has reset the device, stored_device will be None in Python
    if stored_device is None:
        conn.close()
        return jsonify({
            "status": "fail", 
            "message": "Device not linked! Please logout and login again to bind this phone."
        })

    # 2. Security: Device Fingerprint Check
    # We now check that the active device matches the database binding
    if stored_device != current_device:
        conn.close()
        return jsonify({"status": "fail", "message": "Security Alert: Unauthorized Device!"})

    try:
        # Face Recognition Logic
        header, encoded = data.get('image').split(",", 1)
        image_bytes = base64.b64decode(encoded)
        face_path = f"static/faces/{student_id}.jpg"
        
        if not os.path.exists(face_path):
            conn.close()
            return jsonify({"status": "fail", "message": "Registered photo missing!"})
            
        student_master_encoding = get_face_encoding_direct(face_path)
        
        if student_master_encoding is None:
            conn.close()
            return jsonify({"status": "fail", "message": "Face Engine Error: Master photo unreadable."})

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
        if conn: conn.close()
        return jsonify({"status": "error", "message": f"Face Engine Error: {str(e)}"})

    # Geofencing Logic
    class_lat, class_lon = 19.18217458321158, 72.84003716649318
    distance = calculate_distance(lat, lon, class_lat, class_lon)
    status = "Present" if distance <= 100 else "Too Far"
    
    try:
        # 3. DUPLICATE CHECK
        cursor.execute("""
            SELECT 1 FROM AttendanceLogs 
            WHERE StudentName = ? 
            AND SubjectName = ? 
            AND Status = 'Present'
            AND CAST(ScanTime AS DATE) = CAST(GETDATE() AS DATE)
        """, (student_name, subject))
        
        if cursor.fetchone():
            conn.close()
            return jsonify({"status": "fail", "message": "Attendance already marked for this subject today!"})

        # 4. SAVE TO DATABASE
        cursor.execute("""
            INSERT INTO AttendanceLogs (StudentName, TokenUsed, Status, SubjectName, ClassYear, Latitude, Longitude, EntryType) 
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (student_name, scanned_token, status, subject, class_year, lat, lon, 'QR_Scan'))
        
        conn.commit()
        conn.close()
        return jsonify({"status": "success" if status == "Present" else "fail", "message": f"Attendance {status}!"})
    except Exception as e: 
        if conn: conn.close()
        return jsonify({"status": "error", "message": f"DB Error: {str(e)}"})
    
@app.route('/login_teacher')
def login_teacher_page():
    return render_template('login_teacher.html')

@app.route('/login_student')
def login_student_page():
    return render_template('login_student.html')

@app.route('/admin/update_role', methods=['POST'])
@login_required(role="Admin")
def update_role():
    """ Updates a user's role (Student/Teacher/Admin) in the Students table. """
    try:
        data = request.json
        prn = data.get('prn')
        new_role = data.get('role')

        if not prn or not new_role:
            return jsonify({"status": "error", "message": "Missing data"}), 400

        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Update the Role column for the specific StudentID
        cursor.execute("UPDATE Students SET Role = ? WHERE StudentID = ?", (new_role, prn))
        
        conn.commit()
        conn.close()
        
        return jsonify({"status": "success", "message": f"User {prn} successfully updated to {new_role}!"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/login', methods=['POST'])
def login():
    user_id = request.form.get('prn')
    password = request.form.get('password')
    role_type = request.form.get('role_type')
    class_year = request.form.get('class_year') 
    
    conn = get_db_connection()
    cursor = conn.cursor()

    # 1. ADMIN LOGIN LOGIC
    if role_type == 'Admin':
        cursor.execute("SELECT AdminName, Username, Password FROM Admins WHERE Username = ?", (user_id,))
        user = cursor.fetchone()
        
        if user and check_password_hash(user[2], password):
            session['student_id'] = user[1]
            session['student_name'] = user[0]
            session['user_role'] = 'Admin'
            conn.close()
            return redirect(url_for('admin_panel'))
        else:
            conn.close()
            return "Invalid Admin Credentials", 401

    # 2. TEACHER LOGIN LOGIC
    elif role_type == 'Teacher':
        cursor.execute("SELECT TeacherName, StaffID, Password FROM Teachers WHERE StaffID = ?", (user_id,))
        user = cursor.fetchone()
        
        if user and check_password_hash(user[2], password):
            cursor.execute("SELECT SubjectName FROM TeacherSubjects WHERE StaffID = ? AND ClassYear = ?", (user_id, class_year))
            subject_row = cursor.fetchone()
            
            if subject_row:
                session['student_id'] = user[1]
                session['student_name'] = user[0]
                session['user_role'] = 'Teacher'
                session['selected_class'] = class_year
                conn.close()
                return redirect(url_for('teacher_dashboard'))
            else:
                conn.close()
                return "Error: No subject assigned to you for this year.", 403
        else:
            conn.close()
            return "Invalid Teacher Credentials", 401

    # 3. STUDENT LOGIN LOGIC
    else:
        cursor.execute("SELECT StudentName, StudentID, Password, DeviceID FROM Students WHERE StudentID = ?", (user_id,))
        user = cursor.fetchone()

        if user and check_password_hash(user[2], password):
            student_name = user[0]
            student_id = user[1]
            stored_device = user[3]
            
            # Capture the current device fingerprint from the hidden form field
            current_device_from_login = request.form.get('device_fingerprint')

            # --- DEVICE RE-BINDING ONLY ---
            # If the device was reset (NULL in DB), we only link the new ID here.
            # We DO NOT mark attendance; the student must still scan the QR.
            if stored_device is None and current_device_from_login:
                cursor.execute("UPDATE Students SET DeviceID = ? WHERE StudentID = ?", 
                               (current_device_from_login, student_id))
                conn.commit()
                print(f"DEBUG: Device Re-linked for {student_id}. Rescan required.")

            session['student_id'] = student_id
            session['student_name'] = student_name
            session['user_role'] = 'Student'
            conn.close()
            
            # Redirect to dashboard so they can use the "Take Attendance" button to rescan.
            return redirect(url_for('stats_page'))
        else:
            conn.close()
            return "Invalid Student Credentials", 401

    conn.close()
    return "Invalid Credentials", 401

@app.route('/logout')
def logout():
    role = session.get('user_role')
    session.clear() # This removes the StaffID and Subject from memory
    
    # Professional Touch: Redirect them back to their specific portal
    if role == 'Teacher':
        return redirect(url_for('login_teacher_page'))
    else:
        return redirect(url_for('login_student_page'))

@app.route('/stats', methods=['GET', 'POST'])
@login_required(role="Student")
def stats_page():
    prn = session.get('student_id')
    name = session.get('student_name')
    
    if not prn: 
        return redirect(url_for('login_student_page'))

    conn = get_db_connection()
    cursor = conn.cursor()

    # 1. Fetch THIS student's attendance history (Added SubjectName to SELECT)
    cursor.execute("""
        SELECT SubjectName, ScanTime, Status, Latitude, Longitude, ClassYear 
        FROM AttendanceLogs 
        WHERE StudentName = ? 
        ORDER BY ScanTime DESC 
    """, (name,))
    
    rows = cursor.fetchall()
    
    # 2. Correct Mapping (r[0]=Subject, r[1]=Time, r[2]=Status, r[3]=Lat, r[4]=Lon)
    logs = [
        {
            "subject": r[0],
            "time": r[1].strftime("%d-%m-%Y %I:%M %p"),
            "status": r[2],
            "lat": r[3],
            "lon": r[4]
        } for r in rows
    ]

    # 3. Calculate Subject-wise Statistics
    cursor.execute("""
        SELECT SubjectName, COUNT(*) as Total, 
               SUM(CASE WHEN Status = 'Present' THEN 1 ELSE 0 END) as Present
        FROM AttendanceLogs 
        WHERE StudentName = ? 
        GROUP BY SubjectName
    """, (name,))
    
    stats_rows = cursor.fetchall()
    subject_stats = {
        r[0]: {
            "total": r[1], 
            "present": r[2], 
            "percentage": round((r[2]/r[1])*100) if r[1] > 0 else 0
        } for r in stats_rows
    }
    
    conn.close()
    return render_template('stats.html', name=name, logs=logs, subject_stats=subject_stats)

@app.route('/report')
@login_required(role="Teacher")
def report_page():
    teacher_id = session.get('student_id')
    class_year = session.get('selected_class')
    
    conn = get_db_connection()
    cursor = conn.cursor()

    # 1. Identify the subject assigned to this teacher for the selected year
    cursor.execute("SELECT SubjectName FROM TeacherSubjects WHERE StaffID = ? AND ClassYear = ?", 
                   (teacher_id, class_year))
    subj_row = cursor.fetchone()
    assigned_subject = subj_row[0] if subj_row else 'N/A'

    # 2. Get the REAL total number of students enrolled in this year
    # This prevents the denominator from being based on log entries
    cursor.execute("SELECT COUNT(*) FROM Students WHERE ClassYear = ?", (class_year,))
    total_enrolled = cursor.fetchone()[0] or 0

    conn.close()
    
    return render_template('report.html', 
                           subject=assigned_subject, 
                           year=class_year,
                           total_enrolled=total_enrolled)

@app.route('/api/attendance_data')
@login_required(role="Teacher")
def get_attendance_api():
    teacher_id = session.get('student_id')
    class_year = session.get('selected_class')
    
    conn = get_db_connection()
    cursor = conn.cursor()

    # 1. Get the subject name assigned to the teacher for this specific class
    cursor.execute("SELECT SubjectName FROM TeacherSubjects WHERE StaffID = ? AND ClassYear = ?", 
                   (teacher_id, class_year))
    subj_row = cursor.fetchone()
    
    if not subj_row:
        conn.close()
        return jsonify({"all_students": [], "logs": []})
    
    subject = subj_row[0]

    # 2. Get EVERY student registered, including their StudentID (PRN) for device resetting
    # PRN is essential to identify which phone binding to clear
    cursor.execute("""
        SELECT StudentName, ClassYear, RollNo, StudentID 
        FROM Students 
        WHERE ClassYear = ? 
        ORDER BY CAST(RollNo AS INT) ASC
    """, (class_year,))
    
    # We include 'prn' in the dictionary so the frontend can pass it to /reset_device
    all_students = [
        {
            "name": r[0], 
            "class_year": r[1], 
            "roll_no": r[2],
            "prn": r[3] 
        } for r in cursor.fetchall()
    ]

    # 3. Get all attendance logs for this subject, including EntryType
    # EntryType helps the teacher track if a record was QR-scanned or manually entered
    cursor.execute("""
        SELECT StudentName, ScanTime, Status, Latitude, Longitude, ClassYear, EntryType 
        FROM AttendanceLogs 
        WHERE SubjectName = ? AND ClassYear = ? 
        ORDER BY ScanTime DESC
    """, (subject, class_year))
    
    logs = [{
        "name": r[0], 
        "date": r[1].strftime("%Y-%m-%d"), 
        "time": r[1].strftime("%I:%M %p"), 
        "status": r[2],
        "lat": r[3], 
        "lon": r[4],
        "class_year": r[5],
        "entry_type": r[6]
    } for r in cursor.fetchall()]

    conn.close()
    return jsonify({"all_students": all_students, "logs": logs})
    
@app.route('/admin/assign_subject', methods=['POST'])
@login_required(role="Admin")
def assign_subject():
    data = request.json
    staff_id = data.get('staff_id')
    year = data.get('year')
    subject = data.get('subject')
    
    conn = get_db_connection()
    cursor = conn.cursor()
    # This UPDATES existing or INSERTS new assignments
    cursor.execute("""
        IF EXISTS (SELECT 1 FROM TeacherSubjects WHERE StaffID=? AND ClassYear=?)
            UPDATE TeacherSubjects SET SubjectName=? WHERE StaffID=? AND ClassYear=?
        ELSE
            INSERT INTO TeacherSubjects (StaffID, ClassYear, SubjectName) VALUES (?, ?, ?)
    """, (staff_id, year, subject, staff_id, year, staff_id, year, subject))
    conn.commit()
    conn.close()
    return jsonify({"status": "success"})

@app.route('/get_assigned_subject')
def get_assigned_subject():
    staff_id = request.args.get('staff_id')
    year = request.args.get('year')
    
    if not staff_id or not year:
        return jsonify({"subject": ""})

    conn = get_db_connection()
    cursor = conn.cursor()
    # Check the mapping table for the subject
    cursor.execute("SELECT SubjectName FROM TeacherSubjects WHERE StaffID = ? AND ClassYear = ?", (staff_id, year))
    row = cursor.fetchone()
    conn.close()
    
    if row:
        return jsonify({"subject": row[0]})
    return jsonify({"subject": "No subject assigned"})

@app.route('/admin/add_teacher_full', methods=['POST'])
@login_required(role="Admin") 
def add_teacher_full():
    data = request.json
    name = data.get('name')
    staff_id = data.get('staff_id')
    password = data.get('password')
    mappings = data.get('mappings') # This comes from your frontend list

    hashed_pw = generate_password_hash(password)

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        # 1. Create the main Teacher account if it doesn't exist
        cursor.execute("SELECT 1 FROM Teachers WHERE StaffID = ?", (staff_id,))
        if not cursor.fetchone():
            cursor.execute("INSERT INTO Teachers (TeacherName, StaffID, Password) VALUES (?, ?, ?)", 
                           (name, staff_id, hashed_pw))
        
        # 2. CLEAR existing mappings to avoid duplicates
        cursor.execute("DELETE FROM TeacherSubjects WHERE StaffID = ?", (staff_id,))

        # 3. REGISTER all subjects from the frontend into the mapping table
        for item in mappings:
            # item['year'] and item['subject'] come from the .t-year and .t-subj inputs
            cursor.execute("""
                INSERT INTO TeacherSubjects (StaffID, ClassYear, SubjectName) 
                VALUES (?, ?, ?)
            """, (staff_id, item['year'], item['subject']))
        
        conn.commit()
        return jsonify({"status": "success", "message": "Teacher registered and subjects mapped!"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})
    finally:
        conn.close()
    
@app.route('/admin_panel')
@login_required(role="Admin")
def admin_panel():
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # 1. Fetch Teacher Names for the Timetable dropdown
    cursor.execute("SELECT TeacherName FROM Teachers")
    teachers = [row[0] for row in cursor.fetchall()]
    
    # 2. Fetch Name + ID for the new Teacher Management table
    cursor.execute("SELECT TeacherName, StaffID FROM Teachers")
    teacher_list = cursor.fetchall()
    
    conn.close()
    # Pass both lists to the template
    return render_template('admin_panel.html', teachers=teachers, teacher_list=teacher_list)

@app.route('/admin/delete_teacher/<staff_id>', methods=['DELETE'])
@login_required(role="Admin")
def delete_teacher(staff_id):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        # Delete mappings first to avoid foreign key errors, then delete the teacher
        cursor.execute("DELETE FROM TeacherSubjects WHERE StaffID = ?", (staff_id,))
        cursor.execute("DELETE FROM Teachers WHERE StaffID = ?", (staff_id,))
        conn.commit()
        conn.close()
        return jsonify({"status": "success", "message": "Teacher deleted successfully!"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})

@app.route('/api/student_count/<subject>')
def get_student_count(subject):
    # We remove @login_required temporarily to prevent session-loss issues
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # This query focuses ONLY on the Subject and Today's Date
    # It ignores the ClassYear to ensure the counter stays active even if session flips
    cursor.execute("""
        SELECT COUNT(*) FROM AttendanceLogs 
        WHERE SubjectName = ? 
        AND Status = 'Present'
        AND CAST(ScanTime AS DATE) = CAST(GETDATE() AS DATE)
    """, (subject,))
    
    count = cursor.fetchone()[0]
    conn.close()
    return jsonify({"count": count})

@app.route('/admin/save_timetable', methods=['POST'])
@login_required(role="Admin")
def save_timetable():
    data = request.json
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # 1. CONFLICT CHECK: Is this lecturer already teaching elsewhere at this time?
        # We look for any record with the same Day, same Time, and same Lecturer, but a DIFFERENT Class Year
        cursor.execute("""
            SELECT ClassYear FROM Timetable 
            WHERE DayOfWeek = ? AND StartTime = ? AND LecturerName = ? AND ClassYear != ?
        """, (data['day'], data['time'], data['lecturer'], data['year']))
        
        conflict = cursor.fetchone()
        if conflict:
            conn.close()
            return jsonify({
                "status": "error", 
                "message": f"Conflict: {data['lecturer']} is already assigned to {conflict[0]} at this time!"
            })

        # 2. UPSERT LOGIC: Check if this specific Year/Day/Time slot already exists
        cursor.execute("""
            SELECT 1 FROM Timetable 
            WHERE ClassYear = ? AND DayOfWeek = ? AND StartTime = ?
        """, (data['year'], data['day'], data['time']))
        
        if cursor.fetchone():
            # Update existing slot
            cursor.execute("""
                UPDATE Timetable 
                SET SubjectName = ?, LecturerName = ?
                WHERE ClassYear = ? AND DayOfWeek = ? AND StartTime = ?
            """, (data['subject'], data['lecturer'], data['year'], data['day'], data['time']))
            message = "Schedule Updated!"
        else:
            # Insert new slot
            cursor.execute("""
                INSERT INTO Timetable (ClassYear, DayOfWeek, StartTime, SubjectName, LecturerName)
                VALUES (?, ?, ?, ?, ?)
            """, (data['year'], data['day'], data['time'], data['subject'], data['lecturer']))
            message = "New Lecture Added!"
            
        conn.commit()
        conn.close()
        return jsonify({"status": "success", "message": message})
    except Exception as e:
        if 'conn' in locals(): conn.close()
        return jsonify({"status": "error", "message": str(e)})

@app.route('/api/get_full_timetable')
@login_required(role="Admin")
def get_full_timetable():
    conn = get_db_connection()
    cursor = conn.cursor()
    # Fetches the data to display in your admin management table
    cursor.execute("SELECT ScheduleID, ClassYear, DayOfWeek, StartTime, SubjectName, LecturerName FROM Timetable ORDER BY ClassYear, DayOfWeek")
    rows = cursor.fetchall()
    conn.close()
    
    return jsonify([{
        "id": r[0], "year": r[1], "day": r[2], "time": r[3], "subject": r[4], "lecturer": r[5]
    } for r in rows])

@app.route('/admin/delete_lecture/<int:lecture_id>', methods=['DELETE'])
@login_required(role="Admin")
def delete_lecture(lecture_id):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM Timetable WHERE ScheduleID = ?", (lecture_id,))
        conn.commit()
        conn.close()
        return jsonify({"status": "success", "message": "Lecture removed!"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})

@app.route('/admin/update_timetable', methods=['POST'])
@login_required(role="Admin")
def update_timetable():
    data = request.json
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        # Updates existing record using the unique combination of Year, Day, and Time
        cursor.execute("""
            UPDATE Timetable 
            SET SubjectName = ?, LecturerName = ?
            WHERE ClassYear = ? AND DayOfWeek = ? AND StartTime = ?
        """, (data['subject'], data['lecturer'], data['year'], data['day'], data['time']))
        
        if cursor.rowcount == 0:
            return jsonify({"status": "error", "message": "No matching record found to update."})
            
        conn.commit()
        conn.close()
        return jsonify({"status": "success", "message": "Update successful!"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})    
    
@app.route('/teacher/dashboard')
@login_required(role="Teacher")
def teacher_dashboard():
    teacher_id = session.get('student_id')
    teacher_name = session.get('student_name')
    class_year = session.get('selected_class')

    conn = get_db_connection() 
    cursor = conn.cursor()
    
    # 1. Subject for current session cards
    cursor.execute("SELECT SubjectName FROM TeacherSubjects WHERE StaffID = ? AND ClassYear = ?", (teacher_id, class_year))
    subj_row = cursor.fetchone()
    subject = subj_row[0] if subj_row else 'General'

    # 2. Fetch full schedule with Day-Wise Sorting
    cursor.execute("""
        SELECT DayOfWeek, StartTime, SubjectName, ClassYear 
        FROM Timetable 
        WHERE LecturerName LIKE ?
        ORDER BY CASE 
            WHEN DayOfWeek = 'Monday' THEN 1
            WHEN DayOfWeek = 'Tuesday' THEN 2
            WHEN DayOfWeek = 'Wednesday' THEN 3
            WHEN DayOfWeek = 'Thursday' THEN 4
            WHEN DayOfWeek = 'Friday' THEN 5
            WHEN DayOfWeek = 'Saturday' THEN 6
            ELSE 7 END, 
        StartTime ASC
    """, (f"%{teacher_name}%",))
    raw_schedule = cursor.fetchall()

    # 3. NEW: Fetch Students for Manual Attendance Dropdown
    # We filter by class_year so the teacher only sees students in the current class
    cursor.execute("""
        SELECT StudentID, Password, StudentName, DeviceID, RollNo 
        FROM Students 
        WHERE ClassYear = ? 
        ORDER BY RollNo ASC
    """, (class_year,))
    students = cursor.fetchall()

    conn.close()

    # Labels for the frontend display
    days = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday']
    time_slots = ['11:00 AM - 12:00 PM', '12:00 PM - 01:00 PM', '01:00 PM - 02:00 PM', '02:00 PM - 03:00 PM']

    return render_template('teacher_dashboard.html', 
                           subject=subject, 
                           raw_schedule=raw_schedule,
                           students=students,  # Added students list
                           days=days, 
                           time_slots=time_slots)

@app.route('/admin/clear_timetable/<year>', methods=['DELETE'])
@login_required(role="Admin")
def clear_timetable(year):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Deletes all lectures matching the specific year (e.g., '1st Year')
        cursor.execute("DELETE FROM Timetable WHERE ClassYear = ?", (year,))
        
        conn.commit()
        conn.close()
        return jsonify({"status": "success", "message": f"All lectures for {year} have been cleared."})
    except Exception as e:
        if conn: conn.close()
        return jsonify({"status": "error", "message": str(e)})
    
@app.route('/api/get_teacher_subjects/<staff_name>/<class_year>')
@login_required(role="Admin")
def get_teacher_subjects(staff_name, class_year):
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # 1. Get the StaffID for the selected teacher name
    cursor.execute("SELECT StaffID FROM Teachers WHERE TeacherName = ?", (staff_name,))
    teacher = cursor.fetchone()
    
    if not teacher:
        conn.close()
        return jsonify([])

    # 2. Get subjects filtered by BOTH StaffID and the specific ClassYear
    cursor.execute("""
        SELECT DISTINCT SubjectName 
        FROM TeacherSubjects 
        WHERE StaffID = ? AND ClassYear = ?
    """, (teacher[0], class_year))
    
    subjects = [row[0] for row in cursor.fetchall()]
    conn.close()
    return jsonify(subjects)

@app.route('/api/contact', methods=['POST'])
def contact_us():
    try:
        data = request.json
        name = data.get('name')
        email = data.get('email')
        msg = data.get('message')

        # This prints the message to your VS Code / Terminal console
        print(f"\n--- NEW INQUIRY RECEIVED ---")
        print(f"From: {name} ({email})")
        print(f"Message: {msg}")
        print(f"----------------------------\n")

        return jsonify({"status": "success", "message": "Message sent successfully!"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500
    
@app.route('/manual_mark', methods=['POST'])
@login_required(role="Teacher")
def manual_mark():
    student_name = request.form.get('student_name')
    teacher_id = session.get('student_id')
    class_year = session.get('selected_class')
    
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # 1. Dynamically get the subject assigned to THIS teacher for THIS class
    cursor.execute("SELECT SubjectName FROM TeacherSubjects WHERE StaffID = ? AND ClassYear = ?", 
                   (teacher_id, class_year))
    subj_row = cursor.fetchone()
    subject_name = subj_row[0] if subj_row else 'General'

    now = datetime.now()

    try:
        # 2. Insert into AttendanceLogs
        # We ensure Status is 'Present' so the badge turns green in the report
        query = """
        INSERT INTO AttendanceLogs 
        (StudentName, ScanTime, Latitude, Longitude, Status, SubjectName, ClassYear, EntryType)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """
        cursor.execute(query, (
            student_name, 
            now, 
            0.0, 0.0, 
            'Present', 
            subject_name, 
            class_year, 
            'Manual_Entry'
        ))
        
        conn.commit()
        flash(f"Attendance for {student_name} marked successfully!", "success")
    except Exception as e:
        print(f"Manual Mark Error: {e}")
        flash("Error saving to database.", "danger")
    finally:
        conn.close()

    # 3. Redirect back to exactly where the teacher was
    return redirect(request.referrer or url_for('report_page'))

@app.route('/delete_attendance', methods=['POST'])
@login_required(role="Teacher")
def delete_attendance():
    student_name = request.form.get('student_name')
    teacher_id = session.get('student_id')
    class_year = session.get('selected_class')

    conn = get_db_connection()
    cursor = conn.cursor()
    
    # 1. Get current subject
    cursor.execute("SELECT SubjectName FROM TeacherSubjects WHERE StaffID = ? AND ClassYear = ?", 
                   (teacher_id, class_year))
    subj_row = cursor.fetchone()
    subject_name = subj_row[0] if subj_row else 'General'

    try:
        # 2. Delete today's log for this student and subject
        # We target today's date so historical data isn't affected
        cursor.execute("""
            DELETE FROM AttendanceLogs 
            WHERE StudentName = ? 
            AND SubjectName = ? 
            AND CAST(ScanTime AS DATE) = CAST(GETDATE() AS DATE)
        """, (student_name, subject_name))
        
        conn.commit()
        flash(f"Attendance for {student_name} removed.", "info")
    except Exception as e:
        print(f"Delete Error: {e}")
        flash("Error deleting record.", "danger")
    finally:
        conn.close()

    return redirect(request.referrer or url_for('report_page'))

@app.route('/reset_device', methods=['POST'])
@login_required(role="Teacher")
def reset_device():
    student_prn = request.form.get('student_prn')
    
    conn = get_db_connection()
    cursor = conn.cursor()
    
    try:
        # Set DeviceID to NULL so the next login registers the new phone
        cursor.execute("UPDATE Students SET DeviceID = NULL WHERE StudentID = ?", (student_prn,))
        conn.commit()
        flash(f"Device binding reset for PRN: {student_prn}. They can now link a new phone.", "info")
    except Exception as e:
        print(f"Reset Error: {e}")
        flash("Error resetting device binding.", "danger")
    finally:
        conn.close()

    return redirect(request.referrer or url_for('report_page'))

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)