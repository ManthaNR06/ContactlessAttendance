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
    class_year = data.get('class_year')
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
        INSERT INTO Students (StudentID, Password, StudentName, DeviceID, RollNo, PRN, Role, ClassYear) 
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)""", 
        (prn, password, fullname, fingerprint, rollno, prn, 'Student', class_year)) 
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

    # Extract subject from the token
    scanned_token, subject = full_token.split("|", 1) if "|" in full_token else (full_token, "General")

    conn = get_db_connection()
    cursor = conn.cursor()

    # 1. ROBUST CLASS YEAR LOOKUP (Teacher's Target Year)
    class_year = session.get('selected_class')
    if not class_year:
        cursor.execute("SELECT ClassYear FROM TeacherSubjects WHERE SubjectName = ?", (subject,))
        row = cursor.fetchone()
        class_year = row[0] if row else 'N/A'

    # 2. STUDENT VALIDATION (Including their Registered Year)
    # We now fetch ClassYear from the Students table
    cursor.execute("SELECT StudentName, DeviceID, ClassYear FROM Students WHERE StudentID = ?", (student_id,))
    user = cursor.fetchone()
    
    if not user:
        conn.close()
        return jsonify({"status": "fail", "message": "PRN not registered!"})
    
    student_name, stored_device, student_registered_year = user[0], user[1], user[2]

    # --- NEW: ACADEMIC YEAR RESTRICTION ---
    # Check if student's registered year matches the subject's year
    if student_registered_year != class_year:
        conn.close()
        return jsonify({
            "status": "fail", 
            "message": f"Access Denied! You are a {student_registered_year} student. This QR is for {class_year}."
        })

    # 3. Security: Device Fingerprint Check
    if stored_device and stored_device != current_device:
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
        if conn: conn.close()
        return jsonify({"status": "error", "message": f"Face Engine Error: {str(e)}"})

    # Geofencing Logic
    class_lat, class_lon = 19.182159589817456, 72.8400043165067
    distance = calculate_distance(lat, lon, class_lat, class_lon)
    status = "Present" if distance <= 200 else "Too Far"
    
    try:
        # 4. DUPLICATE CHECK
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

        # 5. SAVE TO DATABASE
        cursor.execute("""
            INSERT INTO AttendanceLogs (StudentName, TokenUsed, Status, SubjectName, ClassYear, Latitude, Longitude) 
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (student_name, scanned_token, status, subject, class_year, lat, lon))
        
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

    # 1. ADMIN LOGIN LOGIC (Checks dedicated Admins table)
    if role_type == 'Admin':
        cursor.execute("SELECT AdminName, Username FROM Admins WHERE Username = ? AND Password = ?", (user_id, password))
        user = cursor.fetchone()
        if user:
            session['student_id'] = user[1]
            session['student_name'] = user[0]
            session['user_role'] = 'Admin'
            conn.close()
            return redirect(url_for('admin_panel'))

    # 2. TEACHER LOGIN LOGIC
    elif role_type == 'Teacher':
        cursor.execute("SELECT TeacherName, StaffID FROM Teachers WHERE StaffID = ? AND Password = ?", (user_id, password))
        user = cursor.fetchone()
        
        if user:
            cursor.execute("SELECT SubjectName FROM TeacherSubjects WHERE StaffID = ? AND ClassYear = ?", (user_id, class_year))
            subject_row = cursor.fetchone()
            
            if subject_row:
                auto_subject = subject_row[0]
                session['student_id'] = user[1]
                session['student_name'] = user[0]
                session['user_role'] = 'Teacher'
                session['selected_class'] = class_year
                conn.close()
                return redirect(url_for('qr_display', subject=auto_subject))
            else:
                conn.close()
                return "Error: No subject assigned to you for this year.", 403

    # 3. STUDENT LOGIN LOGIC
    else:
        cursor.execute("SELECT StudentName, StudentID FROM Students WHERE StudentID = ? AND Password = ?", (user_id, password))
        user = cursor.fetchone()
        if user:
            session['student_id'], session['student_name'], session['user_role'] = user[1], user[0], 'Student'
            conn.close()
            return redirect(url_for('stats_page'))

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

    # 1. Get the subject name
    cursor.execute("SELECT SubjectName FROM TeacherSubjects WHERE StaffID = ? AND ClassYear = ?", 
                   (teacher_id, class_year))
    subj_row = cursor.fetchone()
    
    if not subj_row:
        conn.close()
        return jsonify({"all_students": [], "logs": []})
    
    subject = subj_row[0]

    # 2. Get EVERY student registered, sorted by Roll Number ASCENDING
    # We use CAST to ensure numerical sorting (1, 2, 10 instead of 1, 10, 2)
    cursor.execute("""
        SELECT StudentName, ClassYear, RollNo 
        FROM Students 
        WHERE ClassYear = ? 
        ORDER BY CAST(RollNo AS INT) ASC
    """, (class_year,))
    all_students = [{"name": r[0], "class_year": r[1], "roll_no": r[2]} for r in cursor.fetchall()]

    # 3. Get all attendance logs for this subject
    cursor.execute("""
        SELECT StudentName, ScanTime, Status, Latitude, Longitude, ClassYear 
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
        "class_year": r[5]
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
    name, staff_id, password = data.get('name'), data.get('staff_id'), data.get('password')
    mappings = data.get('mappings') # This is now a list

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        # 1. Ensure teacher exists
        cursor.execute("SELECT 1 FROM Teachers WHERE StaffID = ?", (staff_id,))
        if not cursor.fetchone():
            cursor.execute("INSERT INTO Teachers (TeacherName, StaffID, Password) VALUES (?, ?, ?)", 
                           (name, staff_id, password))
        
        # 2. Loop through each mapping and update/insert
        for m in mappings:
            year, subject = m.get('year'), m.get('subject')
            cursor.execute("""
                IF EXISTS (SELECT 1 FROM TeacherSubjects WHERE StaffID=? AND ClassYear=?)
                    UPDATE TeacherSubjects SET SubjectName=? WHERE StaffID=? AND ClassYear=?
                ELSE
                    INSERT INTO TeacherSubjects (StaffID, ClassYear, SubjectName) VALUES (?, ?, ?)
            """, (staff_id, year, subject, staff_id, year, staff_id, year, subject))

        conn.commit()
        conn.close()
        return jsonify({"status": "success", "message": f"Updated {len(mappings)} assignments for {name}!"})
    except Exception as e:
        conn.close()
        return jsonify({"status": "error", "message": str(e)})
    
@app.route('/admin_panel')
@login_required(role="Admin")
def admin_panel():
    conn = get_db_connection()
    cursor = conn.cursor()
    # Fetch all users so you can manage their roles in the table
    cursor.execute("SELECT StudentID, StudentName, Role FROM Students")
    users = cursor.fetchall()
    conn.close()
    return render_template('admin_panel.html', users=users)

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

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)