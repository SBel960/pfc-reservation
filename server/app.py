"""
Pyrénées Fighting Club — Reservation API
Flask + SQLite backend for real-time venue booking
Author: soufiane.to
"""

import os
import sqlite3
from datetime import datetime, timedelta
from functools import wraps

from flask import Flask, g, jsonify, request, send_from_directory
from flask_cors import CORS

# ── Config ──────────────────────────────────────────────
STATIC_DIR = os.path.join(os.path.dirname(__file__), "..")
app = Flask(__name__, static_folder=STATIC_DIR, static_url_path="")
CORS(app)

DB_PATH = os.path.join(os.path.dirname(__file__), "pfc_reservations.db")
PRICE_PER_HOUR = 30  # euros


# ── Database helpers ────────────────────────────────────
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA journal_mode=WAL")
        g.db.execute("PRAGMA foreign_keys=ON")
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    """Create tables and seed the weekly planning slots."""
    db = sqlite3.connect(DB_PATH)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")

    db.executescript("""
        CREATE TABLE IF NOT EXISTS slots (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            date        TEXT    NOT NULL,          -- YYYY-MM-DD
            start_time  TEXT    NOT NULL,          -- HH:MM
            end_time    TEXT    NOT NULL,          -- HH:MM
            status      TEXT    NOT NULL DEFAULT 'available',  -- available | club | reserved
            label       TEXT,                      -- e.g. "MMA", "Open Mat", NULL for free
            created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
            UNIQUE(date, start_time)
        );

        CREATE TABLE IF NOT EXISTS reservations (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            slot_id     INTEGER NOT NULL REFERENCES slots(id),
            client_name TEXT    NOT NULL,
            client_email TEXT   NOT NULL,
            client_phone TEXT,
            purpose     TEXT,                      -- what they'll use the room for
            status      TEXT    NOT NULL DEFAULT 'confirmed',  -- confirmed | cancelled
            price       REAL    NOT NULL,
            created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
            UNIQUE(slot_id)
        );

        CREATE INDEX IF NOT EXISTS idx_slots_date ON slots(date);
        CREATE INDEX IF NOT EXISTS idx_slots_status ON slots(status);
        CREATE INDEX IF NOT EXISTS idx_reservations_slot ON reservations(slot_id);
    """)

    db.commit()
    db.close()


# ── Club schedule (from PFC planning) ──────────────────
# day_index: 0=Monday .. 5=Saturday
CLUB_SCHEDULE = [
    # Monday
    {"day": 0, "start": "09:00", "end": "12:00", "label": "Cours privés"},
    {"day": 0, "start": "12:00", "end": "14:00", "label": "Open Mat"},
    {"day": 0, "start": "14:00", "end": "16:00", "label": "Cours privés / Open Mat"},
    {"day": 0, "start": "18:00", "end": "20:00", "label": "Grappling / JJB"},
    {"day": 0, "start": "20:00", "end": "22:00", "label": "Cross Training"},
    # Tuesday
    {"day": 1, "start": "09:00", "end": "12:00", "label": "Cours privés"},
    {"day": 1, "start": "12:00", "end": "14:00", "label": "Cross Training"},
    {"day": 1, "start": "14:00", "end": "16:00", "label": "Cours privés / Open Mat"},
    {"day": 1, "start": "18:00", "end": "20:00", "label": "Kick Boxing"},
    {"day": 1, "start": "20:00", "end": "22:00", "label": "MMA"},
    # Wednesday
    {"day": 2, "start": "09:00", "end": "12:00", "label": "Cours privés"},
    {"day": 2, "start": "14:00", "end": "16:00", "label": "Karaté enfant 6/9 ans"},
    {"day": 2, "start": "16:00", "end": "17:00", "label": "Karaté ado 10/14 ans"},
    {"day": 2, "start": "18:00", "end": "20:00", "label": "Cross Training"},
    {"day": 2, "start": "20:00", "end": "22:00", "label": "Karaté Adulte"},
    # Thursday
    {"day": 3, "start": "09:00", "end": "12:00", "label": "Cours privés"},
    {"day": 3, "start": "14:00", "end": "16:00", "label": "Cours privés"},
    {"day": 3, "start": "18:00", "end": "20:00", "label": "Fit Boxing"},
    {"day": 3, "start": "20:00", "end": "22:00", "label": "MMA"},
    # Friday
    {"day": 4, "start": "09:00", "end": "12:00", "label": "Cours privés"},
    {"day": 4, "start": "12:00", "end": "14:00", "label": "Cross Training"},
    # Saturday
    {"day": 5, "start": "09:00", "end": "12:00", "label": "Open Mat"},
    {"day": 5, "start": "20:00", "end": "22:00", "label": "Open Mat"},
]

# Time blocks for the venue (7:00 to 23:00, 1-hour slots)
VENUE_HOURS = [(f"{h:02d}:00", f"{h+1:02d}:00") for h in range(7, 23)]


