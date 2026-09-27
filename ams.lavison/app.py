import os
import socket
import csv
import io
import sqlite3
import json
import uuid
from datetime import datetime, date, timedelta
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, flash, redirect, render_template, request, session, url_for, Response, abort, send_file
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

from database import (
    get_allowed_emails,
    get_db_connection,
    get_recent_attendance,
    get_stats,
    init_db,
    normalize_uid,
    seed_data,
)
from nfc.reader import NFCReaderService
from nfc.simulator import simulate_nfc_scan

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "ams-lavison-dev-secret")

reader_service = NFCReaderService(mode="keyboard")
ATTENDANCE_GRACE_MINUTES = 30


def parse_session_time(session_row, field):
    value = session_row[field]
    return datetime.fromisoformat(f"{session_row['session_date']}T{value}")


def display_time(value):
    if not value:
        return ""
    parsed = datetime.strptime(str(value)[:5], "%H:%M")
    return parsed.strftime("%I:%M %p").lstrip("0")


def normalize_session_time(value):
    if not value:
        return None
    text=str(value).strip().upper()
    for fmt in ("%I:%M %p", "%I:%M%p", "%H:%M", "%H:%M:%S"):
        try:
            return datetime.strptime(text,fmt).strftime("%H:%M:%S")
        except ValueError:
            continue
    return None


app.jinja_env.filters["display_time"] = display_time


@app.after_request
def disable_browser_cache(response):
    if response.content_type.startswith("text/html"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
    return response


@app.context_processor
def inject_network_url():
    return {"local_url": get_local_network_url()}


def get_local_network_url():
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("8.8.8.8", 80))
        ip = sock.getsockname()[0]
        sock.close()
        return f"http://{ip}:5000"
    except Exception:
        return "http://127.0.0.1:5000"


@app.before_request
def protect_routes():
    public_routes = {"login"}
    if request.endpoint in public_routes:
        return None
    if request.endpoint and request.endpoint.startswith("static"):
        return None
    if not session.get("account_id"):
        return redirect(url_for("login"))
    role = session.get("role")
    endpoint = request.endpoint
    admin_only = {"students", "student_details", "enroll_student", "courses", "sessions", "attendance", "settings", "accounts"}
    teacher_ok = {"dashboard", "reports", "export_attendance", "calendar", "teacher_courses", "course_materials", "download_material", "view_video", "stream_video", "logout", "index"}
    student_ok = {"student_home", "student_export", "calendar", "course_materials", "download_material", "view_video", "stream_video", "logout", "index"}
    if role == "student" and endpoint not in student_ok:
        return redirect(url_for("student_home"))
    if role == "teacher" and endpoint in admin_only:
        return redirect(url_for("dashboard"))


@app.context_processor
def inject_account():
    return {"current_role": session.get("role"), "current_email": session.get("account_email"), "current_account_id": session.get("account_id")}


def get_student_details(student_id):
    conn = get_db_connection()
    student = conn.execute("SELECT * FROM students WHERE id = ?", (student_id,)).fetchone()
    if not student:
        conn.close()
        return None

    enrolled = conn.execute(
        """
        SELECT c.id, c.name, c.code
        FROM enrollments e
        JOIN courses c ON c.id = e.course_id
        WHERE e.student_id = ?
        ORDER BY c.name ASC
        """,
        (student_id,),
    ).fetchall()

    history = conn.execute(
        """
        SELECT a.check_in_time, c.name AS course_name, s.session_date, s.start_time, a.status
        FROM attendance a
        JOIN courses c ON c.id = a.course_id
        JOIN sessions s ON s.id = a.session_id
        WHERE a.student_id = ?
        ORDER BY a.check_in_time DESC
        """,
        (student_id,),
    ).fetchall()

    total_sessions = conn.execute(
        "SELECT COUNT(DISTINCT session_id) FROM attendance WHERE student_id = ?",
        (student_id,),
    ).fetchone()[0]
    present = conn.execute(
        "SELECT COUNT(*) FROM attendance WHERE student_id = ? AND status = 'Present'",
        (student_id,),
    ).fetchone()[0]
    absent = conn.execute(
        "SELECT COUNT(*) FROM attendance WHERE student_id = ? AND status = 'Absent'",
        (student_id,),
    ).fetchone()[0]
    percent = 0
    if total_sessions:
        percent = round((present / total_sessions) * 100, 1)

    conn.close()
    return {
        "student": student,
        "enrolled_courses": enrolled,
        "history": history,
        "total_sessions": total_sessions,
        "present": present,
        "absent": absent,
        "attendance_percent": percent,
    }


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        conn = get_db_connection()
        account = conn.execute("SELECT * FROM accounts WHERE email = ? AND active = 1", (email,)).fetchone()
        conn.close()
        if account and not account["password_hash"] and len(password) >= 8:
            conn=get_db_connection(); conn.execute("UPDATE accounts SET password_hash=? WHERE id=?",(generate_password_hash(password),account["id"])); conn.commit(); conn.close()
            account=dict(account); account["password_hash"]=generate_password_hash(password)
        if account and account["password_hash"] and check_password_hash(account["password_hash"], password):
            session.clear()
            session.update(account_id=account["id"], role=account["role"], account_email=email, student_id=account["student_id"])
            return redirect(url_for("student_home" if account["role"] == "student" else "dashboard"))
        flash("Email or password is incorrect.", "error")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
def index():
    return redirect(url_for("student_home" if session.get("role") == "student" else "dashboard"))


