#!/usr/bin/env python3

import os
import imaplib
import poplib
import email
import telebot
from flask import Flask, request

# ================= CONFIG =================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

if not BOT_TOKEN:
    raise SystemExit("❌ BOT_TOKEN missing")
if not ADMIN_ID:
    raise SystemExit("❌ ADMIN_ID missing")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")
app = Flask(__name__)

# ================= ADMIN CHECK =================

def is_admin(uid: int) -> bool:
    return uid == ADMIN_ID

# ================= FULL EMAIL FETCH =================

def fetch_full_mail(email_addr, password, protocol, server, port):
    debug = []

    try:
        debug.append("✅ Starting connection...")

        # ================= IMAP =================
        if protocol.lower() == "imap":
            debug.append(f"➡️ IMAP: {server}:{port}")

            imap = imaplib.IMAP4_SSL(server, int(port))
            imap.login(email_addr, password)
            debug.append("✅ IMAP login OK")

            status, _ = imap.select("INBOX")
            debug.append(f"📂 Inbox status: {status}")

            status, data = imap.search(None, "ALL")
            if status != "OK":
                imap.logout()
                return debug + ["❌ IMAP SEARCH FAILED"]

            ids = data[0].split()
            debug.append(f"📧 Total mails: {len(ids)}")

            if not ids:
                imap.logout()
                return debug + ["⚠️ Inbox is EMPTY"]

            last_ids = ids[-3:]
            messages = []

            for eid in last_ids:
                status, msg_data = imap.fetch(eid, "(RFC822)")
                if status != "OK":
                    messages.append("❌ FETCH FAILED")
                    continue

                raw = msg_data[0][1]
                msg = email.message_from_bytes(raw)

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
            return debug + messages

        # ================= POP3 =================
        else:
            debug.append(f"➡️ POP3: {server}:{port}")

            pop = poplib.POP3_SSL(server, int(port))
            pop.user(email_addr)
            pop.pass_(password)

            count, _ = pop.stat()
            debug.append(f"📧 Total mails: {count}")

            if count == 0:
                pop.quit()
                return debug + ["⚠️ Inbox EMPTY"]

            messages = []
            start = max(1, count - 2)

            for i in range(count, start - 1, -1):
                _, lines, _ = pop.retr(i)
                text = b"\n".join(lines).decode(errors="ignore")
                messages.append(text)

            pop.quit()
            return debug + messages

    except Exception as e:
        return debug + [f"🔥 EMAIL CRASH: {str(e)}"]

# ================= BOT COMMAND =================

@bot.message_handler(commands=["start"])
def start_cmd(msg):
    bot.reply_to(
        msg,
        "✅ Send like this:\n"
        "/get email@gmail.com password imap imap.gmail.com 993\n\n"
        "or\n"
        "/get email@gmail.com password pop3 pop.gmail.com 995"
    )

@bot.message_handler(commands=["get"])
def get_cmd(msg):
    if not is_admin(msg.from_user.id):
        bot.reply_to(msg, "❌ Admin only.")
        return

    try:
        _, email_addr, password, protocol, server, port = msg.text.split()

        results = fetch_full_mail(email_addr, password, protocol, server, port)

        if not results:
            bot.reply_to(msg, "⚠️ No response from mailbox.")
            return

        for chunk in results:
            for i in range(0, len(chunk), 3800):
                bot.send_message(msg.chat.id, chunk[i:i+3800])

    except Exception as e:
        bot.reply_to(msg, f"🔥 BOT ERROR:\n{str(e)}")

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
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
