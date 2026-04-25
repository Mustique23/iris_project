#!/usr/bin/env python3
"""
web_app.py  –  IRIS Caregiver Web Dashboard
────────────────────────────────────────────
Flask server that:
  • Reads JSON events from ESP32 on /dev/cu.usbserial-0001 (115200 baud)
  • Logs every event to history.csv
  • Streams live updates to the browser via Server-Sent Events (SSE)
  • Sends Telegram alerts for WATER and EMERGENCY events
  • Serves a modern dashboard at http://localhost:5000

Requirements:
    pip install flask pyserial requests

Usage:
    python3 web_app.py
"""

import csv
import json
import os
import queue
import subprocess
import sys
import threading
import time
from collections import defaultdict
from datetime import datetime

try:
    from flask import Flask, Response, render_template, stream_with_context
except ImportError:
    print("[ERROR] Flask not installed. Run: pip install flask")
    sys.exit(1)

try:
    import serial
except ImportError:
    print("[ERROR] pyserial not installed. Run: pip install pyserial")
    sys.exit(1)

try:
    import requests as req
except ImportError:
    print("[ERROR] requests not installed. Run: pip install requests")
    sys.exit(1)

# ─── Configuration ────────────────────────────────────────────────────────────

SERIAL_PORT  = "/dev/cu.usbserial-0001"
SERIAL_BAUD  = 115200
CSV_FILE     = "history.csv"
ALERT_LOG    = "iris_alerts.log"
FLASK_PORT   = 5001

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8602694462:AAG6kmvo7SLzqO9Zr301ax8dabi1FFkkSFI")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID",   "1417496402")

TELEGRAM_MESSAGES = {
    "WATER":     "💧 Patient needs water!",
    "EMERGENCY": "🚨 EMERGENCY! Patient needs immediate help!",
}

# ─── Shared state (protected by GIL for simple types) ─────────────────────────

state = {
    "counts":      defaultdict(int),   # {"YES": 3, "NO": 1, "WATER": 5, ...}
    "total":       0,
    "emergencies": 0,
    "last_event":  "—",
    "last_ts":     "—",
    "tg_log":      [],                 # last sent Telegram messages
}

# SSE client queues – each connected browser tab gets its own queue
_sse_clients: list[queue.Queue] = []
_sse_lock = threading.Lock()

app = Flask(__name__)

# ─── CSV helpers ──────────────────────────────────────────────────────────────

def ensure_csv():
    if not os.path.exists(CSV_FILE):
        with open(CSV_FILE, "w", newline="") as f:
            csv.writer(f).writerow(["timestamp", "event", "raw"])

def append_csv(ts, event, raw):
    with open(CSV_FILE, "a", newline="") as f:
        csv.writer(f).writerow([ts, event, raw])

# ─── Alert log ────────────────────────────────────────────────────────────────