@app.route("/dashboard")
def dashboard():
    if session.get("role") == "student": return redirect(url_for("student_home"))
    _sync=get_db_connection(); sync_absences(_sync); _sync.close()
    if session.get("role") == "teacher":
        c=get_db_connection()
        stats={"total_students":c.execute("SELECT COUNT(DISTINCT e.student_id) FROM enrollments e JOIN teacher_courses tc ON tc.course_id=e.course_id WHERE tc.teacher_id=?",(session["account_id"],)).fetchone()[0],"total_courses":c.execute("SELECT COUNT(*) FROM teacher_courses WHERE teacher_id=?",(session["account_id"],)).fetchone()[0],"todays_sessions":c.execute("SELECT COUNT(*) FROM sessions s JOIN teacher_courses tc ON tc.course_id=s.course_id WHERE tc.teacher_id=? AND s.session_date=date('now')",(session["account_id"],)).fetchone()[0],"todays_attendance":0,"present":0,"absent":0}
        recent=c.execute("SELECT st.full_name student,c.name course,a.check_in_time,a.status FROM attendance a JOIN students st ON st.id=a.student_id JOIN courses c ON c.id=a.course_id JOIN teacher_courses tc ON tc.course_id=c.id WHERE tc.teacher_id=? ORDER BY a.check_in_time DESC LIMIT 8",(session["account_id"],)).fetchall()
        stats["todays_attendance"]=c.execute("SELECT COUNT(*) FROM attendance a JOIN teacher_courses tc ON tc.course_id=a.course_id WHERE tc.teacher_id=? AND date(a.check_in_time)=date('now')",(session["account_id"],)).fetchone()[0]
        stats["present"]=c.execute("SELECT COUNT(*) FROM attendance a JOIN teacher_courses tc ON tc.course_id=a.course_id WHERE tc.teacher_id=? AND date(a.check_in_time)=date('now') AND a.status='Present'",(session["account_id"],)).fetchone()[0]
        stats["absent"]=c.execute("SELECT COUNT(*) FROM attendance a JOIN teacher_courses tc ON tc.course_id=a.course_id WHERE tc.teacher_id=? AND date(a.check_in_time)=date('now') AND a.status='Absent'",(session["account_id"],)).fetchone()[0]
        alerts=c.execute("SELECT st.full_name,st.phone,c.name,COUNT(*) missed FROM attendance a JOIN students st ON st.id=a.student_id JOIN courses c ON c.id=a.course_id JOIN teacher_courses tc ON tc.course_id=c.id WHERE tc.teacher_id=? AND a.status='Absent' GROUP BY st.id,c.id HAVING missed>=4",(session["account_id"],)).fetchall(); c.close()
    else:
        stats=get_stats(); recent=get_recent_attendance(limit=8)
        c=get_db_connection(); alerts=c.execute("SELECT st.full_name,st.phone,c.name,COUNT(*) missed FROM attendance a JOIN students st ON st.id=a.student_id JOIN courses c ON c.id=a.course_id WHERE a.status='Absent' GROUP BY st.id,c.id HAVING missed>=4").fetchall(); c.close()
    conn = get_db_connection()
    sync_absences(conn)
    today = conn.execute("SELECT date('now')").fetchone()[0]
    teacher_filter=" AND s.course_id IN (SELECT course_id FROM teacher_courses WHERE teacher_id = ?)" if session.get("role")=="teacher" else ""
    today_params=(today,session["account_id"]) if session.get("role")=="teacher" else (today,)
    today_sessions = conn.execute(
        f"""
         SELECT s.id, s.course_id, s.session_date, s.start_time, s.end_time, s.status,
             c.name,
               c.name AS course_name,
               COUNT(DISTINCT e.student_id) AS enrolled_count,
               COUNT(DISTINCT CASE WHEN a.status = 'Present' THEN a.student_id END) AS present_count
        FROM sessions s
        JOIN courses c ON c.id = s.course_id
        LEFT JOIN enrollments e ON e.course_id = s.course_id
        LEFT JOIN attendance a ON a.session_id = s.id AND a.student_id = e.student_id
        WHERE s.session_date = ? {teacher_filter}
        GROUP BY s.id
        ORDER BY s.start_time ASC
        """,
        today_params,
    ).fetchall()

    now = datetime.now()
    today_session_cards = []
    for item in today_sessions:
        ended = now >= parse_session_time(item, "end_time")
        absentees = []
        if ended:
            absentees = conn.execute(
                """
                SELECT st.full_name
                FROM enrollments e
                JOIN students st ON st.id = e.student_id
                WHERE e.course_id = ? AND st.active = 1
                  AND NOT EXISTS (
                      SELECT 1 FROM attendance a
                      WHERE a.session_id = ? AND a.student_id = st.id AND a.status = 'Present'
                  )
                ORDER BY st.full_name ASC
                """,
                (item["course_id"], item["id"]),
            ).fetchall()
        today_session_cards.append(
            {
                "course_name": item["course_name"],
                "session_date": item["session_date"],
                "start_time": display_time(item["start_time"]),
                "end_time": display_time(item["end_time"]),
                "enrolled_count": item["enrolled_count"],
                "present_count": item["present_count"],
                "absent_count": len(absentees),
                "ended": ended,
                "absentees": absentees,
            }
        )
    conn.close()
    open_sessions = [item for item in today_sessions if item["status"] == "open"]
    return render_template(
        "dashboard.html",
        stats=stats,
        recent=recent,
        open_sessions=open_sessions,
        today_sessions=today_session_cards,
        alerts=alerts,
    )


