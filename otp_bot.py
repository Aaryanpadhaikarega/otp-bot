#!/usr/bin/env python3

import os
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

if not BOT_TOKEN or not ADMIN_ID or not DB_URL:
    raise SystemExit("❌ Missing env variables")

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

# ================= ACCESS =================

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

# ================= EMAIL FETCH =================

def fetch_full_mail(acc: Account) -> List[str]:
    messages = []

    try:
        # ---------- IMAP ----------
        if acc.protocol == "imap":
            imap = imaplib.IMAP4_SSL(acc.server, acc.port)
            imap.login(acc.email, acc.password)
            imap.select("inbox")

            _, data = imap.search(None, "ALL")
            ids = data[0].split()[-3:]  # last 3 emails only

            for eid in ids:
                _, msg_data = imap.fetch(eid, "(RFC822)")
                raw = msg_data[0][1]

                msg = email.message_from_bytes(raw, policy=policy.default)

                subject = str(msg["subject"])
                messages.append(f"\n===== SUBJECT =====\n{subject}\n")

                full_text = ""

                if msg.is_multipart():
                    for part in msg.walk():
                        if part.get_content_type() in ("text/plain", "text/html"):
                            payload = part.get_payload(decode=True)
                            if payload:
                                full_text += payload.decode(errors="ignore") + "\n"
                else:
                    payload = msg.get_payload(decode=True)
                    if payload:
                        full_text = payload.decode(errors="ignore")

                messages.append(full_text)

            imap.logout()

        # ---------- POP3 ----------
        else:
            pop = poplib.POP3_SSL(acc.server, acc.port)
            pop.user(acc.email)
            pop.pass_(acc.password)

            count, _ = pop.stat()
            start = max(1, count - 2)

            for i in range(count, start - 1, -1):
                _, lines, _ = pop.retr(i)
                text = b"\n".join(lines).decode(errors="ignore")
                messages.append(text)

            pop.quit()

    except Exception as e:
        return [f"EMAIL ERROR: {e}"]

    return messages

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
    uid = int(msg.text.split()[1])
    con = get_db()
    cur = con.cursor()
    cur.execute("INSERT INTO approved_users(user_id) VALUES(%s) ON CONFLICT DO NOTHING", (uid,))
    con.commit()
    con.close()
    bot.reply_to(msg, "✅ User approved.")

@bot.message_handler(commands=["add"])
def add_account_cmd(msg):
    if not is_admin(msg.from_user.id):
        return
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

@bot.message_handler(commands=["grant"])
def grant_cmd(msg):
    if not is_admin(msg.from_user.id):
        return
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
    bot.reply_to(msg, "✅ Access granted.")

@bot.message_handler(commands=["get"])
def get_cmd(msg):
    uid = msg.from_user.id

    if not is_approved(uid):
        bot.reply_to(msg, "❌ Not approved.")
        return

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
    mails = fetch_full_mail(acc)

    if not mails:
        bot.reply_to(msg, "⚠️ No mails found.")
        return

    for mail in mails:
        for i in range(0, len(mail), 3800):
            bot.send_message(msg.chat.id, mail[i:i+3800])

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
