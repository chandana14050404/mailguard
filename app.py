#!/usr/bin/env python3
"""
MailGuard - Email Label Automation + WhatsApp Alerts
Uses ONLY Python built-in libraries + twilio (one small install)
Works on Python 3.8 to 3.14+
"""

import os, sys, json, imaplib, email, time, threading, logging, webbrowser
import urllib.request, urllib.parse, urllib.error, base64
from email.header import decode_header
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler

BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
LOG_FILE    = os.path.join(BASE_DIR, "mailguard.log")
PORT        = 5000

# ── CONFIG ───────────────────────────────────────────────────────────────────
def blank_cfg():
    return {
        "gmail_address":  "",
        "gmail_password": "",
        "twilio_sid":     "",
        "twilio_token":   "",
        "twilio_from":    "",
        "recipients":     [],
        "rules":          [],
        "check_interval": 300,
        "only_unread":    True,
        "mark_read":      False,
        "wa_template": (
            "🔔 *MailGuard Alert*\n\n"
            "📧 *From:* {from}\n"
            "📌 *Subject:* {subject}\n"
            "🏷️ *Label:* {label}\n"
            "⏰ *Time:* {time}\n\n"
            "📋 {preview}\n\n"
            "_MailGuard Automation_"
        ),
    }

CFG = blank_cfg()

def load_cfg():
    global CFG
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
            CFG.update(saved)
        except Exception as e:
            add_log("warn", f"Config load error: {e}")

def save_cfg():
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(CFG, f, indent=2)

# ── LOGGING ──────────────────────────────────────────────────────────────────
UI_LOGS = []

def add_log(level, msg):
    entry = {"time": datetime.now().strftime("%H:%M:%S"), "level": level, "msg": msg}
    UI_LOGS.append(entry)
    if len(UI_LOGS) > 800:
        UI_LOGS.pop(0)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"{ts} [{level.upper()}] {msg}\n"
    print(line, end="")
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line)
    except:
        pass

# ── ENGINE ────────────────────────────────────────────────────────────────────
ENG = {
    "running":    False,
    "last_check": None,
    "checked":    0,
    "labeled":    0,
    "wa_sent":    0,
    "errors":     0,
    "cooldowns":  {},
    "seen":       set(),
}

# ── WHATSAPP via Twilio REST API (no twilio package needed) ───────────────────
def send_whatsapp(to_number: str, body: str):
    """Calls Twilio REST API directly using urllib — no external packages."""
    sid   = CFG.get("twilio_sid",   "").strip()
    token = CFG.get("twilio_token", "").strip()
    from_ = CFG.get("twilio_from",  "").strip()

    if not sid:   return False, "Twilio Account SID is empty"
    if not token: return False, "Twilio Auth Token is empty"
    if not from_: return False, "Twilio From number is empty"

    if not from_.startswith("whatsapp:"):
        from_ = "whatsapp:" + from_
    if not to_number.startswith("whatsapp:"):
        to_number = "whatsapp:" + to_number

    url  = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
    data = urllib.parse.urlencode({
        "From": from_,
        "To":   to_number,
        "Body": body,
    }).encode("utf-8")

    credentials = base64.b64encode(f"{sid}:{token}".encode()).decode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Authorization", f"Basic {credentials}")
    req.add_header("Content-Type",  "application/x-www-form-urlencoded")

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read().decode())
            return True, result.get("sid", "sent")
    except urllib.error.HTTPError as e:
        body_err = e.read().decode(errors="replace")
        try:
            err_json = json.loads(body_err)
            msg = err_json.get("message", body_err)
        except:
            msg = body_err
        return False, f"Twilio error {e.code}: {msg}"
    except Exception as e:
        return False, str(e)

# ── EMAIL HELPERS ─────────────────────────────────────────────────────────────
def safe_decode(value):
    if not value: return ""
    try:
        parts = decode_header(str(value))
        out = []
        for raw, enc in parts:
            if isinstance(raw, bytes):
                out.append(raw.decode(enc or "utf-8", errors="replace"))
            else:
                out.append(str(raw))
        return " ".join(out)
    except:
        return str(value)

def extract_body(msg):
    body = ""
    try:
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/plain":
                    try:
                        body += part.get_payload(decode=True).decode(errors="replace")
                    except: pass
        else:
            body = msg.get_payload(decode=True).decode(errors="replace")
    except: pass
    return body[:4000]

def rule_matches(rule, sender, subject, body):
    if not rule.get("enabled", True): return False
    cs = rule.get("case_sensitive", False)
    def any_hit(text, kws):
        if not kws: return True
        h = text if cs else text.lower()
        for kw in kws:
            kw = kw.strip()
            if not kw: continue
            n = kw if cs else kw.lower()
            if n in h: return True
        return False
    return (any_hit(sender,  rule.get("from_keywords",    [])) and
            any_hit(subject, rule.get("subject_keywords", [])) and
            any_hit(body,    rule.get("body_keywords",    [])))

