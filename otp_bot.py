#!/usr/bin/env python3
"""
OTP / Sign-in Code Telegram Bot supporting IMAP and POP3
Admin + Approval + Persistent DB + Time-limited email access + Webhook
"""

import os
import re
import email
import time
import sqlite3
import shutil
import imaplib
import poplib
from email import parser
from dataclasses import dataclass
from typing import List, Optional, Tuple

import telebot
from telebot.types import ReplyKeyboardMarkup
from flask import Flask, request

# ================== CONFIG ==================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or 0)
DB_FILE = os.getenv("DB_FILE", "./otp_accounts.db")  # Change to /data/otp_accounts.db on Render
MAX_EMAILS_CHECK = int(os.getenv("MAX_EMAILS_CHECK", "20"))

if not BOT_TOKEN:
    raise SystemExit("Set BOT_TOKEN in environment")
if not ADMIN_ID:
    raise SystemExit("Set ADMIN_ID in environment")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")
app = Flask(__name__)

# ================== OTP PATTERNS ==================
OTP_PATTERNS = [
    re.compile(r"\b\d{6}\b"),
    re.compile(r"code[:\s]+(\d{6})", re.I),
    re.compile(r"otp[:\s]+(\d{6})", re.I),
]

# ================== DATA MODEL ==================
@dataclass
class Account:
    email: str
    password: str
    protocol: str
    server: str
    port: int

