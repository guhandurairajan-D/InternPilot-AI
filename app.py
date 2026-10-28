from pathlib import Path
import os
import sqlite3
import smtplib
import threading
import time
from datetime import datetime, timezone, timedelta
from email.message import EmailMessage
from flask import Flask, jsonify, render_template, request
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "internpilot.db"
DB_PATH = BASE_DIR / "internpilot.db"
    
def update_intern_email():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("PRAGMA table_info(interns)")
    columns = [row[1] for row in cursor.fetchall()]
    print("Intern table columns:", columns)

    conn.close()


EMAIL_ADDRESS = os.getenv("EMAIL_ADDRESS", "").strip()
EMAIL_APP_PASSWORD = os.getenv("EMAIL_APP_PASSWORD", "").strip()
EMAIL_ENABLED = bool(EMAIL_ADDRESS and EMAIL_APP_PASSWORD)
SCHEDULER_STARTED = False
SCHEDULER_LOCK = threading.Lock()

SEED_INTERNS = [
    ("IN-003", "D. Guhan", "guhandurairajan@example.com"),
    
]

def connect_db():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn

def init_db():
    with connect_db() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS interns (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, email TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'No task yet', task TEXT NOT NULL DEFAULT '—'
        );
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT, task_code TEXT NOT NULL,
            intern_id TEXT NOT NULL REFERENCES interns(id), title TEXT NOT NULL,
            description TEXT NOT NULL, due_date TEXT NOT NULL, due_time TEXT NOT NULL,
            language TEXT NOT NULL DEFAULT 'en-US', reminder_lead_days INTEGER NOT NULL DEFAULT 2,
            status TEXT NOT NULL DEFAULT 'Assigned', created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT, intern_id TEXT NOT NULL REFERENCES interns(id),
            requirements TEXT NOT NULL, submission TEXT NOT NULL, score INTEGER NOT NULL,
            feedback TEXT NOT NULL, created_at TEXT NOT NULL
        );
        """)
        # Add these tracking columns safely to an existing database.
        existing = {row["name"] for row in db.execute("PRAGMA table_info(tasks)").fetchall()}
        if "lead_reminder_sent" not in existing:
            db.execute("ALTER TABLE tasks ADD COLUMN lead_reminder_sent INTEGER NOT NULL DEFAULT 0")
        if "due_reminder_sent" not in existing:
            db.execute("ALTER TABLE tasks ADD COLUMN due_reminder_sent INTEGER NOT NULL DEFAULT 0")
        if "assignment_email_sent" not in existing:
            db.execute("ALTER TABLE tasks ADD COLUMN assignment_email_sent INTEGER NOT NULL DEFAULT 0")
        for intern in SEED_INTERNS:
            db.execute("INSERT OR IGNORE INTO interns(id,name,email) VALUES(?,?,?)", intern)

def row_to_task(row):
    reminder = "Due date" if row["reminder_lead_days"] == 0 else f'{row["reminder_lead_days"]} day(s) before'
    return {
        "id": row["task_code"], "db_id": row["id"], "intern": row["intern_id"],
        "title": row["title"], "desc": row["description"], "description": row["description"],
        "date": row["due_date"], "time": row["due_time"], "reminder": reminder,
        "status": row["status"], "lang": row["language"], "language": row["language"]
    }

def send_email(to_email, subject, body):
    """Send one email via Gmail SMTP. Returns (success, error_message)."""
    if not EMAIL_ENABLED:
        return False, "Email is not configured. Set EMAIL_ADDRESS and EMAIL_APP_PASSWORD in .env."
    if not to_email or to_email.endswith("@example.com"):
        return False, "This intern has a placeholder email address. Update it to a real recipient first."
    msg = EmailMessage()
    msg["From"] = EMAIL_ADDRESS
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.set_content(body)
    try:
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=20) as server:
            server.starttls()
            server.login(EMAIL_ADDRESS, EMAIL_APP_PASSWORD)
            server.send_message(msg)
        return True, ""
    except Exception as exc:
        app.logger.exception("Email send failed")
        return False, str(exc)

def task_email(intern_name, task):
    subject = f'Reminder — {task["title"]} due {task["due_date"]}'
    body = (
        f"Hi {intern_name},\n\n"
        f"This is a reminder that your task \"{task['title']}\" is due on "
        f"{task['due_date']} at {task['due_time']}.\n\n"
        f"Please complete the following:\n{task['description']}\n\n"
        "Please upload your work before the deadline and be ready to present it. "
        "If you need clarification, reply to your mentor.\n\nRegards,\nInternship Team"
    )
    return subject, body

def send_assignment_email(intern_id, task_id):
    with connect_db() as db:
        row = db.execute("""
            SELECT t.*, i.name AS intern_name, i.email AS intern_email
            FROM tasks t JOIN interns i ON i.id=t.intern_id
            WHERE t.id=? AND t.intern_id=?
        """, (task_id, intern_id)).fetchone()
    if not row:
        return False, "Task or intern not found."
    task = dict(row)
    subject, body = task_email(task["intern_name"], task)
    subject = "New internship task assigned: " + task["title"]
    body = (
        f"Hi {task['intern_name']},\n\nA new task has been assigned to you.\n\n"
        f"Task: {task['title']}\nDeadline: {task['due_date']} at {task['due_time']}\n\n"
        f"Instructions:\n{task['description']}\n\n"
        "Please submit your work before the deadline. If you have questions, contact your mentor.\n\n"
        "Regards,\nInternship Team"
    )
    ok, error = send_email(task["intern_email"], subject, body)
    if ok:
        with connect_db() as db:
            db.execute("UPDATE tasks SET assignment_email_sent=1 WHERE id=?", (task_id,))
    return ok, error

def check_due_reminders():
    """Send scheduled lead-time and due-day reminders; each is marked only after successful delivery."""
    if not EMAIL_ENABLED:
        return
    now = datetime.now()
    today = now.date()
    with connect_db() as db:
        rows = db.execute("""
            SELECT t.*, i.name AS intern_name, i.email AS intern_email
            FROM tasks t JOIN interns i ON i.id=t.intern_id
            WHERE t.status NOT IN ('Reviewed','Complete')
        """).fetchall()

    for row in rows:
        task = dict(row)
        try:
            due_dt = datetime.strptime(task["due_date"] + " " + task["due_time"], "%Y-%m-%d %H:%M")
        except ValueError:
            continue

        lead_date = due_dt.date() - timedelta(days=int(task["reminder_lead_days"]))
        # Send the lead-time reminder on its scheduled date, any time after midnight.
        if not task["lead_reminder_sent"] and today >= lead_date and today < due_dt.date():
            subject, body = task_email(task["intern_name"], task)
            ok, error = send_email(task["intern_email"], subject, body)
            if ok:
                with connect_db() as db:
                    db.execute("UPDATE tasks SET lead_reminder_sent=1 WHERE id=?", (task["id"],))
                app.logger.info("Lead reminder sent for task %s", task["id"])
            else:
                app.logger.warning("Lead reminder not sent for task %s: %s", task["id"], error)

        # Send the due-day reminder once on the deadline date, before the deadline time.
        if not task["due_reminder_sent"] and today == due_dt.date() and now <= due_dt:
            subject = f'Due today — {task["title"]}'
            body = (
                f"Hi {task['intern_name']},\n\nYour task \"{task['title']}\" is due today "
                f"at {task['due_time']}.\n\nInstructions:\n{task['description']}\n\n"
                "Please submit your work before the deadline.\n\nRegards,\nInternship Team"
            )
            ok, error = send_email(task["intern_email"], subject, body)
            if ok:
                with connect_db() as db:
                    db.execute("UPDATE tasks SET due_reminder_sent=1 WHERE id=?", (task["id"],))
                app.logger.info("Due-day reminder sent for task %s", task["id"])
            else:
                app.logger.warning("Due-day reminder not sent for task %s: %s", task["id"], error)

def reminder_worker():
    while True:
        try:
            check_due_reminders()
        except Exception:
            app.logger.exception("Reminder scheduler cycle failed")
        time.sleep(60)

def start_scheduler():
    global SCHEDULER_STARTED
    with SCHEDULER_LOCK:
        if SCHEDULER_STARTED:
            return
        thread = threading.Thread(target=reminder_worker, name="internpilot-reminders", daemon=True)
        thread.start()
        SCHEDULER_STARTED = True
        app.logger.info("Reminder scheduler started; email configured=%s", EMAIL_ENABLED)

@app.get("/")
def home():
    return render_template("index.html")

@app.get("/api/health")
def health():
    return jsonify({
        "status": "ok",
        "database": "SQLite",
        "database_file": DB_PATH.name,
        "email_configured": EMAIL_ENABLED,
        "scheduler": "running" if SCHEDULER_STARTED else "not started"
    })

@app.get("/api/interns")
def get_interns():
    with connect_db() as db:
        rows = db.execute("""
            SELECT i.id,i.name,i.email,
            COALESCE((SELECT t.status FROM tasks t WHERE t.intern_id=i.id ORDER BY t.id DESC LIMIT 1), i.status) AS status,
            COALESCE((SELECT t.title FROM tasks t WHERE t.intern_id=i.id ORDER BY t.id DESC LIMIT 1), i.task) AS task
            FROM interns i ORDER BY i.rowid
        """).fetchall()
    return jsonify([dict(r) for r in rows])

@app.get("/api/tasks")
def get_tasks():
    with connect_db() as db:
        rows = db.execute("SELECT * FROM tasks ORDER BY id").fetchall()
    return jsonify([row_to_task(r) for r in rows])

@app.post("/api/tasks")
def create_task():
    data = request.get_json(silent=True) or {}
    required = ["intern_id", "title", "description", "due_date", "due_time"]
    missing = [key for key in required if not str(data.get(key, "")).strip()]
    if missing:
        return jsonify({"error": "Missing required fields: " + ", ".join(missing)}), 400
    try:
        lead = int(data.get("reminder_lead_days", 2))
    except (TypeError, ValueError):
        return jsonify({"error": "reminder_lead_days must be 0, 1 or 2"}), 400
    if lead not in (0, 1, 2):
        return jsonify({"error": "reminder_lead_days must be 0, 1 or 2"}), 400
    try:
        datetime.strptime(data["due_date"], "%Y-%m-%d")
        datetime.strptime(data["due_time"], "%H:%M")
    except ValueError:
        return jsonify({"error": "Use a valid date (YYYY-MM-DD) and time (HH:MM)."}), 400

    with connect_db() as db:
        if not db.execute("SELECT id FROM interns WHERE id=?", (data["intern_id"],)).fetchone():
            return jsonify({"error": "Selected intern was not found."}), 404
        code = str(data.get("task_code") or "").strip() or f'TASK-{db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]+1:03d}'
        cur = db.execute("""
            INSERT INTO tasks(task_code,intern_id,title,description,due_date,due_time,language,
                              reminder_lead_days,status,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
        """, (
            code, data["intern_id"], data["title"].strip(), data["description"].strip(),
            data["due_date"], data["due_time"], data.get("language", "en-US"),
            lead, "Assigned", datetime.now(timezone.utc).isoformat()
        ))
        db.execute("UPDATE interns SET status='Awaiting work', task=? WHERE id=?",
                   (data["title"].strip(), data["intern_id"]))
        row = db.execute("SELECT * FROM tasks WHERE id=?", (cur.lastrowid,)).fetchone()

    task_id = row["id"]
    email_sent, email_error = send_assignment_email(data["intern_id"], task_id)
    message = "Task saved. Assignment email sent." if email_sent else "Task saved. Assignment email not sent."
    return jsonify({
        "message": message,
        "task": row_to_task(row),
        "assignment_email_sent": email_sent,
        "email_note": email_error if not email_sent else "Email delivered to configured recipient."
    }), 201

def build_rubric_review(requirements, submission):
    text = submission.lower()
    checks = [
        ("Use cases / examples", any(w in text for w in ["use case", "example", "first", "second", "third", "application", "can help"])),
        ("Risks / limitations", any(w in text for w in ["risk", "limitation", "privacy", "bias", "incorrect", "challenge", "concern"])),
        ("Recommendation / next step", any(w in text for w in ["recommend", "recommendation", "should", "suggest", "pilot", "conclusion", "next step"])),
        ("Clear structure", len(submission) > 180 and any(mark in submission for mark in ".!?")),
        ("Requirements coverage", any(w in text for w in requirements.lower().replace(",", " ").replace(".", " ").split() if len(w) > 5)),
    ]
    score = round(sum(ok for _, ok in checks) / len(checks) * 100)
    met = [name for name, ok in checks if ok]
    missing = [name for name, ok in checks if not ok]
    feedback = f"RUBRIC REVIEW · {score}/100 signals matched\n\nWhat is working:\n"
    feedback += "\n".join("✓ " + x + " detected" for x in met) if met else "No rubric signals detected yet."
    feedback += "\n\nSuggested corrections:\n"
    feedback += "\n".join(f"{i}. Add or strengthen: {name}. Make it explicit and support it with a specific example." for i, name in enumerate(missing, 1)) if missing else "1. Re-check every requirement and polish the final presentation.\n2. Verify facts and cite sources where appropriate."
    feedback += "\n\nImportant: this is a transparent keyword/rubric baseline, not a real AI model. Review the work yourself before making decisions."
    return score, feedback

@app.post("/api/reviews")
def create_review():
    data = request.get_json(silent=True) or {}
    intern_id = str(data.get("intern_id", "")).strip()
    requirements = str(data.get("requirements", "")).strip()
    submission = str(data.get("submission", "")).strip()
    if not intern_id or not requirements or not submission:
        return jsonify({"error": "intern_id, requirements and submission are required."}), 400
    score, feedback = build_rubric_review(requirements, submission)
    with connect_db() as db:
        if not db.execute("SELECT id FROM interns WHERE id=?", (intern_id,)).fetchone():
            return jsonify({"error": "Selected intern was not found."}), 404
        db.execute("""
            INSERT INTO reviews(intern_id,requirements,submission,score,feedback,created_at)
            VALUES(?,?,?,?,?,?)
        """, (intern_id, requirements, submission, score, feedback, datetime.now(timezone.utc).isoformat()))
        db.execute("UPDATE tasks SET status='Reviewed' WHERE id=(SELECT id FROM tasks WHERE intern_id=? ORDER BY id DESC LIMIT 1)", (intern_id,))
        db.execute("UPDATE interns SET status='Review due' WHERE id=?", (intern_id,))
    return jsonify({"score": score, "feedback": feedback, "saved": True}), 201

@app.get("/api/reviews")
def get_reviews():
    with connect_db() as db:
        rows = db.execute("SELECT id,intern_id,score,feedback,created_at FROM reviews ORDER BY id DESC").fetchall()
    return jsonify([dict(r) for r in rows])

@app.post("/api/email/test")
def test_email():
    data = request.get_json(silent=True) or {}
    to_email = str(data.get("to_email", "")).strip()
    if not to_email:
        return jsonify({"error": "Provide to_email in the JSON body."}), 400
    ok, error = send_email(
        to_email,
        "InternPilot AI — test email",
        "This is a test email from your InternPilot AI project. Email delivery is configured."
    )
    if not ok:
        return jsonify({"sent": False, "error": error}), 400
    return jsonify({"sent": True, "message": "Test email sent."})

   
if __name__ == "__main__":
    init_db()
    update_intern_email()
    start_scheduler()
    print(f"InternPilot AI database: {DB_PATH}")
    print(f"Email configured: {EMAIL_ENABLED}")
    print("Reminder scheduler checks deadlines every 60 seconds.")
    app.run(debug=False, use_reloader=False)