def is_club_slot(day_index, start, end):
    """Check if a time range overlaps with any club activity."""
    s = int(start.replace(":", ""))
    e = int(end.replace(":", ""))
    for cs in CLUB_SCHEDULE:
        if cs["day"] == day_index:
            cs_s = int(cs["start"].replace(":", ""))
            cs_e = int(cs["end"].replace(":", ""))
            if s < cs_e and e > cs_s:
                return cs["label"]
    return None


def generate_slots_for_date(db, date_str):
    """Generate hourly slots for a given date, marking club vs available."""
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    day_index = dt.weekday()  # 0=Monday

    if day_index == 6:  # Sunday — closed
        return

    for start, end in VENUE_HOURS:
        existing = db.execute(
            "SELECT id FROM slots WHERE date=? AND start_time=?",
            (date_str, start),
        ).fetchone()

        if existing:
            continue

        club_label = is_club_slot(day_index, start, end)
        if club_label:
            db.execute(
                "INSERT INTO slots (date, start_time, end_time, status, label) VALUES (?,?,?,?,?)",
                (date_str, start, end, "club", club_label),
            )
        else:
            db.execute(
                "INSERT INTO slots (date, start_time, end_time, status, label) VALUES (?,?,?,?,?)",
                (date_str, start, end, "available", None),
            )

    db.commit()


# ── API Routes ──────────────────────────────────────────
@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "PFC Reservation API"})


@app.route("/api/slots", methods=["GET"])
def get_slots():
    """
    GET /api/slots?date=2026-04-01
    Returns all slots for a given date with their availability.
    If slots don't exist yet, generates them on the fly.
    """
    date_str = request.args.get("date")
    if not date_str:
        return jsonify({"error": "Parameter 'date' is required (YYYY-MM-DD)"}), 400

    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        return jsonify({"error": "Invalid date format. Use YYYY-MM-DD"}), 400

    if dt.weekday() == 6:
        return jsonify({"date": date_str, "day": "Dimanche", "closed": True, "slots": []})

    db = get_db()

    # Generate slots if they don't exist
    existing = db.execute("SELECT COUNT(*) as c FROM slots WHERE date=?", (date_str,)).fetchone()
    if existing["c"] == 0:
        generate_slots_for_date(db, date_str)

    rows = db.execute("""
        SELECT s.id, s.date, s.start_time, s.end_time, s.status, s.label,
               r.client_name, r.purpose, r.id as reservation_id
        FROM slots s
        LEFT JOIN reservations r ON r.slot_id = s.id AND r.status = 'confirmed'
        WHERE s.date = ?
        ORDER BY s.start_time
    """, (date_str,)).fetchall()

    days_fr = ["Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi", "Dimanche"]

    slots = []
    for row in rows:
        slot = {
            "id": row["id"],
            "start": row["start_time"],
            "end": row["end_time"],
            "status": row["status"],
            "label": row["label"],
        }
        if row["status"] == "reserved" and row["reservation_id"]:
            slot["reservation"] = {
                "id": row["reservation_id"],
                "client": row["client_name"],
                "purpose": row["purpose"],
            }
        slots.append(slot)

    return jsonify({
        "date": date_str,
        "day": days_fr[dt.weekday()],
        "closed": False,
        "slots": slots,
    })


@app.route("/api/slots/week", methods=["GET"])
def get_week_slots():
    """
    GET /api/slots/week?from=2026-04-01
    Returns slots for 7 days starting from the given date.
    """
    from_str = request.args.get("from")
    if not from_str:
        from_str = datetime.now().strftime("%Y-%m-%d")

    try:
        start_dt = datetime.strptime(from_str, "%Y-%m-%d")
    except ValueError:
        return jsonify({"error": "Invalid date format"}), 400

    # Find the Monday of that week
    monday = start_dt - timedelta(days=start_dt.weekday())

    db = get_db()
    days_fr = ["Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi", "Dimanche"]
    week = []

    for i in range(7):
        day_dt = monday + timedelta(days=i)
        date_str = day_dt.strftime("%Y-%m-%d")

        if i == 6:  # Sunday
            week.append({"date": date_str, "day": days_fr[i], "closed": True, "slots": []})
            continue

        existing = db.execute("SELECT COUNT(*) as c FROM slots WHERE date=?", (date_str,)).fetchone()
        if existing["c"] == 0:
            generate_slots_for_date(db, date_str)

        rows = db.execute("""
            SELECT s.id, s.start_time, s.end_time, s.status, s.label
            FROM slots s
            WHERE s.date = ?
            ORDER BY s.start_time
        """, (date_str,)).fetchall()

        slots = [{"id": r["id"], "start": r["start_time"], "end": r["end_time"],
                  "status": r["status"], "label": r["label"]} for r in rows]

        week.append({"date": date_str, "day": days_fr[i], "closed": False, "slots": slots})

    return jsonify({"week_of": monday.strftime("%Y-%m-%d"), "days": week})


