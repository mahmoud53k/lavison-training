import sqlite3
from pathlib import Path

DB_DIR = Path(__file__).resolve().parent / "database"
DB_PATH = DB_DIR / "attendance.db"


def get_db_connection():
    DB_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def normalize_uid(uid: str | None) -> str:
    if not uid:
        return ""
    cleaned = "".join(ch for ch in str(uid).upper() if ch.isalnum())
    return cleaned


def init_db():
    conn = get_db_connection()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS students (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_code TEXT UNIQUE,
            full_name TEXT NOT NULL,
            phone TEXT,
            email TEXT,
            grade TEXT,
            nfc_uid TEXT,
            active INTEGER DEFAULT 1,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS courses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            code TEXT UNIQUE,
            description TEXT,
            active INTEGER DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS enrollments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id INTEGER NOT NULL,
            course_id INTEGER NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(student_id, course_id)
        );

        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            course_id INTEGER NOT NULL,
            session_date TEXT NOT NULL,
            start_time TEXT NOT NULL,
            end_time TEXT NOT NULL,
            status TEXT DEFAULT 'open'
        );

        CREATE TABLE IF NOT EXISTS attendance (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id INTEGER NOT NULL,
            course_id INTEGER NOT NULL,
            session_id INTEGER NOT NULL,
            nfc_uid TEXT NOT NULL,
            check_in_time TEXT DEFAULT CURRENT_TIMESTAMP,
            status TEXT DEFAULT 'Present',
            UNIQUE(session_id, student_id)
        );

        CREATE TABLE IF NOT EXISTS allowed_emails (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('admin','teacher','student')),
            student_id INTEGER,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS teacher_courses (
            teacher_id INTEGER NOT NULL,
            course_id INTEGER NOT NULL,
            UNIQUE(teacher_id, course_id)
        );
        CREATE TABLE IF NOT EXISTS course_materials (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            course_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            filename TEXT NOT NULL,
            kind TEXT NOT NULL CHECK(kind IN ('pdf','video')),
            uploaded_by INTEGER,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS material_views (
            material_id INTEGER NOT NULL,
            account_id INTEGER NOT NULL,
            view_count INTEGER NOT NULL DEFAULT 0,
            UNIQUE(material_id, account_id)
        );
        """
    )
    conn.commit()
    conn.close()


def seed_data():
    conn = get_db_connection()
    conn.execute(
        "INSERT OR IGNORE INTO allowed_emails (email) VALUES (?)",
        ("mahawad26@gmail.com",),
    )
    student_count = conn.execute("SELECT COUNT(*) AS c FROM students").fetchone()["c"]
    if student_count == 0:
        students = [
            ("ST001", "Ahmed Mohamed", "0100000001", "ahmed@example.com", "Grade 10", "04A1B2C3D4"),
            ("ST002", "Mahmoud Ali", "0100000002", "mahmoud@example.com", "Grade 11", "04B2C3D4E5"),
            ("ST003", "Omar Hassan", "0100000003", "omar@example.com", "Grade 12", "04C3D4E5F6"),
        ]
        conn.executemany(
            "INSERT INTO students (student_code, full_name, phone, email, grade, nfc_uid) VALUES (?, ?, ?, ?, ?, ?)",
            students,
        )

        courses = [
            ("Python", "PY101", "Intro to Python programming"),
            ("Networking", "NET101", "Computer networking essentials"),
            ("Cyber Security", "SEC101", "Cyber defense fundamentals"),
        ]
        conn.executemany(
            "INSERT INTO courses (name, code, description) VALUES (?, ?, ?)",
            courses,
        )

        conn.execute(
            "INSERT INTO enrollments (student_id, course_id) VALUES (1, 1), (1, 2), (2, 1), (2, 3), (3, 2), (3, 3)"
        )

        today = conn.execute("SELECT date('now')").fetchone()[0]
        conn.execute(
            "INSERT INTO sessions (course_id, session_date, start_time, end_time, status) VALUES (1, ?, '18:00:00', '20:00:00', 'open'), (2, ?, '19:00:00', '21:00:00', 'open')",
            (today, today),
        )

    conn.commit()
    conn.close()


def get_allowed_emails():
    conn = get_db_connection()
    rows = conn.execute(
        "SELECT id, email, created_at FROM allowed_emails ORDER BY email ASC"
    ).fetchall()
    conn.close()
    return rows


def get_stats():
    conn = get_db_connection()
    total_students = conn.execute("SELECT COUNT(*) AS c FROM students WHERE active = 1").fetchone()["c"]
    total_courses = conn.execute("SELECT COUNT(*) AS c FROM courses WHERE active = 1").fetchone()["c"]
    today = conn.execute("SELECT date('now')").fetchone()[0]
    todays_sessions = conn.execute(
        "SELECT COUNT(*) AS c FROM sessions WHERE session_date = ?",
        (today,),
    ).fetchone()["c"]
    todays_attendance = conn.execute(
        "SELECT COUNT(*) AS c FROM attendance WHERE date(check_in_time) = ?",
        (today,),
    ).fetchone()["c"]
    present = conn.execute(
        "SELECT COUNT(*) AS c FROM attendance WHERE date(check_in_time) = ? AND status = 'Present'",
        (today,),
    ).fetchone()["c"]
    absent = conn.execute(
        "SELECT COUNT(*) AS c FROM attendance WHERE date(check_in_time) = ? AND status = 'Absent'",
        (today,),
    ).fetchone()["c"]
    conn.close()
    return {
        "total_students": total_students,
        "total_courses": total_courses,
        "todays_sessions": todays_sessions,
        "todays_attendance": todays_attendance,
        "present": present,
        "absent": absent,
    }


def get_recent_attendance(limit=10):
    conn = get_db_connection()
    rows = conn.execute(
        """
        SELECT s.full_name AS student, c.name AS course, a.check_in_time, a.status
        FROM attendance a
        JOIN students s ON s.id = a.student_id
        JOIN courses c ON c.id = a.course_id
        ORDER BY a.check_in_time DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    conn.close()
    return rows


def get_student_by_uid(uid: str):
    conn = get_db_connection()
    row = conn.execute(
        "SELECT * FROM students WHERE active = 1 AND UPPER(nfc_uid) = ?",
        (normalize_uid(uid),),
    ).fetchone()
    conn.close()
    return row


def get_course_by_id(course_id):
    conn = get_db_connection()
    row = conn.execute("SELECT * FROM courses WHERE id = ?", (course_id,)).fetchone()
    conn.close()
    return row


def get_session_by_id(session_id):
    conn = get_db_connection()
    row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    conn.close()
    return row
