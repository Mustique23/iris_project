#!/usr/bin/env python3
"""
iris_analytics.py  –  IRIS Caregiver Edition
─────────────────────────────────────────────
Reads JSON events from an ESP32 on /dev/cu.usbserial-0001 (115200 baud),
logs every event to history.csv, displays a live ASCII dashboard, fires a
macOS system notification on EMERGENCY, AND sends Telegram Bot messages:

    WATER     → 💧 Patient needs water!
    EMERGENCY → 🚨 EMERGENCY! Patient needs immediate help!

Requirements:
    pip install pyserial requests

How to configure:
    Edit TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID below, or set them as
    environment variables:
        export TELEGRAM_BOT_TOKEN="your_token_here"
        export TELEGRAM_CHAT_ID="your_chat_id_here"

Usage:
    python3 iris_analytics.py
    python3 iris_analytics.py --port /dev/cu.usbserial-0002 --baud 9600
"""

import argparse
import csv
import json
import os
import subprocess
import sys
import threading
import time
from collections import defaultdict
from datetime import datetime

try:
    import serial
except ImportError:
    print("[ERROR] pyserial is not installed. Run:  pip install pyserial")
    sys.exit(1)

try:
    import requests
except ImportError:
    print("[ERROR] requests is not installed. Run:  pip install requests")
    sys.exit(1)

# ─── Telegram configuration ──────────────────────────────────────────────────
# You can hard-code the values here OR set them via environment variables.
# Leave as empty strings if you have not set up Telegram yet; alerts will
# simply be skipped and a warning shown in the dashboard.

TELEGRAM_BOT_TOKEN: str = os.environ.get("TELEGRAM_BOT_TOKEN", "8602694462:AAG6kmvo7SLzqO9Zr301ax8dabi1FFkkSFI")
TELEGRAM_CHAT_ID:   str = os.environ.get("TELEGRAM_CHAT_ID",   "1417496402")

# Messages sent per event type
TELEGRAM_MESSAGES: dict = {
    "WATER":     "💧 Patient needs water!",
    "EMERGENCY": "🚨 EMERGENCY! Patient needs immediate help!",
}

# ─── General configuration ────────────────────────────────────────────────────
DEFAULT_PORT     = "/dev/cu.usbserial-0001"
DEFAULT_BAUD     = 115200
CSV_FILE         = "history.csv"
DASHBOARD_EVENTS = ["YES", "NO", "WATER"]  # shown in the bar chart
EMERGENCY_KEY    = "EMERGENCY"
REFRESH_RATE     = 0.1   # seconds between dashboard redraws on idle
ALERT_LOG        = "iris_alerts.log"  # plain-text log that survives clear_screen

# ─── CSV helpers ──────────────────────────────────────────────────────────────

def ensure_csv(path: str) -> None:
    if not os.path.exists(path):
        with open(path, "w", newline="") as f:
            csv.writer(f).writerow(["timestamp", "event", "raw"])
        print(f"[INFO] Created {path}")


def append_csv(path: str, timestamp: str, event: str, raw: str) -> None:
    with open(path, "a", newline="") as f:
        csv.writer(f).writerow([timestamp, event, raw])

# ─── macOS notification ───────────────────────────────────────────────────────

def send_macos_notification(title: str, message: str) -> None:
    script = (
        f'display notification "{message}" '
        f'with title "{title}" '
        f'sound name "Sosumi"'
    )
    try:
        subprocess.run(["osascript", "-e", script], check=False, timeout=5)
    except Exception as exc:
        print(f"[WARN] macOS notification failed: {exc}")

# ─── Telegram sender ──────────────────────────────────────────────────────────

