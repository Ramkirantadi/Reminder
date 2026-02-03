import os
import re
import logging
from datetime import datetime, timezone

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo

import requests
from flask import Flask, render_template, request, redirect, url_for, flash
from flask_sqlalchemy import SQLAlchemy
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv

# -------------------- LOAD ENV --------------------
load_dotenv()

DB_URL = os.getenv("DATABASE_URL", "sqlite:///reminders.db")
TZ = os.getenv("TZ", "Asia/Kolkata")
SCHEDULER_INTERVAL = int(os.getenv("SCHEDULER_INTERVAL", "60"))

SENDGRID_API_KEY = os.getenv("SENDGRID_API_KEY")
EMAIL_FROM = os.getenv("EMAIL_FROM")
EMAIL_FROM_NAME = os.getenv("EMAIL_FROM_NAME", "SmartReminder")

# -------------------- LOGGING --------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# -------------------- FLASK APP --------------------
app = Flask(__name__)
app.config["SQLALCHEMY_DATABASE_URI"] = DB_URL
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "dev-secret")

db = SQLAlchemy(app)

# -------------------- MODEL --------------------
class Reminder(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), nullable=False)
    subject = db.Column(db.String(255), nullable=False, default="Reminder")
    message = db.Column(db.Text, nullable=False)
    remind_at_utc = db.Column(db.DateTime(timezone=True), nullable=False)
    sent = db.Column(db.Boolean, default=False, nullable=False)
    created_at_utc = db.Column(
        db.DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc)
    )

# -------------------- UTILS --------------------
def valid_email(email: str) -> bool:
    return bool(re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email))

def send_email(to_email: str, subject: str, body: str) -> bool:
    if not SENDGRID_API_KEY or not EMAIL_FROM:
        logger.error("SendGrid environment variables not set")
        return False

    url = "https://api.sendgrid.com/v3/mail/send"
    headers = {
        "Authorization": f"Bearer {SENDGRID_API_KEY}",
        "Content-Type": "application/json"
    }

    payload = {
        "personalizations": [{
            "to": [{"email": to_email}],
            "subject": subject
        }],
        "from": {
            "email": EMAIL_FROM,
            "name": EMAIL_FROM_NAME
        },
        "content": [{
            "type": "text/plain",
            "value": body
        }]
    }

    try:
        response = requests.post(url, headers=headers, json=payload, timeout=15)
        if response.status_code in (200, 202):
            return True
        else:
            logger.error(f"SendGrid error {response.status_code}: {response.text}")
            return False
    except Exception as e:
        logger.error(f"Email send failed: {e}")
        return False

# -------------------- ROUTES --------------------
@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        email = request.form.get("email")
        subject = request.form.get("subject") or "Reminder"
        message = request.form.get("message")
        remind_at_raw = request.form.get("remind_at")

        if not (email and message and remind_at_raw):
            flash("All fields are required", "danger")
            return redirect(url_for("index"))

        if not valid_email(email):
            flash("Invalid email format", "danger")
            return redirect(url_for("index"))

        try:
            local_dt = datetime.fromisoformat(remind_at_raw)
            local_dt = local_dt.replace(tzinfo=ZoneInfo(TZ))
            remind_at_utc = local_dt.astimezone(timezone.utc)
        except Exception:
            flash("Invalid date/time", "danger")
            return redirect(url_for("index"))

        if remind_at_utc <= datetime.now(timezone.utc):
            flash("Reminder time must be in the future", "warning")
            return redirect(url_for("index"))

        reminder = Reminder(
            email=email,
            subject=subject,
            message=message,
            remind_at_utc=remind_at_utc
        )
        db.session.add(reminder)
        db.session.commit()

        flash("🎉 Reminder scheduled successfully!", "success")
        return redirect(url_for("index"))

    return render_template("index.html")

# -------------------- SCHEDULER JOB --------------------
def send_due_reminders():
    try:
        with app.app_context():
            now_utc = datetime.now(timezone.utc)
            due = Reminder.query.filter_by(sent=False)\
                .filter(Reminder.remind_at_utc <= now_utc).all()

            for r in due:
                if send_email(r.email, r.subject, r.message):
                    r.sent = True
                    db.session.commit()
                    logger.info(f"✅ Sent reminder {r.id} to {r.email}")
                else:
                    logger.error(f"❌ Failed to send reminder {r.id} to {r.email}")
    except Exception as e:
        logger.error(f"Reminder job crashed: {e}")

scheduler = BackgroundScheduler(timezone=TZ)
scheduler.add_job(
    send_due_reminders,
    "interval",
    seconds=SCHEDULER_INTERVAL,
    id="send_due_reminders",
    max_instances=3,
    coalesce=True
)

def init_app():
    with app.app_context():
        db.create_all()
        if not scheduler.running:
            scheduler.start()
            logger.info("📅 Scheduler started")

init_app()

# -------------------- MAIN --------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
