import telebot
import psycopg2
import re
import os
import datetime
from flask import Flask
from threading import Thread

# ========================= CONFIG =========================

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID"))
DB_URL = os.getenv("DB_URL")

bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)

# ========================= DB CONNECTION =========================

def get_db():
    return psycopg2.connect(DB_URL, sslmode="require")

def init_db():
    con = get_db()
    cur = con.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS approved_users (
            user_id BIGINT PRIMARY KEY
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS granted_emails (
            user_id BIGINT,
            email TEXT,
            expiry TIMESTAMP
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS otps (
            email TEXT,
            otp TEXT,
            time TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    con.commit()
    con.close()

# ========================= FLASK KEEP ALIVE =========================

@app.route("/")
def home():
    return "Bot Running"

def run_flask():
    app.run(host="0.0.0.0", port=10000)

Thread(target=run_flask).start()

# ========================= HELPERS =========================

def is_admin(uid):
    return uid == ADMIN_ID

def is_approved(uid):
    con = get_db()
    cur = con.cursor()
    cur.execute("SELECT 1 FROM approved_users WHERE user_id=%s", (uid,))
    result = cur.fetchone()
    con.close()
    return result is not None

def has_email_access(uid, email):
    con = get_db()
    cur = con.cursor()
    cur.execute("""
        SELECT expiry FROM granted_emails 
        WHERE user_id=%s AND email=%s
    """, (uid, email))
    row = cur.fetchone()
    con.close()
    if not row:
        return False
    return datetime.datetime.now() < row[0]

# ========================= ADMIN COMMANDS =========================

@bot.message_handler(commands=["approve"])
def approve_user(msg):
    if not is_admin(msg.from_user.id):
        return

    try:
        uid = int(msg.text.split()[1])
        con = get_db()
        cur = con.cursor()
        cur.execute("INSERT INTO approved_users VALUES (%s) ON CONFLICT DO NOTHING", (uid,))
        con.commit()
        con.close()
        bot.reply_to(msg, "✅ User approved.")
    except:
        bot.reply_to(msg, "❌ Usage: /approve USER_ID")

@bot.message_handler(commands=["disapprove"])
def disapprove_user(msg):
    if not is_admin(msg.from_user.id):
        return

    try:
        uid = int(msg.text.split()[1])
        con = get_db()
        cur = con.cursor()
        cur.execute("DELETE FROM approved_users WHERE user_id=%s", (uid,))
        con.commit()
        con.close()
        bot.reply_to(msg, "✅ User removed.")
    except:
        bot.reply_to(msg, "❌ Usage: /disapprove USER_ID")

@bot.message_handler(commands=["grant"])
def grant_email(msg):
    if not is_admin(msg.from_user.id):
        return

    try:
        parts = msg.text.split()
        uid = int(parts[1])
        email = parts[2]
        days = int(parts[3])

        expiry = datetime.datetime.now() + datetime.timedelta(days=days)

        con = get_db()
        cur = con.cursor()
        cur.execute("""
            INSERT INTO granted_emails VALUES (%s,%s,%s)
        """, (uid, email, expiry))
        con.commit()
        con.close()

        bot.reply_to(msg, f"✅ {email} granted to {uid} for {days} days.")
    except:
        bot.reply_to(msg, "❌ Usage: /grant USER_ID email@gmail.com days")

@bot.message_handler(commands=["revoke"])
def revoke_email(msg):
    if not is_admin(msg.from_user.id):
        return

    try:
        parts = msg.text.split()
        uid = int(parts[1])
        email = parts[2]

        con = get_db()
        cur = con.cursor()
        cur.execute("""
            DELETE FROM granted_emails WHERE user_id=%s AND email=%s
        """, (uid, email))
        con.commit()
        con.close()

        bot.reply_to(msg, "✅ Email revoked.")
    except:
        bot.reply_to(msg, "❌ Usage: /revoke USER_ID email@gmail.com")

# ========================= OTP FETCH =========================

@bot.message_handler(commands=["get"])
def get_otp(msg):
    uid = msg.from_user.id
    email = msg.text.replace("/get", "").strip()

    if not email:
        bot.reply_to(msg, "❌ Usage: /get email@gmail.com")
        return

    # ✅ ADMIN BYPASS — FULL ACCESS
    if not is_admin(uid):
        if not is_approved(uid):
            bot.reply_to(msg, "⛔ You are not approved.")
            return

        if not has_email_access(uid, email):
            bot.reply_to(msg, "⛔ You don't have access to this email.")
            return

    con = get_db()
    cur = con.cursor()
    cur.execute("""
        SELECT otp, time FROM otps 
        WHERE email=%s 
        ORDER BY time DESC LIMIT 1
    """, (email,))
    row = cur.fetchone()
    con.close()

    if not row:
        bot.reply_to(msg, "❌ No OTP found.")
        return

    otp, time = row
    bot.reply_to(msg, f"✅ Netflix OTP: {otp}\n⏰ {time}")

# ========================= OTP INGESTION (NETFLIX 4 DIGIT ONLY) =========================

def save_otp_from_email(email_body, to_email):
    match = re.search(r"\b\d{4}\b", email_body)
    if not match:
        return

    otp = match.group()

    con = get_db()
    cur = con.cursor()
    cur.execute("""
        INSERT INTO otps VALUES (%s,%s,NOW())
    """, (to_email, otp))
    con.commit()
    con.close()

# ========================= START =========================

init_db()
bot.polling()
