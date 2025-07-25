import os
import re
import logging
from datetime import datetime, timezone

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo

import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from flask import Flask, render_template, request, redirect, url_for, flash
from flask_sqlalchemy import SQLAlchemy
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv

load_dotenv()

DB_URL = os.getenv("DATABASE_URL", "sqlite:///reminders.db")
TZ = os.getenv("TZ", "Asia/Kolkata")
SCHEDULER_INTERVAL = int(os.getenv("SCHEDULER_INTERVAL", "60"))

EMAIL_USER = os.getenv("EMAIL_USER")
EMAIL_PASS = os.getenv("EMAIL_PASS")
EMAIL_FROM_NAME = os.getenv("EMAIL_FROM_NAME", "SmartReminder")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.config["SQLALCHEMY_DATABASE_URI"] = DB_URL
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "dev-secret")

db = SQLAlchemy(app)

class Reminder(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), nullable=False)
    subject = db.Column(db.String(255), nullable=False, default="Reminder")
    message = db.Column(db.Text, nullable=False)
    remind_at_utc = db.Column(db.DateTime(timezone=True), nullable=False)
    sent = db.Column(db.Boolean, default=False, nullable=False)
    created_at_utc = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    def remind_at_local(self):
        return self.remind_at_utc.astimezone(ZoneInfo("Asia/Kolkata"))

def valid_email(email: str) -> bool:
    return bool(re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email))

def send_email(to_email: str, subject: str, body: str) -> bool:
    if not (EMAIL_USER and EMAIL_PASS):
        app.logger.warning("EMAIL_USER / EMAIL_PASS not configured; cannot send email.")
        return False

    msg = MIMEMultipart()
    msg['From'] = f"{EMAIL_FROM_NAME} <{EMAIL_USER}>"
    msg['To'] = to_email
    msg['Subject'] = subject
    msg.attach(MIMEText(body, 'plain'))

    try:
        with smtplib.SMTP('smtp.gmail.com', 587) as server:
            server.starttls()
            server.login(EMAIL_USER, EMAIL_PASS)
            server.send_message(msg)
        return True
    except Exception as e:
        app.logger.error(f"Email send failed: {e}")
        return False

@app.context_processor
def inject_globals():
    return {"tz": TZ}

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
        except Exception as e:
            flash(f"Invalid date/time: {e}", "danger")
            return redirect(url_for("index"))

        if remind_at_utc <= datetime.now(timezone.utc):
            flash("Reminder time must be in the future", "warning")
            return redirect(url_for("index"))

        r = Reminder(
            email=email,
            subject=subject,
            message=message,
            remind_at_utc=remind_at_utc,
        )
        db.session.add(r)
        db.session.commit()
        flash("🎉 Reminder scheduled successfully!", "success")
        return redirect(url_for("index"))

    return render_template("index.html")

# Removed reminders() route for privacy
# @app.route("/reminders")
# def reminders():
#     items = Reminder.query.order_by(Reminder.remind_at_utc.asc()).all()
#     return render_template("reminders.html", reminders=items)

@app.route("/delete/<int:rid>", methods=["POST"])
def delete_reminder(rid):
    r = Reminder.query.get_or_404(rid)
    db.session.delete(r)
    db.session.commit()
    flash("Reminder deleted", "info")
    return redirect(url_for("index"))

def send_due_reminders():
    with app.app_context():
        logger.info("⏰ Checking for due reminders...")
        now_utc = datetime.now(timezone.utc)
        due = Reminder.query.filter_by(sent=False).filter(Reminder.remind_at_utc <= now_utc).all()
        for r in due:
            ok = send_email(r.email, r.subject, r.message)
            if ok:
                r.sent = True
                db.session.add(r)
                db.session.commit()
                app.logger.info(f"✅ Sent reminder {r.id} to {r.email}")
            else:
                app.logger.error(f"❌ Failed to send reminder {r.id} to {r.email}")

scheduler = BackgroundScheduler(timezone=TZ)
scheduler.add_job(send_due_reminders, "interval", seconds=SCHEDULER_INTERVAL, id="send_due_reminders")

def init_app():
    with app.app_context():
        db.create_all()
        if not scheduler.running:
            scheduler.start()
            logger.info(f"📅 Scheduler started: every {SCHEDULER_INTERVAL}s, TZ={TZ}")

init_app()

@app.teardown_appcontext
def shutdown_session(exception=None):
    db.session.remove()

if __name__ == "__main__":
    app.run(debug=True)