# ================== DATABASE ==================
def init_db():
    os.makedirs(os.path.dirname(DB_FILE), exist_ok=True)
    con = sqlite3.connect(DB_FILE)
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
            user_id INTEGER PRIMARY KEY
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS user_email_access (
            user_id INTEGER,
            email TEXT,
            expires_at INTEGER,
            PRIMARY KEY(user_id, email)
        )
    """)
    con.commit()
    con.close()

def upsert_account(acc: Account):
    con = sqlite3.connect(DB_FILE)
    cur = con.cursor()
    cur.execute("""
        INSERT INTO accounts(email,password,protocol,server,port)
        VALUES(?,?,?,?,?)
        ON CONFLICT(email) DO UPDATE SET
          password=excluded.password,
          protocol=excluded.protocol,
          server=excluded.server,
          port=excluded.port
    """, (acc.email, acc.password, acc.protocol, acc.server, acc.port))
    con.commit()
    con.close()

def get_account(email_addr: str) -> Optional[Account]:
    con = sqlite3.connect(DB_FILE)
    cur = con.cursor()
    cur.execute("SELECT email,password,protocol,server,port FROM accounts WHERE email=?", (email_addr,))
    row = cur.fetchone()
    con.close()
    if row:
        return Account(row[0], row[1], row[2], row[3], int(row[4]))
    return None

def list_accounts() -> List[Tuple[str,str,int]]:
    con = sqlite3.connect(DB_FILE)
    cur = con.cursor()
    cur.execute("SELECT email, server, port FROM accounts ORDER BY email")
    rows = cur.fetchall()
    con.close()
    return rows

# ================== APPROVAL & ACCESS ==================
def is_admin(uid: int) -> bool:
    return uid == ADMIN_ID

def is_approved(uid: int) -> bool:
    if is_admin(uid):
        return True
    con = sqlite3.connect(DB_FILE)
    cur = con.cursor()
    cur.execute("SELECT 1 FROM approved_users WHERE user_id=?", (uid,))
    ok = cur.fetchone() is not None
    con.close()
    return ok

def approve_user(uid: int):
    con = sqlite3.connect(DB_FILE)
    cur = con.cursor()
    cur.execute("INSERT OR IGNORE INTO approved_users(user_id) VALUES(?)", (uid,))
    con.commit()
    con.close()

def list_approved() -> List[int]:
    con = sqlite3.connect(DB_FILE)
    cur = con.cursor()
    cur.execute("SELECT user_id FROM approved_users")
    rows = [r[0] for r in cur.fetchall()]
    con.close()
    return rows

def grant_email_access(user_id: int, email_addr: str, days: int):
    expires_at = int(time.time()) + days * 86400
    con = sqlite3.connect(DB_FILE)
    cur = con.cursor()
    cur.execute("""
        INSERT OR REPLACE INTO user_email_access(user_id,email,expires_at)
        VALUES(?,?,?)
    """, (user_id, email_addr, expires_at))
    con.commit()
    con.close()

def check_email_access(user_id: int, email_addr: str) -> bool:
    if is_admin(user_id):
        return True
    now = int(time.time())
    con = sqlite3.connect(DB_FILE)
    cur = con.cursor()
    cur.execute("SELECT expires_at FROM user_email_access WHERE user_id=? AND email=?", (user_id, email_addr))
    row = cur.fetchone()
    con.close()
    return bool(row) and now < row[0]

def list_user_access():
    con = sqlite3.connect(DB_FILE)
    cur = con.cursor()
    cur.execute("SELECT user_id,email,expires_at FROM user_email_access")
    rows = cur.fetchall()
    con.close()
    return rows

# ================== OTP FETCH ==================
def fetch_signin_codes_imap(acc: Account):
    codes = []
    try:
        mail = imaplib.IMAP4_SSL(acc.server, acc.port)
        mail.login(acc.email, acc.password)
        mail.select("inbox")
        result, data = mail.search(None, "ALL")
        ids = data[0].split()[-MAX_EMAILS_CHECK:]
        for mail_id in reversed(ids):
            _, msg_data = mail.fetch(mail_id, "(RFC822)")
            raw = msg_data[0][1]
            msg = email.message_from_bytes(raw)
            subject = msg.get("Subject","")
            body = ""
            if msg.is_multipart():
                for part in msg.walk():
                    if part.get_content_type() == "text/plain":
                        body = part.get_payload(decode=True).decode(errors="ignore")
                        break
            else:
                body = msg.get_payload(decode=True).decode(errors="ignore")
            text = body + " " + subject
            for pat in OTP_PATTERNS:
                found = pat.findall(text)
                for f in found:
                    codes.append(f if isinstance(f,str) else f[0])
        mail.logout()
    except Exception as e:
        return [f"⚠️ Mail error: {e}"]
    return list(set(codes))[:5]

def fetch_signin_codes_pop3(acc: Account):
    codes = []
    try:
        mail = poplib.POP3_SSL(acc.server, acc.port)
        mail.user(acc.email)
        mail.pass_(acc.password)
        num_messages = len(mail.list()[1])
        start = max(0, num_messages - MAX_EMAILS_CHECK)
        for i in range(start, num_messages):
            raw_msg = b"\n".join(mail.retr(i+1)[1])
            msg = parser.Parser().parsestr(raw_msg.decode(errors="ignore"))
            subject = msg.get("Subject","")
            body = ""
            if msg.is_multipart():
                for part in msg.walk():
                    if part.get_content_type() == "text/plain":
                        body = part.get_payload(decode=True).decode(errors="ignore")
                        break
            else:
                body = msg.get_payload(decode=True).decode(errors="ignore")
            text = body + " " + subject
            for pat in OTP_PATTERNS:
                found = pat.findall(text)
                for f in found:
                    codes.append(f if isinstance(f,str) else f[0])
        mail.quit()
    except Exception as e:
        return [f"⚠️ Mail error: {e}"]
    return list(set(codes))[:5]

def fetch_signin_codes(acc: Account):
    if acc.protocol.lower() == "imap":
        return fetch_signin_codes_imap(acc)
    else:
        return fetch_signin_codes_pop3(acc)

# ================== BOT COMMANDS ==================
@bot.message_handler(commands=['start'])
def cmd_start(message):
    uid = message.from_user.id
    if not is_approved(uid):
        bot.reply_to(message, "❌ You are not approved.")
        return
    kb = ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("Exit")
    bot.reply_to(message, "✅ OTP Bot Ready\nUse /getcode email@example.com", reply_markup=kb)

# ---------------- ADMIN ----------------
@bot.message_handler(commands=['add'])
def cmd_add(message):
    if not is_admin(message.from_user.id):
        return
    try:
        _, email_addr, password, protocol, server, port = message.text.split()
        upsert_account(Account(email_addr,password,protocol,server,int(port)))
        bot.reply_to(message,"✅ Email account added.")
    except:
        bot.reply_to(message,"Usage:\n/add email pass imap|pop3 server port")

@bot.message_handler(commands=['list'])
def cmd_list(message):
    if not is_admin(message.from_user.id):
        return
    rows = list_accounts()
    if not rows:
        bot.reply_to(message,"Database empty.")
    else:
        bot.reply_to(message,"\n".join([f"{e} | {s}:{p}" for e,s,p in rows]))

@bot.message_handler(commands=['approve'])
def cmd_approve(message):
    if not is_admin(message.from_user.id):
        return
    try:
        _, uid = message.text.split()
        approve_user(int(uid))
        bot.reply_to(message,"✅ User approved.")
    except:
        bot.reply_to(message,"Usage: /approve 123456")

@bot.message_handler(commands=['approved'])
def cmd_approved(message):
    if not is_admin(message.from_user.id):
        return
    bot.reply_to(message,"\n".join(map(str,list_approved())))

@bot.message_handler(commands=['grant'])
def cmd_grant(message):
    if not is_admin(message.from_user.id):
        return
    try:
        _, uid, email_addr, days = message.text.split()
        grant_email_access(int(uid),email_addr,int(days))
        bot.reply_to(message,f"✅ Access granted:\nUser:{uid}\nEmail:{email_addr}\nDays:{days}")
    except:
        bot.reply_to(message,"Usage: /grant user_id email@example.com days")

@bot.message_handler(commands=['accesslist'])
def cmd_accesslist(message):
    if not is_admin(message.from_user.id):
        return
    rows = list_user_access()
    if not rows:
        bot.reply_to(message,"No active assignments.")
        return
    text=[]
    for uid,email_addr,exp in rows:
        text.append(f"{uid} | {email_addr} | expires: {time.ctime(exp)}")
    bot.reply_to(message,"\n".join(text))

@bot.message_handler(commands=['exportdb'])
def cmd_exportdb(message):
    if not is_admin(message.from_user.id):
        return
    try:
        backup_path = "/data/otp_backup.db"
        shutil.copy(DB_FILE,backup_path)
        with open(backup_path,"rb") as f:
            bot.send_document(message.chat.id,f,caption="✅ OTP Database Backup")
    except Exception as e:
        bot.reply_to(message,f"⚠️ Export failed: {e}")

# ---------------- USER ----------------
@bot.message_handler(commands=['getcode'])
def cmd_getcode(message):
    uid = message.from_user.id
    if not is_approved(uid):
        bot.reply_to(message,"❌ Not approved.")
        return
    try:
        _, email_addr = message.text.split()
        if not check_email_access(uid,email_addr):
            bot.reply_to(message,"⛔ You are not allowed to access this email.")
            return
        acc = get_account(email_addr)
        if not acc:
            bot.reply_to(message,"❌ Email not found.")
            return
        bot.reply_to(message,"⏳ Checking inbox...")
        codes = fetch_signin_codes(acc)
        if not codes:
            bot.reply_to(message,"❌ No OTP found.")
        else:
            bot.reply_to(message,"🔐 Latest OTP Codes:\n\n"+"\n".join(codes))
    except:
        bot.reply_to(message,"Usage:\n/getcode email@example.com")

# ================== WEBHOOK ==================
@app.route("/"+BOT_TOKEN,methods=['POST'])
def webhook_receive():
    json_str = request.stream.read().decode("utf-8")
    update = telebot.types.Update.de_json(json_str)
    bot.process_new_updates([update])
    return "OK",200

@app.route("/")
def webhook_set():
    render_url = os.getenv("RENDER_EXTERNAL_URL","http://localhost:5000")
    webhook_url = render_url.rstrip("/")+"/"+BOT_TOKEN
    bot.remove_webhook()
    bot.set_webhook(url=webhook_url)
    return "Webhook set",200

# ================== MAIN ==================
if __name__=="__main__":
    init_db()
    port = int(os.environ.get("PORT",5000))
    app.run(host="0.0.0.0",port=port)
