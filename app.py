from flask import Flask, request, jsonify, render_template, Response
from flask_cors import CORS
import uuid, time, os, json, sqlite3
import requests as http
from threading import Thread, Lock
from collections import deque

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

TURSO_URL   = os.environ.get("TURSO_URL", "").rstrip("/").replace("libsql://", "https://")
TURSO_TOKEN = os.environ.get("TURSO_TOKEN", "")
USE_TURSO   = bool(TURSO_URL and TURSO_TOKEN)

# ── Persistent HTTP session (connection reuse = much faster) ──────
_http = http.Session()
_http.headers.update({
    "Authorization": f"Bearer {TURSO_TOKEN}",
    "Content-Type":  "application/json"
})

# ── Write queue with lock ─────────────────────────────────────────
_queue = deque()
_lock  = Lock()
_flush_errors = []

def _turso_batch(statements):
    """Send multiple SQL statements in ONE HTTP request."""
    requests_payload = []
    for sql, params in statements:
        typed = []
        for p in params:
            if isinstance(p, int):
                typed.append({"type": "integer", "value": str(p)})
            elif isinstance(p, float):
                typed.append({"type": "float",   "value": str(p)})
            elif p is None:
                typed.append({"type": "null",    "value": None})
            else:
                typed.append({"type": "text",    "value": str(p)})
        requests_payload.append({"type": "execute", "stmt": {"sql": sql, "args": typed}})
    requests_payload.append({"type": "close"})

    resp = _http.post(f"{TURSO_URL}/v2/pipeline",
                      json={"requests": requests_payload}, timeout=10)
    resp.raise_for_status()
    return resp.json()

def _turso_single(sql, params=()):
    """Single read query — returns list of row dicts."""
    data = _turso_batch([(sql, params)])
    result = data["results"][0]
    if result.get("type") == "error":
        raise RuntimeError(result["error"]["message"])
    rs   = result.get("response", {}).get("result", {})
    cols = [c["name"] for c in rs.get("cols", [])]
    rows = []
    for row in rs.get("rows", []):
        rows.append(dict(zip(cols, [
            None              if c["type"] == "null"    else
            int(c["value"])   if c["type"] == "integer" else
            float(c["value"]) if c["type"] == "float"   else
            c["value"]
            for c in row
        ])))
    return rows

def _flush_queue():
    """Drain queue and send all pending writes in one batch."""
    with _lock:
        if not _queue:
            return
        batch = list(_queue)
        _queue.clear()
    try:
        _turso_batch(batch)
    except Exception as e:
        _flush_errors.append(str(e)[-120:])
        if len(_flush_errors) > 10:
            _flush_errors.pop(0)

def _flush_worker():
    while True:
        time.sleep(1)   # flush every second
        if USE_TURSO and _queue:
            _flush_queue()

_t = Thread(target=_flush_worker, daemon=True)
_t.start()

# ── SQLite fallback ───────────────────────────────────────────────
def _sqlite_exec(sql, params=()):
    os.makedirs("data", exist_ok=True)
    conn = sqlite3.connect("data/events.db")
    conn.row_factory = sqlite3.Row
    cur  = conn.execute(sql, params)
    rows = [dict(r) for r in (cur.fetchall() or [])]
    conn.commit()
    conn.close()
    return rows

# ── Public API ────────────────────────────────────────────────────
def db_read(sql, params=()):
    return _turso_single(sql, params) if USE_TURSO else _sqlite_exec(sql, params)

def db_one(sql, params=()):
    rows = db_read(sql, params)
    return rows[0] if rows else {}

def db_write(sql, params=()):
    """Queue write (Turso) or execute immediately (SQLite)."""
    if USE_TURSO:
        with _lock:
            _queue.append((sql, params))
    else:
        _sqlite_exec(sql, params)

