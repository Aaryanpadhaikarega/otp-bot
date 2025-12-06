#!/usr/bin/env python3

import os
import re
import ssl
import imaplib
import poplib
import email
from email import policy
import psycopg2
from datetime import datetime, timedelta
from dataclasses import dataclass
from typing import List
import telebot
from flask import Flask, request

# ================= CONFIG =================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
DB_URL = os.getenv("DATABASE_URL", "").strip()

if not BOT_TOKEN:
    raise SystemExit("❌ BOT_TOKEN missing")
if not ADMIN_ID:
    raise SystemExit("❌ ADMIN_ID missing")
if not DB_URL:
    raise SystemExit("❌ DATABASE_URL missing")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")
app = Flask(__name__)

# ================= DATABASE =================

def get_db():
    return psycopg2.connect(DB_URL, sslmode="require")

def init_db():
    con = get_db()
    cur = con.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS accounts (
            email TEXT PRIMARY KEY,
            password TEXT NOT NULL,
            protocol TEXT NOT NULL,
            server TEXT NOT NULL,
            port INTEGER NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS approved_users (
            user_id BIGINT PRIMARY KEY
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS user_email_access (
            user_id BIGINT,
            email TEXT,
            expires_at TIMESTAMP,
            PRIMARY KEY (user_id, email)
        )
    """)
    con.commit()
    con.close()

# ================= MODELS =================

@dataclass
class Account:
    email: str
    password: str
    protocol: str
    server: str
    port: int

# ================= ADMIN HELPERS =================

def is_admin(uid: int) -> bool:
    return uid == ADMIN_ID

def is_approved(uid: int) -> bool:
    if is_admin(uid):
        return True
    con = get_db()
    cur = con.cursor()
    cur.execute("SELECT 1 FROM approved_users WHERE user_id=%s", (uid,))
    ok = cur.fetchone() is not None
    con.close()
    return ok

def has_email_access(uid: int, email_addr: str) -> bool:
    con = get_db()
    cur = con.cursor()
    cur.execute("""
        SELECT expires_at FROM user_email_access 
        WHERE user_id=%s AND email=%s
    """, (uid, email_addr))
    row = cur.fetchone()
    con.close()
    if not row:
        return False
    return datetime.utcnow() < row[0]

# ================= ✅ NETFLIX OTP DETECTOR (FINAL FIX) =================

def find_signin_code(text):
    text = re.sub(r"<.*?>", " ", text)   # strip html
    text = text.replace("&nbsp;", " ")
    text = re.sub(r"\s+", " ", text)

    # ✅ Convert spaced digits: "1 8 0 0" → "1800"
    text = re.sub(r"(\d)\s+(\d)\s+(\d)\s+(\d)", r"\1\2\3\4", text)

    patterns = [
        r"enter this code.*?(\d{4})",
        r"sign in.*?(\d{4})",
        r"netflix.*?(\d{4})",
        r"\b(\d{4})\b"
    ]

    for pat in patterns:
        match = re.search(pat, text, flags=re.IGNORECASE | re.DOTALL)
        if match:
            code = match.group(1)
            if not re.match(r"19\d\d|20\d\d", code):  # block years
                return code

    return None

# ================= EMAIL FETCH =================

def extract_email_text(msg):
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() in ("text/plain", "text/html"):
                payload = part.get_payload(decode=True)
                if payload:
                    body += payload.decode(errors="ignore") + "\n"
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            body = payload.decode(errors="ignore")
    return body

def fetch_signin_code(acc: Account) -> List[str]:
    try:
        if acc.protocol == "imap":
            imap = imaplib.IMAP4_SSL(acc.server, acc.port)
            imap.login(acc.email, acc.password)
            imap.select("inbox")

            _, data = imap.search(None, "ALL")
            email_ids = data[0].split()[-10:]

            for num in reversed(email_ids):
                _, msg_data = imap.fetch(num, "(RFC822)")
                msg = email.message_from_bytes(msg_data[0][1], policy=policy.default)

                text = extract_email_text(msg)
                code = find_signin_code(text)

                if code:
                    imap.logout()
                    return [code]

            imap.logout()

        else:  # ✅ POP3
            pop = poplib.POP3_SSL(acc.server, acc.port)
            pop.user(acc.email)
            pop.pass_(acc.password)

            count, _ = pop.stat()

            for i in range(count, max(1, count - 10), -1):
                _, lines, _ = pop.retr(i)
                text = b"\n".join(lines).decode(errors="ignore")

                code = find_signin_code(text)
                if code:
                    pop.quit()
                    return [code]

            pop.quit()

    except Exception as e:
        print("EMAIL ERROR:", e)

    return []

# ================= BOT COMMANDS =================

@bot.message_handler(commands=["start"])
def start_cmd(msg):
    if not is_approved(msg.from_user.id):
        bot.reply_to(msg, "❌ You are not approved.")
        return
    bot.reply_to(msg, "✅ Send:\n/get email@example.com")

@bot.message_handler(commands=["approve"])
def approve_cmd(msg):
    if not is_admin(msg.from_user.id):
        return
    try:
        uid = int(msg.text.split()[1])
        con = get_db()
        cur = con.cursor()
        cur.execute(
            "INSERT INTO approved_users(user_id) VALUES(%s) ON CONFLICT DO NOTHING",
            (uid,)
        )
        con.commit()
        con.close()
        bot.reply_to(msg, "✅ User approved.")
    except:
        bot.reply_to(msg, "Usage: /approve 123456")

@bot.message_handler(commands=["add"])
def add_account_cmd(msg):
    if not is_admin(msg.from_user.id):
        return
    try:
        _, email_addr, password, protocol, server, port = msg.text.split()
        con = get_db()
        cur = con.cursor()
        cur.execute("""
            INSERT INTO accounts(email,password,protocol,server,port)
            VALUES(%s,%s,%s,%s,%s)
            ON CONFLICT (email)
            DO UPDATE SET
                password=EXCLUDED.password,
                protocol=EXCLUDED.protocol,
                server=EXCLUDED.server,
                port=EXCLUDED.port
        """, (email_addr, password, protocol, server, int(port)))
        con.commit()
        con.close()
        bot.reply_to(msg, "✅ Account saved.")
    except:
        bot.reply_to(msg, "Usage:\n/add email pass imap mail.server.com 993")

@bot.message_handler(commands=["grant"])
def grant_cmd(msg):
    if not is_admin(msg.from_user.id):
        return
    try:
        _, uid, email_addr, days = msg.text.split()
        expires = datetime.utcnow() + timedelta(days=int(days))
        con = get_db()
        cur = con.cursor()
        cur.execute("""
            INSERT INTO user_email_access(user_id,email,expires_at)
            VALUES(%s,%s,%s)
            ON CONFLICT (user_id,email)
            DO UPDATE SET expires_at=EXCLUDED.expires_at
        """, (int(uid), email_addr, expires))
        con.commit()
        con.close()
        bot.reply_to(msg, f"✅ Access granted for {days} days.")
    except:
        bot.reply_to(msg, "Usage: /grant 123456 email@example.com 7")

@bot.message_handler(commands=["get"])
def get_cmd(msg):
    uid = msg.from_user.id

    if not is_approved(uid):
        bot.reply_to(msg, "❌ Not approved.")
        return

    try:
        email_addr = msg.text.split()[1]

        if not has_email_access(uid, email_addr):
            bot.reply_to(msg, "❌ No access to this email.")
            return

        con = get_db()
        cur = con.cursor()
        cur.execute("""
            SELECT email,password,protocol,server,port 
            FROM accounts WHERE email=%s
        """, (email_addr,))
        row = cur.fetchone()
        con.close()

        if not row:
            bot.reply_to(msg, "❌ Email not found.")
            return

        acc = Account(*row)
        codes = fetch_signin_code(acc)

        if not codes:
            bot.reply_to(msg, "⚠️ No OTP found.")
        else:
            bot.reply_to(msg, f"✅ Netflix OTP:\n<code>{codes[0]}</code>")

    except:
        bot.reply_to(msg, "Usage: /get email@example.com")

# ================= WEBHOOK =================

@app.route("/" + BOT_TOKEN, methods=["POST"])
def webhook_receive():
    json_str = request.stream.read().decode("utf-8")
    update = telebot.types.Update.de_json(json_str)
    bot.process_new_updates([update])
    return "OK", 200

@app.route("/")
def webhook_set():
    render_url = os.getenv("RENDER_EXTERNAL_URL")
    webhook_url = render_url.rstrip("/") + "/" + BOT_TOKEN
    bot.remove_webhook()
    bot.set_webhook(url=webhook_url)
    return "Webhook Set", 200

# ================= MAIN =================

if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
