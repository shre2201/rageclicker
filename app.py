from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
import uuid, time, os, json

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

# ── Database connection ───────────────────────────────────────────
# Local fallback: plain sqlite3
# Production (Render): set TURSO_URL and TURSO_TOKEN env vars in Render dashboard
TURSO_URL   = os.environ.get("TURSO_URL", "")
TURSO_TOKEN = os.environ.get("TURSO_TOKEN", "")

def get_db():
    if TURSO_URL and TURSO_TOKEN:
        import libsql_experimental as libsql
        conn = libsql.connect(
            database=":memory:",
            sync_url=TURSO_URL,
            auth_token=TURSO_TOKEN,
            autocommit=True
        )
        conn.sync()
    else:
        import sqlite3
        os.makedirs("data", exist_ok=True)
        conn = sqlite3.connect("data/events.db")
        conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            event_type TEXT,
            x INTEGER, y INTEGER,
            ts REAL,
            element TEXT,
            page TEXT,
            dom_changed INTEGER DEFAULT 0,
            scroll_y INTEGER DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            session_id TEXT PRIMARY KEY,
            start_ts REAL,
            last_ts REAL,
            frustrated INTEGER DEFAULT 0,
            frustration_score REAL DEFAULT 0.0
        )
    """)
    if TURSO_URL:
        conn.sync()
    else:
        conn.commit()
    conn.close()

def fetchall(conn, sql, params=()):
    cur = conn.execute(sql, params)
    rows = cur.fetchall()
    # libsql rows don't have .keys() — detect and normalise
    if rows and not hasattr(rows[0], 'keys'):
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in rows]
    return [dict(r) for r in rows]

def fetchone(conn, sql, params=()):
    cur = conn.execute(sql, params)
    row = cur.fetchone()
    if row is None:
        return None
    if not hasattr(row, 'keys'):
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, row))
    return dict(row)

def db_commit(conn):
    if TURSO_URL:
        conn.sync()
    else:
        conn.commit()

# ── Keep-alive ping ───────────────────────────────────────────────
@app.route("/ping")
def ping():
    return jsonify({"status": "alive", "ts": time.time()})

# ── Event ingestion ───────────────────────────────────────────────
@app.route("/event", methods=["POST"])
def ingest_event():
    data = request.json or {}
    session_id = data.get("session_id", str(uuid.uuid4()))
    ts = data.get("ts", time.time())
    conn = get_db()
    conn.execute(
        "INSERT INTO events (session_id,event_type,x,y,ts,element,page,dom_changed,scroll_y) VALUES (?,?,?,?,?,?,?,?,?)",
        (session_id, data.get("event_type","click"),
         data.get("x",0), data.get("y",0), ts,
         data.get("element",""), data.get("page","/"),
         int(data.get("dom_changed",0)), data.get("scroll_y",0))
    )
    conn.execute(
        "INSERT INTO sessions (session_id,start_ts,last_ts) VALUES (?,?,?) "
        "ON CONFLICT(session_id) DO UPDATE SET last_ts=excluded.last_ts",
        (session_id, ts, ts)
    )
    db_commit(conn)
    conn.close()
    return jsonify({"ok": True})

# ── Analytics API ─────────────────────────────────────────────────
@app.route("/api/clicks")
def get_clicks():
    page = request.args.get("page", "/")
    conn = get_db()
    rows = fetchall(conn, "SELECT x,y,dom_changed FROM events WHERE event_type='click' AND page=?", (page,))
    conn.close()
    return jsonify(rows)

@app.route("/api/sessions")
def get_sessions():
    conn = get_db()
    rows = fetchall(conn, """
        SELECT s.session_id, s.start_ts, s.last_ts,
               COUNT(e.id) as total_clicks,
               SUM(CASE WHEN e.dom_changed=0 THEN 1 ELSE 0 END) as dead_clicks,
               s.frustrated, s.frustration_score
        FROM sessions s
        LEFT JOIN events e ON e.session_id=s.session_id AND e.event_type='click'
        GROUP BY s.session_id
        ORDER BY s.last_ts DESC LIMIT 100
    """)
    conn.close()
    for r in rows:
        r["duration_s"] = round((r.get("last_ts") or 0) - (r.get("start_ts") or 0), 1)
        r["dead_clicks"] = r.get("dead_clicks") or 0
        r["total_clicks"] = r.get("total_clicks") or 0
    return jsonify(rows)

@app.route("/api/stats")
def get_stats():
    conn = get_db()
    total      = (fetchone(conn, "SELECT COUNT(*) as c FROM sessions") or {}).get("c", 0)
    frustrated = (fetchone(conn, "SELECT COUNT(*) as c FROM sessions WHERE frustrated=1") or {}).get("c", 0)
    total_cl   = (fetchone(conn, "SELECT COUNT(*) as c FROM events WHERE event_type='click'") or {}).get("c", 0)
    dead_cl    = (fetchone(conn, "SELECT COUNT(*) as c FROM events WHERE event_type='click' AND dom_changed=0") or {}).get("c", 0)
    top_el     = fetchone(conn, "SELECT element,COUNT(*) as c FROM events WHERE event_type='click' AND dom_changed=0 AND element!='' GROUP BY element ORDER BY c DESC LIMIT 1")
    conn.close()
    return jsonify({
        "total_sessions": total,
        "frustrated_sessions": frustrated,
        "frustrated_pct": round(frustrated / total * 100, 1) if total else 0,
        "total_clicks": total_cl,
        "dead_click_pct": round(dead_cl / total_cl * 100, 1) if total_cl else 0,
        "top_dead_element": top_el
    })

@app.route("/api/analyze/<session_id>", methods=["POST"])
def analyze_session(session_id):
    conn = get_db()
    events = fetchall(conn, "SELECT * FROM events WHERE session_id=? ORDER BY ts ASC", (session_id,))
    clicks = [e for e in events if e["event_type"] == "click"]

    # Rage burst: 3+ clicks within 1500ms in 60x60px
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
    u_turns = sum(1 for k in range(1, len(pages)-1) if pages[k] == pages[k-2])

    score = min(1.0, rage_bursts * 0.4 + dead_clicks * 0.05 + u_turns * 0.2)
    frustrated = 1 if (rage_bursts >= 1 or dead_clicks >= 3) else 0

    conn.execute("UPDATE sessions SET frustrated=?,frustration_score=? WHERE session_id=?",
                 (frustrated, score, session_id))
    db_commit(conn)
    conn.close()
    return jsonify({"session_id": session_id, "rage_bursts": rage_bursts,
                    "dead_clicks": dead_clicks, "u_turns": u_turns,
                    "frustration_score": round(score, 2), "frustrated": bool(frustrated)})

@app.route("/api/export")
def export_csv():
    """Download all sessions as CSV for ML training."""
    conn = get_db()
    rows = fetchall(conn, """
        SELECT s.session_id, s.frustrated, s.frustration_score,
               COUNT(e.id) as total_clicks,
               SUM(CASE WHEN e.dom_changed=0 THEN 1 ELSE 0 END) as dead_clicks,
               MAX(e.scroll_y) as max_scroll,
               (s.last_ts - s.start_ts) as duration_s,
               COUNT(DISTINCT e.page) as pages_visited
        FROM sessions s
        LEFT JOIN events e ON e.session_id=s.session_id
        WHERE s.frustrated IS NOT NULL
        GROUP BY s.session_id
        ORDER BY s.last_ts DESC
    """)
    conn.close()
    if not rows:
        return "No data yet", 204
    headers = list(rows[0].keys())
    lines = [",".join(headers)]
    for r in rows:
        lines.append(",".join(str(r.get(h) or 0) for h in headers))
    from flask import Response
    return Response(
        "\n".join(lines),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=sessions.csv"}
    )

# ── Pages ─────────────────────────────────────────────────────────
@app.route("/")
def index():      return render_template("site/index.html")

@app.route("/shop")
def shop():       return render_template("site/shop.html")

@app.route("/contact")
def contact():    return render_template("site/contact.html")

@app.route("/checkout")
def checkout():   return render_template("site/checkout.html")

@app.route("/dashboard")
def dashboard():  return render_template("dashboard.html")

# ── Init ──────────────────────────────────────────────────────────
init_db()

if __name__ == "__main__":
    print("\n  Rage Click Detector")
    print("  Site      -> http://localhost:5000")
    print("  Dashboard -> http://localhost:5000/dashboard")
    print("  Export    -> http://localhost:5000/api/export\n")
    app.run(debug=False, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))