def init_db():
    # init_db uses direct write so tables exist before any request
    if USE_TURSO:
        _turso_batch([
            ("CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, event_type TEXT, x INTEGER, y INTEGER, ts REAL, element TEXT, page TEXT, dom_changed INTEGER DEFAULT 0, scroll_y INTEGER DEFAULT 0)", ()),
            ("CREATE TABLE IF NOT EXISTS sessions (session_id TEXT PRIMARY KEY, start_ts REAL, last_ts REAL, frustrated INTEGER DEFAULT 0, frustration_score REAL DEFAULT 0.0)", ()),
        ])
    else:
        _sqlite_exec("CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, event_type TEXT, x INTEGER, y INTEGER, ts REAL, element TEXT, page TEXT, dom_changed INTEGER DEFAULT 0, scroll_y INTEGER DEFAULT 0)")
        _sqlite_exec("CREATE TABLE IF NOT EXISTS sessions (session_id TEXT PRIMARY KEY, start_ts REAL, last_ts REAL, frustrated INTEGER DEFAULT 0, frustration_score REAL DEFAULT 0.0)")

# ── Keep-alive ────────────────────────────────────────────────────
@app.route("/ping")
def ping():
    return jsonify({"status": "alive", "ts": time.time(),
                    "db": "turso" if USE_TURSO else "sqlite",
                    "queue_depth": len(_queue)})

