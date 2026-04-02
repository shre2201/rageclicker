from flask import Flask, request, jsonify, render_template, send_from_directory
from flask_cors import CORS
import sqlite3, json, uuid, time, os

app = Flask(__name__)
CORS(app)

DB = "data/events.db"

def get_db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    os.makedirs("data", exist_ok=True)
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            event_type TEXT,
            x INTEGER,
            y INTEGER,
            ts REAL,
            element TEXT,
            page TEXT,
            dom_changed INTEGER DEFAULT 0,
            scroll_y INTEGER DEFAULT 0,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            session_id TEXT PRIMARY KEY,
            start_ts REAL,
            last_ts REAL,
            page_count INTEGER DEFAULT 1,
            frustrated INTEGER DEFAULT 0,
            frustration_score REAL DEFAULT 0.0
        )
    """)
    conn.commit()
    conn.close()

# ── Event ingestion ──────────────────────────────────────────────
@app.route("/event", methods=["POST"])
def ingest_event():
    data = request.json
    session_id = data.get("session_id", str(uuid.uuid4()))
    ts = data.get("ts", time.time())
    conn = get_db()
    conn.execute("""
        INSERT INTO events (session_id, event_type, x, y, ts, element, page, dom_changed, scroll_y)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        session_id,
        data.get("event_type", "click"),
        data.get("x", 0), data.get("y", 0), ts,
        data.get("element", ""),
        data.get("page", "/"),
        int(data.get("dom_changed", 0)),
        data.get("scroll_y", 0)
    ))
    # Upsert session
    conn.execute("""
        INSERT INTO sessions (session_id, start_ts, last_ts)
        VALUES (?, ?, ?)
        ON CONFLICT(session_id) DO UPDATE SET last_ts=excluded.last_ts
    """, (session_id, ts, ts))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})

# ── Analytics API ────────────────────────────────────────────────
@app.route("/api/clicks")
def get_clicks():
    page = request.args.get("page", "/")
    conn = get_db()
    rows = conn.execute(
        "SELECT x, y, dom_changed FROM events WHERE event_type='click' AND page=?", (page,)
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route("/api/sessions")
def get_sessions():
    conn = get_db()
    sessions = conn.execute("""
        SELECT s.session_id,
               s.start_ts, s.last_ts,
               COUNT(e.id) as total_clicks,
               SUM(CASE WHEN e.dom_changed=0 THEN 1 ELSE 0 END) as dead_clicks,
               s.frustrated, s.frustration_score
        FROM sessions s
        LEFT JOIN events e ON e.session_id=s.session_id AND e.event_type='click'
        GROUP BY s.session_id
        ORDER BY s.last_ts DESC
        LIMIT 100
    """).fetchall()
    conn.close()
    result = []
    for s in sessions:
        d = dict(s)
        d["duration_s"] = round((d["last_ts"] or 0) - (d["start_ts"] or 0), 1)
        result.append(d)
    return jsonify(result)

@app.route("/api/stats")
def get_stats():
    conn = get_db()
    total = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    frustrated = conn.execute("SELECT COUNT(*) FROM sessions WHERE frustrated=1").fetchone()[0]
    total_clicks = conn.execute("SELECT COUNT(*) FROM events WHERE event_type='click'").fetchone()[0]
    dead_clicks = conn.execute("SELECT COUNT(*) FROM events WHERE event_type='click' AND dom_changed=0").fetchone()[0]
    top_element = conn.execute("""
        SELECT element, COUNT(*) as c FROM events
        WHERE event_type='click' AND dom_changed=0 AND element!=''
        GROUP BY element ORDER BY c DESC LIMIT 1
    """).fetchone()
    conn.close()
    return jsonify({
        "total_sessions": total,
        "frustrated_sessions": frustrated,
        "frustrated_pct": round(frustrated / total * 100, 1) if total else 0,
        "total_clicks": total_clicks,
        "dead_click_pct": round(dead_clicks / total_clicks * 100, 1) if total_clicks else 0,
        "top_dead_element": dict(top_element) if top_element else None
    })

@app.route("/api/analyze/<session_id>", methods=["POST"])
def analyze_session(session_id):
    conn = get_db()
    events = conn.execute(
        "SELECT * FROM events WHERE session_id=? ORDER BY ts ASC", (session_id,)
    ).fetchall()
    events = [dict(e) for e in events]
    clicks = [e for e in events if e["event_type"] == "click"]

    # Rage burst detection: 3+ clicks within 1500ms in 60x60px radius
    rage_bursts = 0
    i = 0
    while i < len(clicks):
        cluster = [clicks[i]]
        for j in range(i+1, len(clicks)):
            dt = clicks[j]["ts"] - clicks[i]["ts"]
            dx = abs(clicks[j]["x"] - clicks[i]["x"])
            dy = abs(clicks[j]["y"] - clicks[i]["y"])
            if dt <= 1.5 and dx <= 60 and dy <= 60:
                cluster.append(clicks[j])
            else:
                break
        if len(cluster) >= 3:
            rage_bursts += 1
        i += len(cluster) if len(cluster) > 1 else 1

    dead_clicks = sum(1 for c in clicks if not c["dom_changed"])
    pages = list(dict.fromkeys(e["page"] for e in events))
    u_turns = 0
    for k in range(1, len(pages)-1):
        if pages[k] == pages[k-2]:
            u_turns += 1

    # Frustration score heuristic (0-1)
    score = min(1.0, (rage_bursts * 0.4) + (dead_clicks * 0.05) + (u_turns * 0.2))
    frustrated = 1 if (rage_bursts >= 1 or dead_clicks >= 3) else 0

    conn.execute("""
        UPDATE sessions SET frustrated=?, frustration_score=? WHERE session_id=?
    """, (frustrated, score, session_id))
    conn.commit()
    conn.close()

    return jsonify({
        "session_id": session_id,
        "rage_bursts": rage_bursts,
        "dead_clicks": dead_clicks,
        "u_turns": u_turns,
        "frustration_score": round(score, 2),
        "frustrated": bool(frustrated)
    })

@app.route("/api/analyze_all", methods=["POST"])
def analyze_all():
    conn = get_db()
    sessions = conn.execute("SELECT session_id FROM sessions").fetchall()
    conn.close()
    results = []
    for s in sessions:
        import requests as req
        r = app.test_client().post(f"/api/analyze/{s['session_id']}")
        results.append(json.loads(r.data))
    return jsonify({"analyzed": len(results), "results": results})

# ── Pages ────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("site/index.html")

@app.route("/shop")
def shop():
    return render_template("site/shop.html")

@app.route("/contact")
def contact():
    return render_template("site/contact.html")

@app.route("/checkout")
def checkout():
    return render_template("site/checkout.html")

@app.route("/dashboard")
def dashboard():
    return render_template("dashboard.html")

if __name__ == "__main__":
    init_db()
    print("\n  Rage Click Detector running!")
    print("  Demo site   → http://localhost:5000")
    print("  Dashboard   → http://localhost:5000/dashboard\n")
    app.run(debug=True, port=5000)