def fill_template(tmpl, sender, subject, label, preview, rule_name):
    return (tmpl
        .replace("{from}",      sender)
        .replace("{subject}",   subject)
        .replace("{label}",     label)
        .replace("{time}",      datetime.now().strftime("%H:%M:%S"))
        .replace("{preview}",   preview)
        .replace("{rule_name}", rule_name))

# ── CORE CHECK ────────────────────────────────────────────────────────────────
def do_check():
    addr = CFG.get("gmail_address",  "").strip()
    pwd  = CFG.get("gmail_password", "").strip()
    if not addr or not pwd:
        add_log("error", "Gmail credentials missing — go to Credentials tab")
        ENG["errors"] += 1
        return

    add_log("info", f"Connecting to Gmail ({addr})...")
    try:
        M = imaplib.IMAP4_SSL("imap.gmail.com", 993)
        M.login(addr, pwd)
        M.select("INBOX")
        add_log("success", "Connected to Gmail ✓")
    except imaplib.IMAP4.error as e:
        add_log("error", f"Gmail login failed: {e} — Use App Password not your Gmail password")
        ENG["errors"] += 1
        return
    except Exception as e:
        add_log("error", f"Gmail connection error: {e}")
        ENG["errors"] += 1
        return

    term = "UNSEEN" if CFG.get("only_unread", True) else "ALL"
    try:
        typ, data = M.uid("SEARCH", None, term)
    except Exception as e:
        add_log("error", f"Search error: {e}")
        M.logout(); return

    if typ != "OK" or not data[0]:
        add_log("info", "No new emails")
        ENG["last_check"] = datetime.now().strftime("%H:%M:%S")
        M.logout(); return

    uids  = data[0].split()[-50:]
    rules = CFG.get("rules", [])
    tmpl  = CFG.get("wa_template", "")
    add_log("info", f"Processing {len(uids)} email(s)...")

    for uid_b in uids:
        uid_s = uid_b.decode()
        if uid_s in ENG["seen"]: continue
        try:
            typ, raw_data = M.uid("FETCH", uid_b, "(RFC822)")
            if typ != "OK" or not raw_data or not raw_data[0]: continue

            msg     = email.message_from_bytes(raw_data[0][1])
            sender  = safe_decode(msg.get("From",    ""))
            subject = safe_decode(msg.get("Subject", "(no subject)"))
            body    = extract_body(msg)
            preview = " ".join(body.split())[:160]

            ENG["checked"] += 1
            add_log("info", f"  📧 {sender[:45]} | {subject[:50]}")

            for rule in rules:
                if not rule_matches(rule, sender, subject, body): continue
                label = rule.get("label", "").strip()
                add_log("success", f"  ✅ Rule '{rule['name']}' matched → [{label}]")

                if label:
                    try:
                        M.uid("COPY", uid_b, label)
                        ENG["labeled"] += 1
                        add_log("success", f"  🏷️ Label [{label}] applied")
                    except Exception as le:
                        add_log("warn", f"  ⚠️ Label '{label}' not found in Gmail — create it in Gmail first")

                if CFG.get("mark_read"):
                    try: M.uid("STORE", uid_b, "+FLAGS", "\\Seen")
                    except: pass

                if rule.get("send_wa", True):
                    recips = CFG.get("recipients", [])
                    if not recips:
                        add_log("warn", "  ⚠️ No recipients — add them in WhatsApp tab")
                    else:
                        rule_id  = rule.get("id", rule["name"])
                        cooldown = int(rule.get("cooldown_sec", 0))
                        if cooldown > 0:
                            last = ENG["cooldowns"].get(rule_id, 0)
                            if time.time() - last < cooldown:
                                add_log("info", "  ⏳ Cooldown active, skipping WA")
                                break

                        custom  = rule.get("wa_custom_msg", "").strip()
                        wa_msg  = fill_template(
                            custom if custom else tmpl,
                            sender, subject, label, preview, rule["name"]
                        )
                        for number in recips:
                            ok, info = send_whatsapp(number, wa_msg)
                            if ok:
                                add_log("wa", f"  📲 WhatsApp sent to {number} (SID: {info})")
                                ENG["wa_sent"] += 1
                            else:
                                add_log("error", f"  ❌ WhatsApp FAILED to {number}: {info}")
                                ENG["errors"] += 1
                        ENG["cooldowns"][rule_id] = time.time()
                break

        except Exception as e:
            add_log("error", f"Email error: {e}")
            ENG["errors"] += 1

        ENG["seen"].add(uid_s)
        if len(ENG["seen"]) > 5000:
            ENG["seen"] = set(list(ENG["seen"])[-3000:])

    ENG["last_check"] = datetime.now().strftime("%H:%M:%S")
    M.logout()
    add_log("info", "Check complete ✓")

def engine_loop():
    add_log("success", "▶ Engine started")
    while ENG["running"]:
        try: do_check()
        except Exception as e: add_log("error", f"Engine error: {e}")
        interval = int(CFG.get("check_interval", 300))
        for _ in range(interval * 10):
            if not ENG["running"]: break
            time.sleep(0.1)
    add_log("warn", "■ Engine stopped")