def write_alert_log(line):
    try:
        with open(ALERT_LOG, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass

# ─── macOS notification ───────────────────────────────────────────────────────

def send_macos_notification(title, message):
    script = f'display notification "{message}" with title "{title}" sound name "Sosumi"'
    try:
        subprocess.run(["osascript", "-e", script], check=False, timeout=5)
    except Exception:
        pass

# ─── Telegram ─────────────────────────────────────────────────────────────────

def _send_telegram(text):
    token = TELEGRAM_BOT_TOKEN.strip()
    chat  = TELEGRAM_CHAT_ID.strip()
    if not token or token == "YOUR_BOT_TOKEN_HERE":
        return False, "Token not configured"
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        r = req.post(url, json={"chat_id": chat, "text": text, "parse_mode": "HTML"}, timeout=8)
        return (True, "") if r.ok else (False, r.text[:120])
    except req.RequestException as e:
        return False, str(e)[:120]


def dispatch_telegram(event, msg, ts):
    """Runs on a daemon thread – never blocks the serial loop."""
    print(f"[ALERT] Sending Telegram for {event}: {msg}", flush=True)
    write_alert_log(f"[{ts}] ALERT {event}: {msg}")
    ok, err = _send_telegram(msg)
    entry = f"✅ [{ts}] {msg}" if ok else f"❌ [{ts}] {event} – {err}"
    print(f"[ALERT] Result → {entry}", flush=True)
    write_alert_log(f"[{ts}] RESULT: {entry}")
    state["tg_log"].append(entry)
    if len(state["tg_log"]) > 20:
        state["tg_log"] = state["tg_log"][-20:]

# ─── SSE broadcast ────────────────────────────────────────────────────────────

def broadcast(data: dict):
    """Push a JSON payload to every connected SSE browser client."""
    payload = "data: " + json.dumps(data) + "\n\n"
    with _sse_lock:
        dead = []
        for q in _sse_clients:
            try:
                q.put_nowait(payload)
            except queue.Full:
                dead.append(q)
        for q in dead:
            _sse_clients.remove(q)

# ─── Serial reader thread ─────────────────────────────────────────────────────

def serial_reader():
    ensure_csv()
    print(f"[SERIAL] Connecting to {SERIAL_PORT} at {SERIAL_BAUD} baud …")

    ser = None
    while True:
        # ── Open / reconnect ────────────────────────────────────────────────
        try:
            if ser is None or not ser.is_open:
                ser = serial.Serial(SERIAL_PORT, SERIAL_BAUD, timeout=1)
                print(f"[SERIAL] Connected to {SERIAL_PORT}")
        except serial.SerialException as e:
            print(f"[SERIAL] Cannot open port: {e}. Retrying in 3 s …")
            time.sleep(3)
            continue

        # ── Read loop ───────────────────────────────────────────────────────
        try:
            raw_bytes = ser.readline()
        except serial.SerialException as e:
            print(f"[SERIAL] Read error: {e}. Reconnecting …")
            try:
                ser.close()
            except Exception:
                pass
            ser = None
            time.sleep(2)
            continue

        if not raw_bytes:
            continue   # timeout, keep polling

        raw = raw_bytes.decode("utf-8", errors="replace").strip()
        if not raw:
            continue

        ts = datetime.now().isoformat(timespec="seconds")

        # ── Parse JSON ──────────────────────────────────────────────────────
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            append_csv(ts, "UNKNOWN", raw)
            continue

        event = (
            payload.get("event")
            or payload.get("type")
            or payload.get("status")
            or "UNKNOWN"
        )
        event = str(event).strip().upper()

        # ── Log + update state ──────────────────────────────────────────────
        append_csv(ts, event, raw)
        state["counts"][event] += 1
        state["total"]         += 1
        state["last_event"]     = event
        state["last_ts"]        = ts

        if event == "EMERGENCY":
            state["emergencies"] += 1
            send_macos_notification("⚠ IRIS EMERGENCY", f"Emergency at {ts}")

        # ── Telegram (non-blocking) ─────────────────────────────────────────
        if event in TELEGRAM_MESSAGES:
            threading.Thread(
                target=dispatch_telegram,
                args=(event, TELEGRAM_MESSAGES[event], ts),
                daemon=True,
            ).start()

        # ── Broadcast to SSE clients ────────────────────────────────────────
        broadcast({
            "event":       event,
            "ts":          ts,
            "counts":      dict(state["counts"]),
            "total":       state["total"],
            "emergencies": state["emergencies"],
            "last_event":  state["last_event"],
            "last_ts":     state["last_ts"],
            "tg_log":      state["tg_log"][-5:],
        })


# ─── Flask routes ─────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/state")
def api_state():
    """Initial state snapshot for page load."""
    return {
        "counts":      dict(state["counts"]),
        "total":       state["total"],
        "emergencies": state["emergencies"],
        "last_event":  state["last_event"],
        "last_ts":     state["last_ts"],
        "tg_log":      state["tg_log"][-5:],
    }


@app.route("/events")
def events():
    """Server-Sent Events stream – one per browser tab."""
    q: queue.Queue = queue.Queue(maxsize=50)
    with _sse_lock:
        _sse_clients.append(q)

    def generate():
        # Send a heartbeat immediately so the browser knows we're alive
        yield "data: {\"heartbeat\": true}\n\n"
        try:
            while True:
                try:
                    chunk = q.get(timeout=20)
                    yield chunk
                except queue.Empty:
                    yield ": heartbeat\n\n"   # SSE comment keeps connection alive
        except GeneratorExit:
            pass
        finally:
            with _sse_lock:
                try:
                    _sse_clients.remove(q)
                except ValueError:
                    pass

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ─── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Start serial reader in background
    t = threading.Thread(target=serial_reader, daemon=True)
    t.start()

    print(f"\n🚀  IRIS Dashboard → http://localhost:{FLASK_PORT}\n")
    # use_reloader=False is critical – reloader would spawn a second serial thread
    app.run(host="0.0.0.0", port=FLASK_PORT, debug=False, use_reloader=False)
