import telebot
import psycopg2
import re
import os
from datetime import datetime

# ==========================
# CONFIG
# ==========================

BOT_TOKEN = os.getenv("BOT_TOKEN")
DB_URL = os.getenv("DATABASE_URL")
ADMIN_ID = int(os.getenv("ADMIN_ID"))

bot = telebot.TeleBot(BOT_TOKEN)

# ✅ ONLY 4 DIGIT NETFLIX OTP
NETFLIX_OTP_PATTERN = re.compile(r"\b\d{4}\b")

# ==========================
# DATABASE
# ==========================

def get_db():
    return psycopg2.connect(DB_URL, sslmode="require")

def init_db():
    con = get_db()
    cur = con.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS otps (
            id SERIAL PRIMARY KEY,
            otp VARCHAR(10),
            time TIMESTAMP
        )
    """)
    con.commit()
    cur.close()
    con.close()

# ==========================
# SAVE OTP
# ==========================

def save_otp(otp):
    con = get_db()
    cur = con.cursor()
    cur.execute(
        "INSERT INTO otps (otp, time) VALUES (%s, %s)",
        (otp, datetime.now())
    )
    con.commit()
    cur.close()
    con.close()

# ==========================
# GET LAST OTP
# ==========================

def get_last_otp():
    con = get_db()
    cur = con.cursor()
    cur.execute("SELECT otp, time FROM otps ORDER BY id DESC LIMIT 1")
    row = cur.fetchone()
    cur.close()
    con.close()
    return row

# ==========================
# BOT COMMANDS
# ==========================

@bot.message_handler(commands=["start"])
def start(msg):
    bot.reply_to(msg, "✅ Netflix OTP Bot is LIVE.\nSend OTP to store.")

@bot.message_handler(commands=["lastotp"])
def last_otp(msg):
    if msg.from_user.id != ADMIN_ID:
        bot.reply_to(msg, "❌ Access Denied.")
        return

    data = get_last_otp()
    if not data:
        bot.reply_to(msg, "No OTP saved yet.")
    else:
        otp, time = data
        bot.reply_to(msg, f"🎯 Last Netflix OTP: {otp}\n🕒 Time: {time}")

# ==========================
# OTP LISTENER
# ==========================

@bot.message_handler(func=lambda message: True)
def otp_listener(message):
    text = message.text

    match = NETFLIX_OTP_PATTERN.search(text)
    if match:
        otp = match.group()
        save_otp(otp)

        bot.send_message(
            ADMIN_ID,
            f"✅ New Netflix OTP Received:\n🔐 OTP: {otp}"
        )

# ==========================
# START BOT
# ==========================

if __name__ == "__main__":
    init_db()
    print("✅ Netflix 4-Digit OTP Bot Running...")
    bot.infinity_polling()