# ── HTTP SERVER ───────────────────────────────────────────────────────────────
HTML_FILE = os.path.join(BASE_DIR, "index.html")

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args): pass  # silence default access log

    def send_json(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type",   "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin",  "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            try:
                with open(HTML_FILE, "rb") as f:
                    body = f.read()
                self.send_response(200)
                self.send_header("Content-Type",   "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception as e:
                self.send_json({"error": str(e)}, 500)
        elif self.path == "/api/status":
            self.send_json({
                "running":    ENG["running"],
                "last_check": ENG["last_check"],
                "checked":    ENG["checked"],
                "labeled":    ENG["labeled"],
                "wa_sent":    ENG["wa_sent"],
                "errors":     ENG["errors"],
                "logs":       UI_LOGS[-150:],
            })
        elif self.path == "/api/config":
            safe = dict(CFG)
            if safe.get("gmail_password"): safe["gmail_password"] = "__SET__"
            if safe.get("twilio_token"):   safe["twilio_token"]   = "__SET__"
            self.send_json(safe)
        else:
            self.send_json({"error": "not found"}, 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw    = self.rfile.read(length) if length else b"{}"
        try:    body = json.loads(raw)
        except: body = {}

        if self.path == "/api/config":
            for k, v in body.items():
                if v == "__SET__": continue
                CFG[k] = v
            save_cfg()
            self.send_json({"ok": True})

        elif self.path == "/api/engine/start":
            if ENG["running"]:
                return self.send_json({"ok": False, "error": "Already running"})
            missing = []
            if not CFG.get("gmail_address"):  missing.append("Gmail address")
            if not CFG.get("gmail_password"): missing.append("Gmail App Password")
            if not CFG.get("twilio_sid"):     missing.append("Twilio SID")
            if not CFG.get("twilio_token"):   missing.append("Twilio Auth Token")
            if not CFG.get("twilio_from"):    missing.append("Twilio From number")
            if not CFG.get("recipients"):     missing.append("WhatsApp recipient")
            if missing:
                return self.send_json({"ok": False, "error": "Missing: " + ", ".join(missing)})
            ENG["running"] = True
            threading.Thread(target=engine_loop, daemon=True).start()
            self.send_json({"ok": True})

        elif self.path == "/api/engine/stop":
            ENG["running"] = False
            self.send_json({"ok": True})

        elif self.path == "/api/engine/check_now":
            threading.Thread(target=do_check, daemon=True).start()
            self.send_json({"ok": True})

        elif self.path == "/api/test/gmail":
            addr = CFG.get("gmail_address",  "").strip()
            pwd  = CFG.get("gmail_password", "").strip()
            if not addr or not pwd:
                return self.send_json({"ok": False, "error": "Save Gmail credentials first"})
            try:
                M = imaplib.IMAP4_SSL("imap.gmail.com", 993)
                M.login(addr, pwd)
                M.select("INBOX")
                _, data = M.uid("SEARCH", None, "UNSEEN")
                count = len(data[0].split()) if data[0] else 0
                M.logout()
                add_log("success", f"Gmail test OK — {count} unread email(s)")
                self.send_json({"ok": True, "unread": count})
            except imaplib.IMAP4.error as e:
                self.send_json({"ok": False, "error": f"Login failed: {e}. Use an App Password."})
            except Exception as e:
                self.send_json({"ok": False, "error": str(e)})

        elif self.path == "/api/test/whatsapp":
            to  = body.get("to",      "").strip()
            msg = body.get("message", "✅ MailGuard test — WhatsApp works!").strip()
            if not to:
                return self.send_json({"ok": False, "error": "No recipient selected"})
            ok, info = send_whatsapp(to, msg)
            if ok:
                add_log("wa", f"Test WhatsApp sent to {to} (SID: {info})")
                self.send_json({"ok": True, "sid": info})
            else:
                add_log("error", f"Test WhatsApp FAILED to {to}: {info}")
                self.send_json({"ok": False, "error": info})

        elif self.path == "/api/logs/clear":
            UI_LOGS.clear()
            self.send_json({"ok": True})

        else:
            self.send_json({"error": "not found"}, 404)

# ── MAIN ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    load_cfg()
    add_log("info", f"Python {sys.version}")
    add_log("info", "MailGuard starting...")

    server = HTTPServer(("0.0.0.0", PORT), Handler)

    print("\n" + "="*50)
    print("  MailGuard is running!")
    print(f"  Open this in your browser:")
    print(f"  -->  http://localhost:{PORT}  <--")
    print("  Keep this window OPEN.")
    print("="*50 + "\n")

    # Open browser automatically after 1.5 seconds
    def open_browser():
        time.sleep(1.5)
        webbrowser.open(f"http://localhost:{PORT}")
    threading.Thread(target=open_browser, daemon=True).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nMailGuard stopped.")