@app.route("/students", methods=["GET", "POST"])
def students():
    conn = get_db_connection()
    q = request.args.get("q", "")
    if request.method == "POST":
        action = request.form.get("action")
        student_id = request.form.get("student_id")
        full_name = request.form.get("full_name", "").strip()
        student_code = request.form.get("student_code", "").strip()
        phone = request.form.get("phone", "").strip()
        email = request.form.get("email", "").strip()
        grade = request.form.get("grade", "").strip()
        nfc_uid = normalize_uid(request.form.get("nfc_uid"))

        if action == "delete":
            if student_id:
                conn.execute("UPDATE students SET active = 0 WHERE id = ?", (student_id,))
                conn.commit()
            flash("Student deactivated.", "success")
            conn.close()
            return redirect(url_for("students"))

        if not full_name or not student_code:
            flash("Full name and student code are required.", "error")
            conn.close()
            return redirect(url_for("students"))

        if student_id:
            conn.execute(
                """
                UPDATE students
                SET student_code = ?, full_name = ?, phone = ?, email = ?, grade = ?, nfc_uid = ?
                WHERE id = ?
                """,
                (student_code, full_name, phone, email, grade, nfc_uid, student_id),
            )
            flash("Student details updated.", "success")
        else:
            conn.execute(
                "INSERT INTO students (student_code, full_name, phone, email, grade, nfc_uid) VALUES (?, ?, ?, ?, ?, ?)",
                (student_code, full_name, phone, email, grade, nfc_uid),
            )
            flash("Student added successfully.", "success")
        conn.commit()
        conn.close()
        return redirect(url_for("students"))

    if q:
        like = f"%{q}%"
        rows = conn.execute(
            "SELECT * FROM students WHERE active = 1 AND (full_name LIKE ? OR student_code LIKE ? OR email LIKE ?) ORDER BY full_name ASC",
            (like, like, like),
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM students WHERE active = 1 ORDER BY full_name ASC").fetchall()
    conn.close()
    return render_template("students.html", students=rows, search=q)


@app.route("/student/<int:student_id>", methods=["GET", "POST"])
def student_details(student_id):
    if request.method == "POST":
        full_name=request.form.get("full_name","").strip()
        student_code=request.form.get("student_code","").strip()
        phone=request.form.get("phone","").strip()
        email=request.form.get("email","").strip().lower()
        grade=request.form.get("grade","").strip()
        nfc_uid=normalize_uid(request.form.get("nfc_uid"))
        if not full_name or not student_code:
            flash("Student name and code are required.","error")
            return redirect(url_for("student_details",student_id=student_id))
        conn=get_db_connection()
        if not conn.execute("SELECT 1 FROM students WHERE id=?",(student_id,)).fetchone():
            conn.close(); flash("Student not found.","error"); return redirect(url_for("students"))
        linked_account=conn.execute("SELECT id FROM accounts WHERE student_id=? AND role='student'",(student_id,)).fetchone()
        if linked_account and not email:
            conn.close(); flash("A student with a login account must have an email address.","error"); return redirect(url_for("student_details",student_id=student_id))
        duplicate_email=conn.execute("SELECT id FROM accounts WHERE lower(email)=? AND id<>COALESCE(?, -1)",(email,linked_account["id"] if linked_account else None)).fetchone() if email else None
        if duplicate_email:
            conn.close(); flash("That email is already used by another account.","error"); return redirect(url_for("student_details",student_id=student_id))
        try:
            conn.execute("UPDATE students SET full_name=?,student_code=?,phone=?,email=?,grade=?,nfc_uid=? WHERE id=?",(full_name,student_code,phone,email,grade,nfc_uid,student_id))
            if linked_account:
                conn.execute("UPDATE accounts SET email=? WHERE id=?",(email,linked_account["id"]))
            conn.commit(); flash("Student details saved.","success")
        except sqlite3.IntegrityError:
            conn.rollback(); flash("That student code is already assigned to another student.","error")
        conn.close()
        return redirect(url_for("student_details",student_id=student_id))

    details = get_student_details(student_id)
    if not details:
        flash("Student not found.", "error")
        return redirect(url_for("students"))

    conn = get_db_connection()
    all_courses = conn.execute("SELECT * FROM courses WHERE active = 1 ORDER BY name ASC").fetchall()
    conn.close()
    return render_template("student_details.html", **details, all_courses=all_courses)


@app.route("/student/<int:student_id>/enroll", methods=["POST"])
def enroll_student(student_id):
    course_id = request.form.get("course_id")
    if not course_id:
        flash("Please select a course.", "error")
        return redirect(url_for("student_details", student_id=student_id))

    conn = get_db_connection()
    conn.execute(
        "INSERT OR IGNORE INTO enrollments (student_id, course_id) VALUES (?, ?)",
        (student_id, course_id),
    )
    conn.commit()
    conn.close()
    flash("Student enrolled successfully.", "success")
    return redirect(url_for("student_details", student_id=student_id))


@app.route("/courses", methods=["GET", "POST"])
def courses():
    conn = get_db_connection()
    if request.method == "POST":
        action = request.form.get("action")
        course_id = request.form.get("course_id")
        name = request.form.get("name", "").strip()
        code = request.form.get("code", "").strip()
        description = request.form.get("description", "").strip()

        if action == "archive":
            if course_id:
                conn.execute("UPDATE courses SET active = 0 WHERE id = ?", (course_id,))
                conn.commit()
            flash("Course archived. Its sessions and attendance history were preserved.", "success")
            conn.close()
            return redirect(url_for("courses"))

        if action == "delete":
            if course_id:
                linked = conn.execute("""
                    SELECT
                      EXISTS(SELECT 1 FROM sessions WHERE course_id = ?) OR
                      EXISTS(SELECT 1 FROM enrollments WHERE course_id = ?) OR
                      EXISTS(SELECT 1 FROM teacher_courses WHERE course_id = ?) OR
                      EXISTS(SELECT 1 FROM course_materials WHERE course_id = ?)
                """, (course_id, course_id, course_id, course_id)).fetchone()[0]
                if linked:
                    conn.execute("UPDATE courses SET active=0 WHERE id=?", (course_id,))
                    conn.commit()
                    flash("Course removed from active lists. Its sessions, enrollments, and attendance history were preserved.", "success")
                else:
                    conn.execute("DELETE FROM courses WHERE id = ?", (course_id,))
                    conn.commit()
                    flash("Course permanently deleted.", "success")
            conn.close()
            return redirect(url_for("courses"))

        if not name or not code:
            flash("Course name and course code are required.", "error")
            conn.close()
            return redirect(url_for("courses"))

        if course_id:
            conn.execute(
                "UPDATE courses SET name = ?, code = ?, description = ? WHERE id = ?",
                (name, code, description, course_id),
            )
            flash("Course updated.", "success")
        else:
            conn.execute(
                "INSERT INTO courses (name, code, description) VALUES (?, ?, ?)",
                (name, code, description),
            )
            flash("Course created.", "success")
        conn.commit()
        conn.close()
        return redirect(url_for("courses"))

    q = request.args.get("q", "")
    if q:
        like = f"%{q}%"
        rows = conn.execute(
            "SELECT * FROM courses WHERE active = 1 AND (name LIKE ? OR code LIKE ? OR description LIKE ?) ORDER BY name ASC",
            (like, like, like),
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM courses WHERE active = 1 ORDER BY name ASC").fetchall()
    conn.close()
    return render_template("courses.html", courses=rows, search=q)


@app.route("/sessions", methods=["GET", "POST"])
def sessions():
    conn = get_db_connection()
    if request.method == "POST":
        action = request.form.get("action")
        session_id = request.form.get("session_id")
        course_id = request.form.get("course_id")
        date_from = request.form.get("date_from") or request.form.get("session_date")
        date_to = request.form.get("date_to") or date_from
        start_time = normalize_session_time(request.form.get("start_time"))
        end_time = normalize_session_time(request.form.get("end_time"))

        if action == "delete":
            if session_id:
                conn.execute("DELETE FROM attendance WHERE session_id=?",(session_id,))
                conn.execute("DELETE FROM sessions WHERE id=?",(session_id,))
                conn.commit()
                flash("Session and its attendance records were deleted.","success")
            conn.close()
            return redirect(url_for("sessions"))

        if action == "toggle":
            current = conn.execute("SELECT status FROM sessions WHERE id = ?", (session_id,)).fetchone()
            if current:
                new_status = "closed" if current["status"] == "open" else "open"
                conn.execute("UPDATE sessions SET status = ? WHERE id = ?", (new_status, session_id))
                conn.commit()
                flash("Session status updated.", "success")
            conn.close()
            return redirect(url_for("sessions"))

        if not all([course_id, date_from, date_to, start_time, end_time]):
            flash("Complete all fields and enter times in AM/PM format, such as 09:00 AM.", "error")
            conn.close()
            return redirect(url_for("sessions"))
        if end_time <= start_time:
            flash("End time must be later than start time.","error")
            conn.close()
            return redirect(url_for("sessions"))
        try:
            first_date=date.fromisoformat(date_from); last_date=date.fromisoformat(date_to)
        except ValueError:
            conn.close(); flash("Enter valid start and end dates.", "error"); return redirect(url_for("sessions"))
        if last_date < first_date or (last_date-first_date).days > 365:
            conn.close(); flash("The end date must be on or after the start date, with a maximum range of one year.", "error"); return redirect(url_for("sessions"))

        days=[(first_date+timedelta(days=i)).isoformat() for i in range((last_date-first_date).days+1)]
        created=0
        if session_id:
            existing=conn.execute("SELECT id FROM sessions WHERE id=?",(session_id,)).fetchone()
            if not existing:
                conn.close(); flash("Session not found.","error"); return redirect(url_for("sessions"))
            conn.execute("UPDATE sessions SET course_id=?,session_date=?,start_time=?,end_time=? WHERE id=?",(course_id,days[0],start_time,end_time,session_id))
            created=1
            days=days[1:]
        for day in days:
            duplicate=conn.execute("SELECT id FROM sessions WHERE course_id=? AND session_date=? AND start_time=? AND end_time=?",(course_id,day,start_time,end_time)).fetchone()
            if not duplicate:
                conn.execute("INSERT INTO sessions(course_id,session_date,start_time,end_time,status) VALUES(?,?,?,?, 'open')",(course_id,day,start_time,end_time)); created+=1
        if not session_id and not created:
            flash("Those sessions already exist for the selected date range.","warning")
        elif session_id:
            flash(f"Session updated and {created-1} additional day(s) scheduled.","success")
        else:
            flash(f"{created} daily session(s) scheduled.","success")
        conn.commit()
        conn.close()
        return redirect(url_for("sessions"))

    courses = conn.execute("SELECT * FROM courses WHERE active = 1 ORDER BY name ASC").fetchall()
    rows = conn.execute(
        """
        SELECT s.*, c.name AS course_name
        FROM sessions s
        JOIN courses c ON c.id = s.course_id
        ORDER BY s.session_date DESC, s.start_time DESC
        """
    ).fetchall()
    edit_id = request.args.get("edit_id")
    edit_session = None
    if edit_id:
        edit_session = conn.execute("SELECT * FROM sessions WHERE id = ?", (edit_id,)).fetchone()
    conn.close()
    return render_template("sessions.html", courses=courses, sessions=rows, edit_session=edit_session, new_session_date=request.args.get("session_date", ""))


@app.route("/attendance", methods=["GET", "POST"])
def attendance():
    conn = get_db_connection()
    courses = conn.execute("SELECT * FROM courses WHERE active = 1 ORDER BY name ASC").fetchall()
    if request.method == "POST":
        course_id = request.form.get("course_id")
        session_id = request.form.get("session_id")
        uid = normalize_uid(request.form.get("nfc_uid") or request.form.get("manual_uid"))
        if not course_id or not session_id:
            flash("Course and session are required.", "error")
            conn.close()
            return render_template("attendance.html", courses=courses, sessions=[], current_course=None, current_session=None, result=None)

        session_row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if not session_row:
            flash("Session not found.", "error")
            conn.close()
            return render_template("attendance.html", courses=courses, sessions=[], current_course=None, current_session=None, result=None)

        if session_row["status"] != "open":
            flash("Session Closed. Attendance is currently unavailable.", "error")
            conn.close()
            return render_template("attendance.html", courses=courses, sessions=[], current_course=session_row, current_session=session_row, result=None)

        if str(session_row["course_id"]) != str(course_id):
            flash("This session does not belong to the selected course.", "error")
            conn.close()
            return render_template("attendance.html", courses=courses, sessions=[], current_course=None, current_session=session_row, result=None)

        now = datetime.now()
        session_start = parse_session_time(session_row, "start_time")
        session_end = parse_session_time(session_row, "end_time")
        if now < session_start:
            flash(f"Session has not started yet. Attendance opens at {display_time(session_row['start_time'])}.", "error")
            conn.close()
            return render_template("attendance.html", courses=courses, sessions=[], current_course=None, current_session=session_row, result=None)
        if now > session_end:
            flash(f"Session ended at {display_time(session_row['end_time'])}. Attendance is closed.", "error")
            conn.close()
            return render_template("attendance.html", courses=courses, sessions=[], current_course=None, current_session=session_row, result=None)
        registration_deadline = session_start + timedelta(minutes=ATTENDANCE_GRACE_MINUTES)
        if now > registration_deadline:
            flash(f"Attendance registration closed {ATTENDANCE_GRACE_MINUTES} minutes after the session started, at {display_time(registration_deadline.strftime('%H:%M'))}.", "error")
            conn.close()
            return render_template("attendance.html", courses=courses, sessions=[], current_course=None, current_session=session_row, result=None)

        student = conn.execute("SELECT * FROM students WHERE active = 1 AND UPPER(nfc_uid) = ?", (uid,),).fetchone()
        if not student:
            flash("Unknown NFC Card. This card is not assigned to a student.", "error")
            conn.close()
            return render_template("attendance.html", courses=courses, sessions=[], current_course=None, current_session=session_row, result=None)

        enrollment = conn.execute(
            "SELECT * FROM enrollments WHERE student_id = ? AND course_id = ?",
            (student["id"], course_id),
        ).fetchone()
        if not enrollment:
            flash("Student Not Enrolled. This student is not enrolled in this course.", "error")
            conn.close()
            return render_template("attendance.html", courses=courses, sessions=[], current_course=None, current_session=session_row, result=None)

        duplicate = conn.execute(
            "SELECT id FROM attendance WHERE student_id = ? AND session_id = ?",
            (student["id"], session_id),
        ).fetchone()
        if duplicate:
            flash("Already Checked In. This student has already been marked present for this session.", "warning")
            conn.close()
            return render_template("attendance.html", courses=courses, sessions=[], current_course=None, current_session=session_row, result=None)

        inserted = conn.execute(
            "INSERT INTO attendance (student_id, course_id, session_id, nfc_uid, status) VALUES (?, ?, ?, ?, 'Present')",
            (student["id"], course_id, session_id, uid),
        )
        course = conn.execute("SELECT * FROM courses WHERE id = ?", (course_id,)).fetchone()
        conn.commit()
        conn.close()
        flash("Attendance Recorded. Student was marked present.", "success")
        return render_template(
            "attendance.html",
            courses=courses,
            sessions=[],
            current_course=course,
            current_session=session_row,
            result={
                "student": student["full_name"],
                "course": course["name"],
                "time": datetime.now().strftime("%H:%M:%S"),
                "uid": uid,
                "session_id": session_id,
            },
        )

    selected_course = request.args.get("course_id")
    if selected_course:
        sessions = conn.execute(
            "SELECT * FROM sessions WHERE course_id = ? ORDER BY session_date DESC, start_time DESC",
            (selected_course,),
        ).fetchall()
    else:
        sessions = []
    conn.close()
    return render_template("attendance.html", courses=courses, sessions=sessions, current_course=None, current_session=None, result=None)


@app.route("/reports")
def reports():
    conn = get_db_connection()
    sync_absences(conn)
    students = conn.execute("SELECT * FROM students WHERE active = 1 ORDER BY full_name ASC").fetchall()
    courses = conn.execute("SELECT * FROM courses WHERE active = 1 ORDER BY name ASC").fetchall()
    sessions = conn.execute("SELECT * FROM sessions ORDER BY session_date DESC, start_time DESC").fetchall()

    student_filter = request.args.get("student_id")
    course_filter = request.args.get("course_id")
    date_filter = request.args.get("date")
    session_filter = request.args.get("session_id")
    status_filter = request.args.get("status")

    query = """
        SELECT a.id, s.full_name AS student, c.name AS course, ss.session_date AS date, a.check_in_time AS check_in_time, a.status
        FROM attendance a
        JOIN students s ON s.id = a.student_id
        JOIN courses c ON c.id = a.course_id
        JOIN sessions ss ON ss.id = a.session_id
        WHERE 1 = 1
    """
    params = []
    if student_filter:
        query += " AND a.student_id = ?"
        params.append(student_filter)
    if course_filter:
        query += " AND a.course_id = ?"
        params.append(course_filter)
    if date_filter:
        query += " AND ss.session_date = ?"
        params.append(date_filter)
    if session_filter:
        query += " AND a.session_id = ?"
        params.append(session_filter)
    if status_filter:
        query += " AND a.status = ?"
        params.append(status_filter)
    if session.get("role") == "teacher":
        query += " AND a.course_id IN (SELECT course_id FROM teacher_courses WHERE teacher_id = ?)"
        params.append(session["account_id"])
        courses = conn.execute("SELECT c.* FROM courses c JOIN teacher_courses tc ON tc.course_id=c.id WHERE tc.teacher_id=? AND c.active=1", (session["account_id"],)).fetchall()
        students = conn.execute("SELECT DISTINCT st.* FROM students st JOIN enrollments e ON e.student_id=st.id JOIN teacher_courses tc ON tc.course_id=e.course_id WHERE tc.teacher_id=? AND st.active=1 ORDER BY st.full_name", (session["account_id"],)).fetchall()
        sessions = conn.execute("SELECT s.* FROM sessions s JOIN teacher_courses tc ON tc.course_id=s.course_id WHERE tc.teacher_id=? ORDER BY s.session_date DESC,s.start_time DESC", (session["account_id"],)).fetchall()

    query += " ORDER BY ss.session_date DESC, a.check_in_time DESC"
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return render_template(
        "reports.html",
        students=students,
        courses=courses,
        sessions=sessions,
        rows=rows,
        filters={
            "student_id": student_filter,
            "course_id": course_filter,
            "date": date_filter,
            "session_id": session_filter,
            "status": status_filter,
        },
    )


def sync_absences(conn):
    """Create absent rows once a scheduled session has ended."""
    now = datetime.now().isoformat(timespec="seconds")
    conn.execute("""
        INSERT OR IGNORE INTO attendance(student_id, course_id, session_id, nfc_uid, status, check_in_time)
        SELECT e.student_id, s.course_id, s.id, '', 'Absent', NULL
        FROM sessions s JOIN enrollments e ON e.course_id=s.course_id
        JOIN students st ON st.id=e.student_id AND st.active=1
        WHERE datetime(s.session_date || ' ' || s.end_time) < datetime(?)
          AND NOT EXISTS (SELECT 1 FROM attendance a WHERE a.session_id=s.id AND a.student_id=e.student_id)
    """, (now,))
    conn.commit()


@app.route("/calendar")
def calendar():
    conn = get_db_connection()
    role, account_id = session.get("role"), session.get("account_id")
    if role == "student":
        rows = conn.execute("""SELECT s.*, c.name course_name FROM sessions s JOIN courses c ON c.id=s.course_id
          JOIN enrollments e ON e.course_id=c.id WHERE e.student_id=? ORDER BY s.session_date,s.start_time""", (session["student_id"],)).fetchall()
    elif role == "teacher":
        rows = conn.execute("""SELECT s.*,c.name course_name FROM sessions s JOIN courses c ON c.id=s.course_id
          JOIN teacher_courses tc ON tc.course_id=c.id WHERE tc.teacher_id=? ORDER BY s.session_date,s.start_time""", (account_id,)).fetchall()
    else:
        rows = conn.execute("SELECT s.*,c.name course_name FROM sessions s JOIN courses c ON c.id=s.course_id ORDER BY s.session_date,s.start_time").fetchall()
    month = request.args.get("month", date.today().strftime("%Y-%m"))
    try: first=date.fromisoformat(month+"-01")
    except ValueError: first=date.today().replace(day=1); month=first.strftime("%Y-%m")
    selected_date=request.args.get("date","")
    try:
        if selected_date: date.fromisoformat(selected_date)
    except ValueError: selected_date=""
    import calendar as cal
    weekday, total=cal.monthrange(first.year,first.month); by_day={}
    for row in rows:
        if row["session_date"].startswith(month): by_day.setdefault(int(row["session_date"][8:10]),[]).append(row)
    weeks=[]; cells=[None]*weekday
    for day in range(1,total+1):
        iso_day=f"{month}-{day:02d}"
        cells.append({"day":day,"date":iso_day,"sessions":by_day.get(day,[])})
        if len(cells)==7: weeks.append(cells); cells=[]
    if cells: weeks.append(cells+[None]*(7-len(cells)))
    prev=(first-timedelta(days=1)).strftime("%Y-%m"); nxt=(date(first.year+(first.month==12), first.month%12+1,1)).strftime("%Y-%m")
    selected_sessions=[row for row in rows if row["session_date"]==selected_date]
    conn.close()
    return render_template("calendar.html", sessions=rows, weeks=weeks, month=month, month_label=first.strftime("%B %Y"), prev_month=prev, next_month=nxt, selected_date=selected_date, selected_sessions=selected_sessions)


@app.route("/teacher/courses")
def teacher_courses():
    if session.get("role") != "teacher": abort(403)
    conn=get_db_connection()
    courses=conn.execute("SELECT c.* FROM courses c JOIN teacher_courses tc ON tc.course_id=c.id WHERE tc.teacher_id=? AND c.active=1 ORDER BY c.name",(session["account_id"],)).fetchall()
    conn.close(); return render_template("teacher_courses.html", courses=courses)


@app.route("/my-attendance")
def student_home():
    if session.get("role") != "student" or not session.get("student_id"):
        return redirect(url_for("dashboard"))
    conn = get_db_connection(); sync_absences(conn)
    sid = session["student_id"]
    my_courses=conn.execute("""SELECT c.id,c.name,c.code,c.description FROM enrollments e
      JOIN courses c ON c.id=e.course_id WHERE e.student_id=? AND c.active=1 ORDER BY c.name""",(sid,)).fetchall()
    rows = conn.execute("""SELECT c.name course,ss.session_date date,a.status,a.check_in_time FROM attendance a
      JOIN courses c ON c.id=a.course_id JOIN sessions ss ON ss.id=a.session_id WHERE a.student_id=? ORDER BY ss.session_date DESC""", (sid,)).fetchall()
    alerts = conn.execute("""SELECT c.name, COUNT(*) n FROM attendance a JOIN courses c ON c.id=a.course_id
      WHERE a.student_id=? AND a.status='Absent' GROUP BY a.course_id HAVING n>=4""", (sid,)).fetchall()
    materials = conn.execute("""SELECT DISTINCT m.*,c.name course_name,COALESCE(v.view_count,0) view_count
      FROM course_materials m JOIN courses c ON c.id=m.course_id JOIN enrollments e ON e.course_id=c.id
      LEFT JOIN material_views v ON v.material_id=m.id AND v.account_id=? WHERE e.student_id=? ORDER BY m.created_at DESC""", (session["account_id"],sid)).fetchall()
    conn.close()
    return render_template("student_home.html", rows=rows, alerts=alerts, materials=materials, my_courses=my_courses)


@app.route("/my-attendance/export")
def student_export():
    if session.get("role") != "student": abort(403)
    conn = get_db_connection(); sync_absences(conn)
    rows = conn.execute("""SELECT c.name,ss.session_date,a.status,a.check_in_time FROM attendance a
      JOIN courses c ON c.id=a.course_id JOIN sessions ss ON ss.id=a.session_id WHERE a.student_id=? ORDER BY ss.session_date""", (session["student_id"],)).fetchall(); conn.close()
    buf=io.StringIO(); writer=csv.writer(buf); writer.writerow(["Course","Date","Status","Check-in"]); writer.writerows([tuple(r) for r in rows])
    return Response(buf.getvalue(), mimetype="text/csv", headers={"Content-Disposition":"attachment; filename=my-attendance.csv"})


@app.route("/reports/export")
def export_attendance():
    course_id=request.args.get("course_id"); period=request.args.get("period","daily"); target=request.args.get("date") or request.args.get("month")
    if not course_id or not target or period not in ("daily","monthly"): abort(400)
    conn=get_db_connection(); sync_absences(conn)
    if session.get("role")=="student":
        clause="AND a.student_id=?"; extra=[session["student_id"]]
    elif session.get("role")=="teacher":
        ok=conn.execute("SELECT 1 FROM teacher_courses WHERE teacher_id=? AND course_id=?",(session["account_id"],course_id)).fetchone()
        if not ok: conn.close(); abort(403)
        clause=""; extra=[]
    else: clause=""; extra=[]
    dateclause="ss.session_date=?" if period=="daily" else "substr(ss.session_date,1,7)=?"
    rows=conn.execute(f"""SELECT st.student_code,st.full_name,c.name,ss.session_date,a.status,a.check_in_time FROM attendance a
      JOIN students st ON st.id=a.student_id JOIN courses c ON c.id=a.course_id JOIN sessions ss ON ss.id=a.session_id
      WHERE a.course_id=? AND {dateclause} {clause} ORDER BY ss.session_date,st.full_name""", [course_id,target,*extra]).fetchall(); conn.close()
    buf=io.StringIO(); writer=csv.writer(buf); writer.writerow(["Student Code","Student","Course","Date","Status","Check-in"]); writer.writerows([tuple(r) for r in rows])
    return Response(buf.getvalue(),mimetype="text/csv",headers={"Content-Disposition":f"attachment; filename=attendance-{period}-{target}.csv"})


@app.route("/courses/<int:course_id>/materials", methods=["GET","POST"])
def course_materials(course_id):
    conn=get_db_connection(); role=session.get("role")
    if role=="student": allowed=conn.execute("SELECT 1 FROM enrollments WHERE student_id=? AND course_id=?",(session["student_id"],course_id)).fetchone()
    elif role=="teacher": allowed=conn.execute("SELECT 1 FROM teacher_courses WHERE teacher_id=? AND course_id=?",(session["account_id"],course_id)).fetchone()
    else: allowed=True
    if not allowed: conn.close(); abort(403)
    if request.method=="POST":
        if role not in ("teacher","admin"): conn.close(); abort(403)
        if request.form.get("action")=="delete_material":
            try: material_id=int(request.form.get("material_id", ""))
            except ValueError: conn.close(); flash("Material not found.","error"); return redirect(url_for("course_materials",course_id=course_id))
            material=conn.execute("SELECT id,filename FROM course_materials WHERE id=? AND course_id=?",(material_id,course_id)).fetchone()
            if not material:
                conn.close(); flash("Material not found.","error"); return redirect(url_for("course_materials",course_id=course_id))
            conn.execute("DELETE FROM material_views WHERE material_id=?",(material_id,))
            conn.execute("DELETE FROM course_materials WHERE id=?",(material_id,))
            conn.commit(); conn.close()
            (Path(app.root_path)/"private_uploads"/material["filename"]).unlink(missing_ok=True)
            flash("Material deleted.","success")
            return redirect(url_for("course_materials",course_id=course_id))
        upload=request.files.get("file"); title=request.form.get("title","").strip()
        if not upload or not upload.filename or not title: conn.close(); flash("Add a title and choose a file.","error"); return redirect(url_for("course_materials",course_id=course_id))
        original=secure_filename(upload.filename); ext=Path(original).suffix.lower(); kind="pdf" if ext==".pdf" else "video" if ext in {".mp4",".webm",".mov"} else None
        if not kind: conn.close(); flash("Upload a PDF or MP4, WebM, or MOV video.","error"); return redirect(url_for("course_materials",course_id=course_id))
        folder=Path(app.root_path)/"private_uploads"; folder.mkdir(parents=True,exist_ok=True); stored=f"{datetime.now().timestamp():.0f}-{original}"; upload.save(folder/stored)
        conn.execute("INSERT INTO course_materials(course_id,title,filename,kind,uploaded_by) VALUES(?,?,?,?,?)",(course_id,title,stored,kind,session["account_id"])); conn.commit(); conn.close()
        flash("Material added.","success"); return redirect(url_for("course_materials",course_id=course_id))
    rows=conn.execute("""SELECT m.*,COALESCE(v.view_count,0) view_count FROM course_materials m LEFT JOIN material_views v
      ON v.material_id=m.id AND v.account_id=? WHERE m.course_id=? ORDER BY m.created_at DESC""",(session["account_id"],course_id)).fetchall()
    course=conn.execute("SELECT name FROM courses WHERE id=?",(course_id,)).fetchone(); conn.close()
    if not course: abort(404)
    return render_template("materials.html",materials=rows,course=course,course_id=course_id)


def can_manage_course_materials(course_id):
    role=session.get("role")
    if role not in ("teacher", "admin"):
        return False
    if role == "admin":
        return True
    conn=get_db_connection()
    allowed=conn.execute("SELECT 1 FROM teacher_courses WHERE teacher_id=? AND course_id=?",(session["account_id"],course_id)).fetchone()
    conn.close()
    return bool(allowed)


@app.route("/courses/<int:course_id>/materials/upload-manager")
def material_upload_manager(course_id):
    if not can_manage_course_materials(course_id): abort(403)
    conn=get_db_connection()
    course=conn.execute("SELECT id,name FROM courses WHERE id=?",(course_id,)).fetchone()
    conn.close()
    if not course: abort(404)
    return render_template("material_upload_manager.html",course=course)


@app.route("/courses/<int:course_id>/materials/upload/start", methods=["POST"])
def start_material_upload(course_id):
    if not can_manage_course_materials(course_id): abort(403)
    data=request.get_json(silent=True) or {}
    title=str(data.get("title", "")).strip()
    filename=secure_filename(str(data.get("filename", "")))
    try: size=int(data.get("size", 0))
    except (TypeError, ValueError): size=0
    ext=Path(filename).suffix.lower()
    if not title or ext not in {".pdf", ".mp4", ".webm", ".mov"} or size < 1:
        return {"error":"Add a title and choose a PDF or supported video."},400
    if size > 1024 * 1024 * 1024:
        return {"error":"Files must be smaller than 1 GB."},413
    upload_id=uuid.uuid4().hex
    folder=Path(app.root_path)/"private_uploads"/".upload_chunks"
    folder.mkdir(parents=True,exist_ok=True)
    meta={"course_id":course_id,"title":title,"filename":filename,"size":size,"uploaded":0,"next_chunk":0,"account_id":session["account_id"]}
    (folder/f"{upload_id}.json").write_text(json.dumps(meta),encoding="utf-8")
    (folder/f"{upload_id}.part").touch()
    return {"upload_id":upload_id,"chunk_size":512*1024}


def read_upload_metadata(upload_id):
    if len(upload_id)!=32 or any(ch not in "0123456789abcdef" for ch in upload_id): abort(404)
    folder=Path(app.root_path)/"private_uploads"/".upload_chunks"
    meta_path=folder/f"{upload_id}.json"
    try: meta=json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError): abort(404)
    if meta.get("account_id")!=session.get("account_id") or not can_manage_course_materials(meta.get("course_id")): abort(403)
    return folder,meta_path,meta


@app.route("/materials/upload/<upload_id>/chunk/<int:index>", methods=["POST"])
def upload_material_chunk(upload_id,index):
    folder,meta_path,meta=read_upload_metadata(upload_id)
    if request.content_length and request.content_length>512*1024: abort(413)
    chunk=request.get_data(cache=False)
    if index!=meta["next_chunk"] or not chunk or len(chunk)>512*1024 or meta["uploaded"]+len(chunk)>meta["size"]:
        return {"error":"Upload chunk is out of order or invalid."},400
    with (folder/f"{upload_id}.part").open("ab") as target: target.write(chunk)
    meta["uploaded"]+=len(chunk); meta["next_chunk"]+=1
    meta_path.write_text(json.dumps(meta),encoding="utf-8")
    return {"uploaded":meta["uploaded"],"size":meta["size"]}


@app.route("/materials/upload/<upload_id>/complete", methods=["POST"])
def complete_material_upload(upload_id):
    folder,meta_path,meta=read_upload_metadata(upload_id)
    part_path=folder/f"{upload_id}.part"
    if meta["uploaded"]!=meta["size"] or not part_path.is_file() or part_path.stat().st_size!=meta["size"]:
        return {"error":"The upload is incomplete. Please try again."},400
    stored=f"{datetime.now().timestamp():.0f}-{upload_id[:8]}-{meta['filename']}"
    destination=Path(app.root_path)/"private_uploads"/stored
    os.replace(part_path,destination)
    kind="pdf" if Path(meta["filename"]).suffix.lower()==".pdf" else "video"
    conn=get_db_connection()
    conn.execute("INSERT INTO course_materials(course_id,title,filename,kind,uploaded_by) VALUES(?,?,?,?,?)",(meta["course_id"],meta["title"],stored,kind,session["account_id"]))
    conn.commit(); conn.close()
    meta_path.unlink(missing_ok=True)
    return {"ok":True,"redirect":url_for("course_materials",course_id=meta["course_id"])}


@app.route("/materials/<int:material_id>/download")
def download_material(material_id):
    conn, m = get_authorized_material(material_id)
    if m["kind"] != "pdf":
        conn.close(); abort(400)
    path=Path(app.root_path)/"private_uploads"/m["filename"]; conn.close()
    if not path.is_file(): abort(404)
    return send_file(path,as_attachment=True,download_name=m["filename"])

@app.route("/materials/<int:material_id>/view")
def view_video(material_id):
    conn, m = get_authorized_material(material_id)
    if m["kind"] != "video":
        conn.close(); abort(400)
    if session.get("role") == "student":
        row=conn.execute("SELECT view_count FROM material_views WHERE material_id=? AND account_id=?",(material_id,session["account_id"])).fetchone()
        count=row[0] if row else 0
        if count>=2: conn.close(); abort(403,"Video view limit reached.")
        conn.execute("INSERT INTO material_views(material_id,account_id,view_count) VALUES(?,?,1) ON CONFLICT(material_id,account_id) DO UPDATE SET view_count=view_count+1",(material_id,session["account_id"]))
        conn.commit()
    filename=m["filename"]
    conn.close()
    ext=Path(filename).suffix.lower()
    video_mime={".mp4":"video/mp4",".webm":"video/webm",".mov":"video/quicktime"}.get(ext,"video/mp4")
    return render_template("video_player.html", material=m, video_mime=video_mime)

def get_authorized_material(material_id):
    conn=get_db_connection(); m=conn.execute("SELECT * FROM course_materials WHERE id=?",(material_id,)).fetchone()
    if not m: conn.close(); abort(404)
    role=session.get("role"); cid=m["course_id"]
    if role=="student": allowed=conn.execute("SELECT 1 FROM enrollments WHERE student_id=? AND course_id=?",(session["student_id"],cid)).fetchone()
    elif role=="teacher": allowed=conn.execute("SELECT 1 FROM teacher_courses WHERE teacher_id=? AND course_id=?",(session["account_id"],cid)).fetchone()
    else: allowed=True
    if not allowed: conn.close(); abort(403)
    return conn,m

@app.route("/materials/<int:material_id>/stream")
def stream_video(material_id):
    conn,m=get_authorized_material(material_id)
    if m["kind"]!="video": conn.close(); abort(400)
    if session.get("role")=="student":
        row=conn.execute("SELECT view_count FROM material_views WHERE material_id=? AND account_id=?",(material_id,session["account_id"])).fetchone()
        if not row or row[0]<1: conn.close(); abort(403)
    path=Path(app.root_path)/"private_uploads"/m["filename"]; conn.close()
    if not path.is_file(): abort(404)
    ext=path.suffix.lower()
    mime={".mp4":"video/mp4",".webm":"video/webm",".mov":"video/quicktime"}.get(ext,"application/octet-stream")
    response=send_file(path,mimetype=mime,as_attachment=False,download_name=m["filename"],conditional=True)
    response.headers["Cache-Control"]="private, no-store"
    return response


@app.route("/accounts", methods=["GET","POST"])
def accounts():
    if session.get("role")!="admin": abort(403)
    conn=get_db_connection()
    if request.method=="POST":
        action=request.form.get("action")
        if action=="delete":
            try: target_id=int(request.form.get("account_id", ""))
            except ValueError: conn.close(); flash("Account not found.","error"); return redirect(url_for("accounts"))
            target=conn.execute("SELECT id,role,active FROM accounts WHERE id=?",(target_id,)).fetchone()
            if not target:
                flash("Account not found.","error")
            elif target_id==session.get("account_id"):
                flash("You cannot delete the account you are currently using.","error")
            elif target["role"]=="admin" and target["active"]:
                admin_count=conn.execute("SELECT COUNT(*) FROM accounts WHERE role='admin' AND active=1").fetchone()[0]
                if admin_count<=1:
                    flash("The last active admin account cannot be deleted.","error")
                else:
                    conn.execute("DELETE FROM teacher_courses WHERE teacher_id=?",(target_id,))
                    conn.execute("DELETE FROM material_views WHERE account_id=?",(target_id,))
                    conn.execute("UPDATE course_materials SET uploaded_by=NULL WHERE uploaded_by=?",(target_id,))
                    conn.execute("DELETE FROM accounts WHERE id=?",(target_id,)); conn.commit(); flash("Account deleted.","success")
            else:
                conn.execute("DELETE FROM teacher_courses WHERE teacher_id=?",(target_id,))
                conn.execute("DELETE FROM material_views WHERE account_id=?",(target_id,))
                conn.execute("UPDATE course_materials SET uploaded_by=NULL WHERE uploaded_by=?",(target_id,))
                conn.execute("DELETE FROM accounts WHERE id=?",(target_id,)); conn.commit(); flash("Account deleted.","success")
            conn.close(); return redirect(url_for("accounts"))
        if action=="disable":
            conn.execute("UPDATE accounts SET active=0 WHERE id=? AND role!='admin'",(request.form.get("account_id"),)); conn.commit(); conn.close(); flash("Account disabled.","success"); return redirect(url_for("accounts"))
        if action=="password":
            new_password=request.form.get("new_password","")
            if len(new_password)<8: conn.close(); flash("Password must be at least 8 characters.","error"); return redirect(url_for("accounts"))
            conn.execute("UPDATE accounts SET password_hash=? WHERE id=?",(generate_password_hash(new_password),request.form.get("account_id"))); conn.commit(); conn.close(); flash("Password updated.","success"); return redirect(url_for("accounts"))
        email=request.form.get("email","").strip().lower(); password=request.form.get("password",""); role=request.form.get("role"); student_id=None
        if role not in {"student","teacher","admin"} or len(password)<8 or "@" not in email:
            conn.close(); flash("Use a valid email, role, and password of at least 8 characters.","error"); return redirect(url_for("accounts"))
        if role=="student":
            student_code=request.form.get("student_code","").strip()
            student=conn.execute("SELECT id FROM students WHERE active=1 AND lower(trim(student_code))=lower(?)",(student_code,)).fetchone() if student_code else None
            if not student and not student_code:
                student=conn.execute("SELECT id FROM students WHERE active=1 AND lower(email)=? ORDER BY id LIMIT 1",(email,)).fetchone()
            if not student:
                conn.close(); flash("Enter the student code from the Students page so the account can be linked and the email saved.","error"); return redirect(url_for("accounts"))
            student_id=student["id"]
            duplicate_email=conn.execute("SELECT id FROM students WHERE active=1 AND lower(email)=? AND id<>?",(email,student_id)).fetchone()
            if duplicate_email:
                conn.close(); flash("That email is already assigned to another student.","error"); return redirect(url_for("accounts"))
            conn.execute("UPDATE students SET email=? WHERE id=?",(email,student_id))
        try:
            conn.execute("INSERT INTO accounts(email,password_hash,role,student_id) VALUES(?,?,?,?)",(email,generate_password_hash(password),role,student_id)); aid=conn.execute("SELECT id FROM accounts WHERE email=?",(email,)).fetchone()[0]
            if role=="teacher":
                for cid in request.form.getlist("course_ids"): conn.execute("INSERT OR IGNORE INTO teacher_courses(teacher_id,course_id) VALUES(?,?)",(aid,cid))
            conn.commit(); flash("Account created.","success")
        except Exception: conn.rollback(); flash("That email already has an account or the details are invalid.","error")
        conn.close(); return redirect(url_for("accounts"))
    account_search=request.args.get("q","").strip()
    account_query="SELECT a.*,s.full_name FROM accounts a LEFT JOIN students s ON s.id=a.student_id"
    if account_search:
        like=f"%{account_search}%"
        account_query+=" WHERE a.email LIKE ? OR a.role LIKE ? OR s.full_name LIKE ? OR s.student_code LIKE ?"
        users=conn.execute(account_query+" ORDER BY a.id DESC",(like,like,like,like)).fetchall()
    else:
        users=conn.execute(account_query+" ORDER BY a.id DESC").fetchall()
    courses=conn.execute("SELECT id,name,code FROM courses WHERE active=1 ORDER BY name").fetchall(); conn.close()
    return render_template("accounts.html",accounts=users,courses=courses,account_search=account_search)


@app.route("/settings", methods=["GET", "POST"])
def settings():
    if request.method == "POST":
        action = request.form.get("action")
        email = request.form.get("email", "").strip().lower()
        conn = get_db_connection()

        if action == "add":
            if "@" not in email or "." not in email.rsplit("@", 1)[-1]:
                flash("Enter a valid email address.", "error")
            else:
                conn.execute(
                    "INSERT OR IGNORE INTO allowed_emails (email) VALUES (?)",
                    (email,),
                )
                conn.commit()
                flash("Login email added.", "success")
        elif action == "remove":
            conn.execute("DELETE FROM allowed_emails WHERE id = ?", (request.form.get("email_id"),))
            conn.commit()
            flash("Login email removed.", "success")

        conn.close()
        return redirect(url_for("settings"))

    return render_template("settings.html", allowed_emails=get_allowed_emails())


@app.route("/api/simulate-scan", methods=["POST"])
def simulate_scan():
    raw_uid = request.form.get("nfc_uid")
    uid = simulate_nfc_scan(raw_uid)
    if not uid:
        return {"ok": False, "message": "Please provide a valid UID."}, 400
    return {"ok": True, "uid": uid}


init_db()
seed_data()

# Upgrade legacy allowed-email logins into admin accounts. Their first login
# sets a password, avoiding a shared default credential.
_conn = get_db_connection()
for _legacy in _conn.execute("SELECT email FROM allowed_emails").fetchall():
    _conn.execute("INSERT OR IGNORE INTO accounts(email,password_hash,role) VALUES(?,'', 'admin')", (_legacy["email"],))
_conn.commit()
_conn.close()


if __name__ == "__main__":
    print("NFC Attendance System")
    print("Server running on:")
    print(f"http://127.0.0.1:5000")
    print(f"Network access: {get_local_network_url()}")
    app.run(host="0.0.0.0", port=5000, debug=True)
