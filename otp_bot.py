import os
import re
import psycopg2
import imaplib
import poplib
import email
from datetime import datetime, timedelta
import telebot
from flask import Flask, request

# ================= CONFIG =================
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
DB_URL = os.getenv("DATABASE_URL")

bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)

# ================ DB ====================
def get_db():
    return psycopg2.connect(DB_URL, sslmode="require")

def init_db():
    con = get_db()
    cur = con.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS accounts (
        email TEXT PRIMARY KEY,
        password TEXT,
        protocol TEXT,
        server TEXT,
        port INTEGER
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS approved_users (
        user_id BIGINT PRIMARY KEY
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS grants (
        user_id BIGINT,
        email TEXT,
        expires_at TIMESTAMP
    )
    """)

    con.commit()
    con.close()

# ============ HELPERS =================
def is_admin(uid): return uid == ADMIN_ID

def is_approved(uid):
    if is_admin(uid):
        return True
    con = get_db()
    cur = con.cursor()
    cur.execute("SELECT 1 FROM approved_users WHERE user_id=%s", (uid,))
    ok = cur.fetchone()
    con.close()
    return ok is not None

def has_access(uid, email_addr):
    if is_admin(uid):
        return True

    con = get_db()
    cur = con.cursor()
    cur.execute("""
    SELECT 1 FROM grants 
    WHERE user_id=%s AND email=%s AND expires_at > NOW()
    """, (uid, email_addr))
    ok = cur.fetchone()
    con.close()
    return ok is not None

# ============ ADMIN COMMANDS =================

@bot.message_handler(commands=["approve"])
def approve_cmd(msg):
    if not is_admin(msg.from_user.id):
        return
    uid = int(msg.text.split()[1])
    con = get_db()
    cur = con.cursor()
    cur.execute("INSERT INTO approved_users(user_id) VALUES(%s) ON CONFLICT DO NOTHING", (uid,))
    con.commit()
    con.close()
    bot.reply_to(msg, "✅ User approved")

@bot.message_handler(commands=["disapprove"])
def disapprove_cmd(msg):
    if not is_admin(msg.from_user.id):
        return
    uid = int(msg.text.split()[1])
    con = get_db()
    cur = con.cursor()
    cur.execute("DELETE FROM approved_users WHERE user_id=%s", (uid,))
    con.commit()
    con.close()
    bot.reply_to(msg, "✅ User removed")

@bot.message_handler(commands=["addemail"])
def addemail_cmd(msg):
    if not is_admin(msg.from_user.id):
        return
    _, email_addr, password, protocol, server, port = msg.text.split()
    con = get_db()
    cur = con.cursor()
    cur.execute("""
    INSERT INTO accounts VALUES(%s,%s,%s,%s,%s)
    ON CONFLICT(email) DO UPDATE SET
    password=excluded.password,
    protocol=excluded.protocol,
    server=excluded.server,
    port=excluded.port
    """, (email_addr, password, protocol, server, int(port)))
    con.commit()
    con.close()
    bot.reply_to(msg, "✅ Email inbox saved")

@bot.message_handler(commands=["grant"])
def grant_cmd(msg):
    if not is_admin(msg.from_user.id):
        return
    _, uid, email_addr, days = msg.text.split()
    expires = datetime.now() + timedelta(days=int(days))

    con = get_db()
    cur = con.cursor()
    cur.execute("INSERT INTO grants VALUES(%s,%s,%s)", (int(uid), email_addr, expires))
    con.commit()
    con.close()
    bot.reply_to(msg, "✅ Access granted")

@bot.message_handler(commands=["revoke"])
def revoke_cmd(msg):
    if not is_admin(msg.from_user.id):
        return
    _, uid, email_addr = msg.text.split()
    con = get_db()
    cur = con.cursor()
    cur.execute("DELETE FROM grants WHERE user_id=%s AND email=%s", (int(uid), email_addr))
    con.commit()
    con.close()
    bot.reply_to(msg, "✅ Access revoked")

# ============ OTP FETCH ==================

def fetch_otp(acc):
    if acc[2] == "imap":
        mail = imaplib.IMAP4_SSL(acc[3], acc[4])
        mail.login(acc[0], acc[1])
        mail.select("inbox")
        _, data = mail.search(None, "ALL")
        ids = data[0].split()[-5:]
        for i in reversed(ids):
            _, msg_data = mail.fetch(i, "(RFC822)")
            msg = email.message_from_bytes(msg_data[0][1])
            body = ""
            if msg.is_multipart():
                for p in msg.walk():
                    if p.get_content_type() == "text/plain":
                        body = p.get_payload(decode=True).decode()
            else:
                body = msg.get_payload(decode=True).decode()

            m = re.search(r"\b\d{4}\b", body)
            if m:
                return m.group()
        return None

    else:
        pop = poplib.POP3_SSL(acc[3], acc[4])
        pop.user(acc[0])
        pop.pass_(acc[1])
        count = len(pop.list()[1])

        for i in range(count, 0, -1):
            msg = b"\n".join(pop.retr(i)[1])
            msg = email.message_from_bytes(msg)
            body = msg.get_payload(decode=True).decode()
            m = re.search(r"\b\d{4}\b", body)
            if m:
                return m.group()
        return None

# ============ USER COMMAND =================

@bot.message_handler(commands=["get"])
def get_cmd(msg):
    uid = msg.from_user.id
    email_addr = msg.text.split()[1]

    if not is_approved(uid):
        bot.reply_to(msg, "❌ Not approved")
        return

    if not has_access(uid, email_addr):
        bot.reply_to(msg, "❌ No access to this email")
        return

    con = get_db()
    cur = con.cursor()
    cur.execute("SELECT * FROM accounts WHERE email=%s", (email_addr,))
    acc = cur.fetchone()
    con.close()

    otp = fetch_otp(acc)
    bot.reply_to(msg, f"✅ Netflix OTP: {otp}")

# ============ WEBHOOK =================

@app.route("/" + BOT_TOKEN, methods=["POST"])
def webhook():
    update = telebot.types.Update.de_json(request.stream.read())
    bot.process_new_updates([update])
    return "OK", 200

@app.route("/")
def main():
    bot.remove_webhook()
    bot.set_webhook(url=os.getenv("RENDER_EXTERNAL_URL") + "/" + BOT_TOKEN)
    return "OK"

# ============ START =================
if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
