from flask import Flask, request, jsonify, render_template, Response
from flask_cors import CORS
from collections import defaultdict
from threading import Lock
import uuid, time, os

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

# ── Pure in-memory store ──────────────────────────────────────────
# No database. Everything lives in these two dicts.
# Resets on each Render restart — fine for a demo.
_lock    = Lock()
_events  = defaultdict(list)   # session_id -> [event, ...]
_sessions = {}                 # session_id -> session metadata

def _analyze(session_id):
    """Run frustration analysis and update session metadata in place."""
    clicks = [e for e in _events[session_id] if e["event_type"] == "click"]

    # Rage burst: 3+ clicks within 1500ms inside 60x60px area
    rage_bursts = 0
    i = 0
    while i < len(clicks):
        cluster = [clicks[i]]
        for j in range(i + 1, len(clicks)):
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
    pages   = list(dict.fromkeys(e["page"] for e in _events[session_id]))
    u_turns = sum(1 for k in range(1, len(pages) - 1) if pages[k] == pages[k - 2])

    score      = min(1.0, rage_bursts * 0.4 + dead_clicks * 0.05 + u_turns * 0.2)
    frustrated = rage_bursts >= 1 or dead_clicks >= 3

    _sessions[session_id].update({
        "rage_bursts":       rage_bursts,
        "dead_clicks":       dead_clicks,
        "u_turns":           u_turns,
        "frustration_score": round(score, 2),
        "frustrated":        frustrated,
        "total_clicks":      len(clicks),
        "pages_visited":     len(set(pages)),
    })
    return _sessions[session_id]

# ── Keep-alive ────────────────────────────────────────────────────
@app.route("/ping")
def ping():
    with _lock:
        n_sess = len(_sessions)
        n_ev   = sum(len(v) for v in _events.values())
    return jsonify({"status": "alive", "sessions": n_sess, "events": n_ev})

# ── Debug ─────────────────────────────────────────────────────────
@app.route("/debug")
def debug():
    with _lock:
        n_ev   = sum(len(v) for v in _events.values())
        recent = []
        for sid, evs in _events.items():
            for e in evs[-3:]:
                recent.append(e)
        recent = sorted(recent, key=lambda x: x["ts"], reverse=True)[:10]
    return jsonify({
        "ok":             True,
        "db_mode":        "in-memory (no database)",
        "total_events":   n_ev,
        "total_sessions": len(_sessions),
        "last_10_events": recent
    })

# ── Event ingestion ───────────────────────────────────────────────
@app.route("/event", methods=["POST"])
def ingest_event():
    d   = request.json or {}
    sid = d.get("session_id", str(uuid.uuid4()))
    ts  = d.get("ts", time.time())

    event = {
        "session_id":  sid,
        "event_type":  d.get("event_type", "click"),
        "x":           d.get("x", 0),
        "y":           d.get("y", 0),
        "ts":          ts,
        "element":     d.get("element", ""),
        "page":        d.get("page", "/"),
        "dom_changed": int(d.get("dom_changed", 0)),
        "scroll_y":    d.get("scroll_y", 0),
    }

    with _lock:
        _events[sid].append(event)
        if sid not in _sessions:
            _sessions[sid] = {
                "session_id":       sid,
                "start_ts":         ts,
                "last_ts":          ts,
                "frustrated":       False,
                "frustration_score": 0.0,
                "rage_bursts":      0,
                "dead_clicks":      0,
                "u_turns":          0,
                "total_clicks":     0,
                "pages_visited":    0,
            }
        else:
            _sessions[sid]["last_ts"] = ts

        # Re-analyze on every click so dashboard stays live
        _analyze(sid)

    return jsonify({"ok": True})

# ── Analytics API ─────────────────────────────────────────────────
@app.route("/api/clicks")
def get_clicks():
    page = request.args.get("page", "/")
    with _lock:
        rows = [
            {"x": e["x"], "y": e["y"], "dom_changed": e["dom_changed"]}
            for evs in _events.values()
            for e in evs
            if e["event_type"] == "click" and e["page"] == page
        ]
    return jsonify(rows)

@app.route("/api/sessions")
def get_sessions():
    with _lock:
        rows = sorted(_sessions.values(), key=lambda s: s["last_ts"], reverse=True)
        result = []
        for s in rows:
            r = dict(s)
            r["duration_s"] = round(s["last_ts"] - s["start_ts"], 1)
            result.append(r)
    return jsonify(result)

@app.route("/api/stats")
def get_stats():
    with _lock:
        total      = len(_sessions)
        frustrated = sum(1 for s in _sessions.values() if s["frustrated"])
        total_cl   = sum(len(v) for v in _events.values())
        dead_cl    = sum(
            1 for evs in _events.values()
            for e in evs if e["event_type"] == "click" and not e["dom_changed"]
        )
        # Most clicked unresponsive element
        elem_counts = defaultdict(int)
        for evs in _events.values():
            for e in evs:
                if e["event_type"] == "click" and not e["dom_changed"] and e["element"]:
                    elem_counts[e["element"]] += 1
        top_el = max(elem_counts, key=elem_counts.get) if elem_counts else None

    return jsonify({
        "total_sessions":      total,
        "frustrated_sessions": frustrated,
        "frustrated_pct":      round(frustrated / total * 100, 1) if total else 0,
        "total_clicks":        total_cl,
        "dead_click_pct":      round(dead_cl / total_cl * 100, 1) if total_cl else 0,
        "top_dead_element":    {"element": top_el, "c": elem_counts[top_el]} if top_el else None
    })

@app.route("/api/analyze/<session_id>", methods=["POST"])
def analyze_session(session_id):
    with _lock:
        if session_id not in _sessions:
            return jsonify({"error": "session not found"}), 404
        result = _analyze(session_id)
    return jsonify(result)

@app.route("/api/export")
def export_csv():
    with _lock:
        rows = list(_sessions.values())
    if not rows:
        return "No data yet", 204
    headers = ["session_id", "frustrated", "frustration_score", "total_clicks",
               "dead_clicks", "u_turns", "rage_bursts", "pages_visited"]
    lines = [",".join(headers)]
    for r in rows:
        lines.append(",".join(str(r.get(h, 0)) for h in headers))
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

if __name__ == "__main__":
    print("\n  Rage Click Detector — in-memory mode")
    print("  Site      -> http://localhost:5000")
    print("  Dashboard -> http://localhost:5000/dashboard")
    print("  Debug     -> http://localhost:5000/debug\n")
    app.run(debug=False, host="0.0.0.0",
            port=int(os.environ.get("PORT", 5000)))