@app.route("/api/reserve", methods=["POST"])
def reserve_slot():
    """
    POST /api/reserve
    Body: { slot_id, client_name, client_email, client_phone?, purpose? }
    Reserves an available slot.
    """
    data = request.get_json()
    if not data:
        return jsonify({"error": "JSON body required"}), 400

    required = ["slot_id", "client_name", "client_email"]
    for field in required:
        if field not in data or not data[field]:
            return jsonify({"error": f"Field '{field}' is required"}), 400

    db = get_db()

    # Check slot exists and is available
    slot = db.execute("SELECT * FROM slots WHERE id=?", (data["slot_id"],)).fetchone()
    if not slot:
        return jsonify({"error": "Slot not found"}), 404
    if slot["status"] != "available":
        return jsonify({"error": "Slot is not available", "current_status": slot["status"]}), 409

    # Calculate price based on duration
    start_h = int(slot["start_time"].split(":")[0])
    end_h = int(slot["end_time"].split(":")[0])
    hours = end_h - start_h
    price = hours * PRICE_PER_HOUR

    # Transaction: update slot + create reservation
    try:
        db.execute("UPDATE slots SET status='reserved' WHERE id=?", (data["slot_id"],))
        db.execute("""
            INSERT INTO reservations (slot_id, client_name, client_email, client_phone, purpose, price)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            data["slot_id"],
            data["client_name"],
            data["client_email"],
            data.get("client_phone", ""),
            data.get("purpose", ""),
            price,
        ))
        db.commit()
    except sqlite3.IntegrityError:
        db.rollback()
        return jsonify({"error": "Slot already reserved (race condition)"}), 409

    reservation = db.execute(
        "SELECT * FROM reservations WHERE slot_id=?", (data["slot_id"],)
    ).fetchone()

    return jsonify({
        "success": True,
        "reservation": {
            "id": reservation["id"],
            "date": slot["date"],
            "start": slot["start_time"],
            "end": slot["end_time"],
            "client": data["client_name"],
            "price": price,
            "status": "confirmed",
        },
    }), 201


@app.route("/api/reservations", methods=["GET"])
def list_reservations():
    """
    GET /api/reservations?from=2026-04-01&to=2026-04-07
    Admin endpoint: list all reservations in a date range.
    """
    from_date = request.args.get("from", datetime.now().strftime("%Y-%m-%d"))
    to_date = request.args.get("to", (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d"))

    db = get_db()
    rows = db.execute("""
        SELECT r.*, s.date, s.start_time, s.end_time
        FROM reservations r
        JOIN slots s ON s.id = r.slot_id
        WHERE s.date BETWEEN ? AND ? AND r.status = 'confirmed'
        ORDER BY s.date, s.start_time
    """, (from_date, to_date)).fetchall()

    return jsonify({
        "reservations": [{
            "id": r["id"],
            "date": r["date"],
            "start": r["start_time"],
            "end": r["end_time"],
            "client_name": r["client_name"],
            "client_email": r["client_email"],
            "client_phone": r["client_phone"],
            "purpose": r["purpose"],
            "price": r["price"],
            "created_at": r["created_at"],
        } for r in rows],
        "total_revenue": sum(r["price"] for r in rows),
    })


@app.route("/api/cancel/<int:reservation_id>", methods=["POST"])
def cancel_reservation(reservation_id):
    """Cancel a reservation and free the slot."""
    db = get_db()

    res = db.execute("SELECT * FROM reservations WHERE id=?", (reservation_id,)).fetchone()
    if not res:
        return jsonify({"error": "Reservation not found"}), 404
    if res["status"] == "cancelled":
        return jsonify({"error": "Already cancelled"}), 409

    db.execute("UPDATE reservations SET status='cancelled' WHERE id=?", (reservation_id,))
    db.execute("UPDATE slots SET status='available' WHERE id=?", (res["slot_id"],))
    db.commit()

    return jsonify({"success": True, "message": "Reservation cancelled, slot freed"})


@app.route("/api/stats", methods=["GET"])
def stats():
    """Quick dashboard stats for the club admin."""
    db = get_db()

    total_res = db.execute(
        "SELECT COUNT(*) as c FROM reservations WHERE status='confirmed'"
    ).fetchone()["c"]

    revenue = db.execute(
        "SELECT COALESCE(SUM(price),0) as s FROM reservations WHERE status='confirmed'"
    ).fetchone()["s"]

    this_week = db.execute("""
        SELECT COUNT(*) as c FROM reservations r
        JOIN slots s ON s.id = r.slot_id
        WHERE r.status='confirmed'
        AND s.date BETWEEN date('now','weekday 1','-7 days') AND date('now','weekday 0')
    """).fetchone()["c"]

    return jsonify({
        "total_reservations": total_res,
        "total_revenue": revenue,
        "this_week": this_week,
        "price_per_hour": PRICE_PER_HOUR,
    })


# ── Serve frontend ──────────────────────────────────────
@app.route("/")
def serve_index():
    return send_from_directory(STATIC_DIR, "index.html")


# ── Entry point ─────────────────────────────────────────
init_db()

if __name__ == "__main__":
    print("✦ PFC running on http://localhost:5000")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
