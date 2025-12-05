import telebot
from flask import Flask, request
import psycopg2
import os
import re
from datetime import datetime

# ===========================
# CONFIG
# ===========================

BOT_TOKEN = os.getenv("BOT_TOKEN")
DB_URL = os.getenv("DB_URL")

# ✅ YOUR ADMIN TELEGRAM ID
ADMIN_ID = 123456789   # 🔴 REPLACE THIS WITH YOUR REAL TELEGRAM ID

bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)

# ===========================
# DATABASE CONNECTION
# ===========================

def get_db():
    return psycopg2.connect(DB_URL, sslmode="require")

def init_db():
    con = get_db()
    cur = con.cursor()

    # OTP storage
    cur.execute("""
        CREATE TABLE IF NOT EXISTS otps (
            id SERIAL PRIMARY KEY,
            email TEXT,
            otp TEXT,
            created_at TIMESTAMP
        )
    """)

    # Approved users
    cur.execute("""
        CREATE TABLE IF NOT EXISTS approved_users (
            chat_id BIGINT UNIQUE
        )
    """)

    # Emails waiting for approval
    cur.execute("""
        CREATE TABLE IF NOT EXISTS pending_emails (
            email TEXT UNIQUE
        )
    """)

    con.commit()
    cur.close()
    con.close()

# ===========================
# UTILITIES
# ===========================

def is_admin(chat_id):
    return chat_id == ADMIN_ID

def is_user_approved(chat_id):
    con = get_db()
    cur = con.cursor()
    cur.execute("SELECT 1 FROM approved_users WHERE chat_id=%s", (chat_id,))
    result = cur.fetchone()
    cur.close()
    con.close()
    return result is not None

def save_otp(email, otp):
    con = get_db()
    cur = con.cursor()
    cur.execute(
        "INSERT INTO otps (email, otp, created_at) VALUES (%s, %s, %s)",
        (email, otp, datetime.utcnow())
    )
    con.commit()
    cur.close()
    con.close()

def get_latest_otp(email):
    con = get_db()
    cur = con.cursor()
    cur.execute(
        "SELECT otp FROM otps WHERE email=%s ORDER BY created_at DESC LIMIT 1",
        (email,)
    )
    result = cur.fetchone()
    cur.close()
    con.close()
    return result[0] if result else None

# ===========================
# BOT COMMANDS
# ===========================

@bot.message_handler(commands=["start"])
def start(msg):
    if is_admin(msg.chat.id):
        bot.send_message(msg.chat.id, "✅ Admin access enabled.")
    elif is_user_approved(msg.chat.id):
        bot.send_message(msg.chat.id, "✅ You are approved. Use /get <email>")
    else:
        bot.send_message(msg.chat.id, "❌ You are not approved. Ask admin.")

# ---------------------------
# ADD EMAIL TO PENDING LIST
# ---------------------------

@bot.message_handler(commands=["addemail"])
def add_email(msg):
    try:
        email = msg.text.split(" ", 1)[1].strip()
    except:
        bot.send_message(msg.chat.id, "Usage: /addemail email@gmail.com")
        return

    con = get_db()
    cur = con.cursor()
    try:
        cur.execute("INSERT INTO pending_emails (email) VALUES (%s)", (email,))
        con.commit()
        bot.send_message(msg.chat.id, f"✅ Email added for approval:\n{email}")
    except:
        bot.send_message(msg.chat.id, "⚠️ Email already pending.")
    finally:
        cur.close()
        con.close()

# ---------------------------
# ADMIN: VIEW PENDING EMAILS
# ---------------------------

@bot.message_handler(commands=["pending"])
def pending(msg):
    if not is_admin(msg.chat.id):
        bot.send_message(msg.chat.id, "❌ Admin only.")
        return

    con = get_db()
    cur = con.cursor()
    cur.execute("SELECT email FROM pending_emails")
    rows = cur.fetchall()
    cur.close()
    con.close()

    if not rows:
        bot.send_message(msg.chat.id, "✅ No pending emails.")
        return

    text = "📩 Pending Emails:\n\n"
    for r in rows:
        text += f"• {r[0]}\n"

    bot.send_message(msg.chat.id, text)

# ---------------------------
# ADMIN: APPROVE USER
# ---------------------------

@bot.message_handler(commands=["approve"])
def approve(msg):
    if not is_admin(msg.chat.id):
        bot.send_message(msg.chat.id, "❌ Admin only.")
        return

    try:
        chat_id = int(msg.text.split(" ", 1)[1])
    except:
        bot.send_message(msg.chat.id, "Usage: /approve CHAT_ID")
        return

    con = get_db()
    cur = con.cursor()
    try:
        cur.execute("INSERT INTO approved_users (chat_id) VALUES (%s)", (chat_id,))
        con.commit()
        bot.send_message(msg.chat.id, f"✅ Approved: {chat_id}")
    except:
        bot.send_message(msg.chat.id, "⚠️ User already approved.")
    finally:
        cur.close()
        con.close()

# ---------------------------
# GET OTP (ADMIN BYPASS ✅)
# ---------------------------

@bot.message_handler(commands=["get"])
def get_otp(msg):
    chat_id = msg.chat.id

    if not is_admin(chat_id) and not is_user_approved(chat_id):
        bot.send_message(chat_id, "❌ You are not approved.")
        return

    try:
        email = msg.text.split(" ", 1)[1].strip()
    except:
        bot.send_message(chat_id, "Usage: /get email@gmail.com")
        return

    otp = get_latest_otp(email)

    if otp:
        bot.send_message(chat_id, f"✅ Latest OTP for {email}:\n\n🔐 {otp}")
    else:
        bot.send_message(chat_id, "❌ No OTP found.")

# ===========================
# EMAIL CAPTCHA / OTP PARSER (4 DIGITS NETFLIX)
# ===========================

def extract_netflix_otp(text):
    match = re.search(r"\b\d{4}\b", text)
    return match.group() if match else None

# ===========================
# WEBHOOK FIX ✅ (IMPORTANT)
# ===========================

@app.route("/" + BOT_TOKEN, methods=["POST"])
def webhook():
    try:
        json_str = request.stream.read().decode("utf-8")
        update = telebot.types.Update.de_json(json_str)
        bot.process_new_updates([update])
        return "OK", 200
    except Exception as e:
        print("Webhook error:", e)
        return "ERR", 500


@app.route("/")
def main():
    bot.remove_webhook()
    bot.set_webhook(url=os.getenv("RENDER_EXTERNAL_URL") + "/" + BOT_TOKEN)
    return "Webhook set", 200

# ===========================
# START
# ===========================

if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=10000)