def _write_alert_log(line: str) -> None:
    """Append *line* to the persistent alert log file."""
    try:
        with open(ALERT_LOG, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def send_telegram(text: str) -> tuple[bool, str]:
    """
    Send *text* via the Telegram Bot API.
    Returns (success: bool, error_message: str).
    Called from a background thread so it never blocks serial reading.
    """
    token = TELEGRAM_BOT_TOKEN.strip()
    chat  = TELEGRAM_CHAT_ID.strip()

    if not token or token == "YOUR_BOT_TOKEN_HERE":
        return False, "Bot token not configured"
    if not chat or chat == "YOUR_CHAT_ID_HERE":
        return False, "Chat ID not configured"

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        resp = requests.post(
            url,
            json={"chat_id": chat, "text": text, "parse_mode": "HTML"},
            timeout=8,
        )
        if resp.ok:
            return True, ""
        return False, resp.text[:120]
    except requests.RequestException as exc:
        return False, str(exc)[:120]


def dispatch_telegram(event: str, msg: str, ts: str, tg_log: list) -> None:
    """
    Runs in a daemon thread. Sends the Telegram message, then appends the
    result to *tg_log* (shared list, GIL-safe for append) and the alert log.
    """
    print(f"[ALERT] Sending Telegram alert for {event}: {msg}", flush=True)
    _write_alert_log(f"[{ts}] ALERT {event}: {msg}")

    ok, err = send_telegram(msg)
    if ok:
        entry = f"✅ [{ts}] Sent: {msg}"
    else:
        entry = f"❌ [{ts}] {event} – {err}"

    print(f"[ALERT] Result → {entry}", flush=True)
    _write_alert_log(f"[{ts}] RESULT: {entry}")
    tg_log.append(entry)

# ─── ASCII dashboard ──────────────────────────────────────────────────────────

BAR_WIDTH = 30

def _bar(count: int, max_count: int) -> str:
    filled = 0 if max_count == 0 else int(round((count / max_count) * BAR_WIDTH))
    return "█" * filled + "░" * (BAR_WIDTH - filled)


def render_dashboard(
    counts: dict,
    total: int,
    emergencies: int,
    last_event: str,
    last_ts: str,
    port: str,
    tg_log: list,          # recent Telegram status messages
) -> str:
    max_count = max((counts[e] for e in DASHBOARD_EVENTS), default=1) or 1
    lines = []
    w = BAR_WIDTH + 34

    tg_ok = TELEGRAM_BOT_TOKEN not in ("", "YOUR_BOT_TOKEN_HERE")
    tg_status = "✅ configured" if tg_ok else "⚠ not configured"

    lines.append("╔" + "═" * w + "╗")
    lines.append("║" + " IRIS ANALYTICS  –  Caregiver Dashboard ".center(w) + "║")
    lines.append("║" + f" Port: {port}  •  {datetime.now().strftime('%Y-%m-%d  %H:%M:%S')} ".center(w) + "║")
    lines.append("║" + f" Telegram: {tg_status} ".center(w) + "║")
    lines.append("╠" + "═" * w + "╣")

    for event in DASHBOARD_EVENTS:
        c   = counts[event]
        pct = (c / total * 100) if total else 0
        bar = _bar(c, max_count)
        row = f" {event:<8} [{bar}]  {c:>5}  ({pct:5.1f}%)"
        lines.append("║ " + row.ljust(w - 1) + "║")

    lines.append("╠" + "═" * w + "╣")

    summary = f" Total events: {total}   🚨 Emergencies: {emergencies}"
    lines.append("║" + summary.ljust(w) + "║")

    last = f" Last event : {last_event}  @  {last_ts}"
    lines.append("║" + last.ljust(w) + "║")

    lines.append("╠" + "═" * w + "╣")
    lines.append("║" + " Telegram log (last 4) ".center(w) + "║")
    recent = tg_log[-4:]                           # snapshot; never mutates outer list
    for entry in recent:
        lines.append("║  " + entry[:w - 2].ljust(w - 2) + "║")
    for _ in range(4 - len(recent)):               # pad blank rows without touching tg_log
        lines.append("║" + " " * w + "║")

    lines.append("╚" + "═" * w + "╝")
    lines.append(" Press Ctrl+C to quit.")
    return "\n".join(lines)


def clear_screen() -> None:
    print("\033[2J\033[H", end="", flush=True)

# ─── Main loop ────────────────────────────────────────────────────────────────

def run(port: str, baud: int) -> None:
    ensure_csv(CSV_FILE)
    counts: dict      = defaultdict(int)
    total: int        = 0
    emergencies: int  = 0
    last_event: str   = "—"
    last_ts: str      = "—"
    tg_log: list      = []   # status strings shown in the dashboard

    print(f"[INFO] Opening {port} at {baud} baud …")
    try:
        ser = serial.Serial(port, baud, timeout=1)
    except serial.SerialException as exc:
        print(f"[ERROR] Cannot open serial port: {exc}")
        sys.exit(1)

    print("[INFO] Connected. Waiting for data from ESP32 …\n")
    time.sleep(0.5)

    try:
        while True:
            try:
                # ── Read one line ────────────────────────────────────────────────
                raw_bytes = ser.readline()
                if not raw_bytes:
                    clear_screen()
                    print(render_dashboard(counts, total, emergencies,
                                           last_event, last_ts, port, tg_log))
                    time.sleep(REFRESH_RATE)
                    continue

                raw = raw_bytes.decode("utf-8", errors="replace").strip()
                if not raw:
                    continue

                ts = datetime.now().isoformat(timespec="seconds")

                # ── Parse JSON ───────────────────────────────────────────────────
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError:
                    append_csv(CSV_FILE, ts, "UNKNOWN", raw)
                    continue

                # ── Extract event type ───────────────────────────────────────────
                event = (
                    payload.get("event")
                    or payload.get("type")
                    or payload.get("status")
                    or "UNKNOWN"
                )
                event = str(event).strip().upper()

                # ── Log to CSV ───────────────────────────────────────────────────
                append_csv(CSV_FILE, ts, event, raw)

                # ── Update counters ──────────────────────────────────────────────
                counts[event] += 1
                total         += 1
                last_event     = event
                last_ts        = ts

                # ── Telegram alert (WATER or EMERGENCY) – non-blocking thread ─────
                if event in TELEGRAM_MESSAGES:
                    msg = TELEGRAM_MESSAGES[event]
                    t = threading.Thread(
                        target=dispatch_telegram,
                        args=(event, msg, ts, tg_log),
                        daemon=True,
                    )
                    t.start()

                # ── macOS notification on EMERGENCY ──────────────────────────────
                if event == EMERGENCY_KEY:
                    emergencies += 1
                    send_macos_notification(
                        "⚠ IRIS EMERGENCY ALERT",
                        f"Emergency event received at {ts}",
                    )

                # ── Redraw dashboard ─────────────────────────────────────────────
                clear_screen()
                print(render_dashboard(counts, total, emergencies,
                                       last_event, last_ts, port, tg_log))
            
            except serial.SerialException as e:
                clear_screen()
                print(f"[RECONNECTING] Loss of connection: {e}")
                time.sleep(2)
                try:
                    ser.close()
                    ser.open()
                except Exception:
                    print("Could not reconnect yet. Retrying...")
                    continue

    except KeyboardInterrupt:
        print("\n[INFO] Stopping … Goodbye!")
    finally:
        ser.close()

# ─── Entry point ──────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="IRIS Analytics – Caregiver Edition with Telegram Bot"
    )
    parser.add_argument("--port", default=DEFAULT_PORT,
                        help=f"Serial port (default: {DEFAULT_PORT})")
    parser.add_argument("--baud", type=int, default=DEFAULT_BAUD,
                        help=f"Baud rate (default: {DEFAULT_BAUD})")
    args = parser.parse_args()
    run(args.port, args.baud)


if __name__ == "__main__":
    main()