# ── Debug ─────────────────────────────────────────────────────────
@app.route("/debug")
def debug():
    try:
        total_events    = db_one("SELECT COUNT(*) as c FROM events").get("c", 0)
        total_sessions  = db_one("SELECT COUNT(*) as c FROM sessions").get("c", 0)
        recent          = db_read("SELECT session_id, page, element, dom_changed, ts FROM events ORDER BY ts DESC LIMIT 10")
        return jsonify({
            "ok": True,
            "db_mode":        "turso" if USE_TURSO else "sqlite",
            "turso_url":      TURSO_URL[:45] + "..." if TURSO_URL else "not set",
            "queue_depth":    len(_queue),
            "flush_errors":   _flush_errors[-3:],
            "total_events":   total_events,
            "total_sessions": total_sessions,
            "last_10_events": recent
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# ── Event ingestion ───────────────────────────────────────────────
@app.route("/event", methods=["POST"])
def ingest_event():
    d   = request.json or {}
    sid = d.get("session_id", str(uuid.uuid4()))
    ts  = d.get("ts", time.time())
    db_write(
        "INSERT INTO events (session_id,event_type,x,y,ts,element,page,dom_changed,scroll_y) VALUES (?,?,?,?,?,?,?,?,?)",
        (sid, d.get("event_type","click"), d.get("x",0), d.get("y",0), ts,
         d.get("element",""), d.get("page","/"),
         int(d.get("dom_changed",0)), d.get("scroll_y",0))
    )
    db_write(
        "INSERT INTO sessions (session_id,start_ts,last_ts) VALUES (?,?,?) ON CONFLICT(session_id) DO UPDATE SET last_ts=excluded.last_ts",
        (sid, ts, ts)
    )
    return jsonify({"ok": True})

# ── Analytics ─────────────────────────────────────────────────────
@app.route("/api/clicks")
def get_clicks():
    page = request.args.get("page", "/")
    return jsonify(db_read("SELECT x,y,dom_changed FROM events WHERE event_type='click' AND page=?", (page,)))

@app.route("/api/sessions")
def get_sessions():
    rows = db_read("""
        SELECT s.session_id, s.start_ts, s.last_ts,
               COUNT(e.id) as total_clicks,
               SUM(CASE WHEN e.dom_changed=0 THEN 1 ELSE 0 END) as dead_clicks,
               s.frustrated, s.frustration_score
        FROM sessions s
        LEFT JOIN events e ON e.session_id=s.session_id AND e.event_type='click'
        GROUP BY s.session_id ORDER BY s.last_ts DESC LIMIT 100
    """)
    for r in rows:
        r["duration_s"]   = round((r.get("last_ts") or 0) - (r.get("start_ts") or 0), 1)
        r["dead_clicks"]  = r.get("dead_clicks") or 0
        r["total_clicks"] = r.get("total_clicks") or 0
    return jsonify(rows)

@app.route("/api/stats")
def get_stats():
    total      = db_one("SELECT COUNT(*) as c FROM sessions").get("c", 0)
    frustrated = db_one("SELECT COUNT(*) as c FROM sessions WHERE frustrated=1").get("c", 0)
    total_cl   = db_one("SELECT COUNT(*) as c FROM events WHERE event_type='click'").get("c", 0)
    dead_cl    = db_one("SELECT COUNT(*) as c FROM events WHERE event_type='click' AND dom_changed=0").get("c", 0)
    top_el     = db_one("SELECT element,COUNT(*) as c FROM events WHERE event_type='click' AND dom_changed=0 AND element!='' GROUP BY element ORDER BY c DESC LIMIT 1")
    return jsonify({
        "total_sessions":      total,
        "frustrated_sessions": frustrated,
        "frustrated_pct":      round(frustrated / total * 100, 1) if total else 0,
        "total_clicks":        total_cl,
        "dead_click_pct":      round(dead_cl / total_cl * 100, 1) if total_cl else 0,
        "top_dead_element":    top_el or None
    })

@app.route("/api/analyze/<session_id>", methods=["POST"])
def analyze_session(session_id):
    events = db_read("SELECT * FROM events WHERE session_id=? ORDER BY ts ASC", (session_id,))
    clicks = [e for e in events if e["event_type"] == "click"]

    rage_bursts = 0
    i = 0
    while i < len(clicks):
        cluster = [clicks[i]]
        for j in range(i+1, len(clicks)):
            if (clicks[j]["ts"] - clicks[i]["ts"] <= 1.5 and
                abs(clicks[j]["x"] - clicks[i]["x"]) <= 60 and
                abs(clicks[j]["y"] - clicks[i]["y"]) <= 60):
                cluster.append(clicks[j])
            else:
                break
        if len(cluster) >= 3:
            rage_bursts += 1
        i += len(cluster) if len(cluster) > 1 else 1

    dead_clicks = sum(1 for c in clicks if not c["dom_changed"])
    pages   = list(dict.fromkeys(e["page"] for e in events))
    u_turns = sum(1 for k in range(1, len(pages)-1) if pages[k] == pages[k-2])
    score   = min(1.0, rage_bursts * 0.4 + dead_clicks * 0.05 + u_turns * 0.2)
    frustrated = 1 if (rage_bursts >= 1 or dead_clicks >= 3) else 0

    db_write("UPDATE sessions SET frustrated=?,frustration_score=? WHERE session_id=?",
             (frustrated, score, session_id))
    return jsonify({"session_id": session_id, "rage_bursts": rage_bursts,
                    "dead_clicks": dead_clicks, "u_turns": u_turns,
                    "frustration_score": round(score, 2), "frustrated": bool(frustrated)})

@app.route("/api/export")
def export_csv():
    rows = db_read("""
        SELECT s.session_id, s.frustrated, s.frustration_score,
               COUNT(e.id) as total_clicks,
               SUM(CASE WHEN e.dom_changed=0 THEN 1 ELSE 0 END) as dead_clicks,
               MAX(e.scroll_y) as max_scroll,
               (s.last_ts - s.start_ts) as duration_s,
               COUNT(DISTINCT e.page) as pages_visited
        FROM sessions s
        LEFT JOIN events e ON e.session_id=s.session_id
        WHERE s.frustrated IS NOT NULL
        GROUP BY s.session_id ORDER BY s.last_ts DESC
    """)
    if not rows:
        return "No data yet", 204
    headers = list(rows[0].keys())
    lines   = [",".join(headers)] + [",".join(str(r.get(h) or 0) for h in headers) for r in rows]
    return Response("\n".join(lines), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=sessions.csv"})

# ── Pages ─────────────────────────────────────────────────────────
@app.route("/")
def index():     return render_template("site/index.html")
@app.route("/shop")
def shop():      return render_template("site/shop.html")
@app.route("/contact")
def contact():   return render_template("site/contact.html")
@app.route("/checkout")
def checkout():  return render_template("site/checkout.html")
@app.route("/dashboard")
def dashboard(): return render_template("dashboard.html")

# ── Init ──────────────────────────────────────────────────────────
init_db()

if __name__ == "__main__":
    print(f"\n  DB: {'turso @ ' + TURSO_URL[:40] if USE_TURSO else 'sqlite (local)'}")
    print("  Site      -> http://localhost:5000")
    print("  Dashboard -> http://localhost:5000/dashboard")
    print("  Debug     -> http://localhost:5000/debug\n")
    app.run(debug=False, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))