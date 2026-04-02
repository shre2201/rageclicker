(function () {
  const ENDPOINT = "http://localhost:5000/event";
  const PAGE = window.location.pathname;

  // Session ID persisted for tab lifetime
  let SESSION_ID = sessionStorage.getItem("rc_session");
  if (!SESSION_ID) {
    SESSION_ID = "sess_" + Math.random().toString(36).slice(2, 10) + "_" + Date.now();
    sessionStorage.setItem("rc_session", SESSION_ID);
  }

  function send(payload) {
    const body = Object.assign({ session_id: SESSION_ID, page: PAGE, ts: Date.now() / 1000 }, payload);
    fetch(ENDPOINT, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      keepalive: true
    }).catch(() => {});
  }

  // ── DOM change detection via MutationObserver ─────────────────
  let domChangedRecently = false;
  let domTimer = null;
  const observer = new MutationObserver(() => {
    domChangedRecently = true;
    clearTimeout(domTimer);
    domTimer = setTimeout(() => { domChangedRecently = false; }, 350);
  });
  observer.observe(document.body, { childList: true, subtree: true, attributes: true });

  // ── Click handler ─────────────────────────────────────────────
  document.addEventListener("click", function (e) {
    const target = e.target;
    const tag = target.tagName.toLowerCase();
    const id = target.id ? "#" + target.id : "";
    const cls = target.className && typeof target.className === "string"
      ? "." + target.className.trim().split(/\s+/).join(".") : "";
    const element = tag + (id || cls || "").slice(0, 60);

    // Small delay so MutationObserver has a chance to fire first
    setTimeout(() => {
      send({
        event_type: "click",
        x: Math.round(e.clientX),
        y: Math.round(e.clientY),
        element: element,
        dom_changed: domChangedRecently ? 1 : 0,
        scroll_y: Math.round(window.scrollY)
      });
      domChangedRecently = false;
    }, 80);
  }, true);

  // ── Rage click visual indicator (for demo purposes) ───────────
  const clickTimes = [];
  const clickPositions = [];
  document.addEventListener("click", function (e) {
    const now = Date.now();
    clickTimes.push(now);
    clickPositions.push({ x: e.clientX, y: e.clientY });

    // Keep only last 2 seconds
    while (clickTimes.length && now - clickTimes[0] > 1500) {
      clickTimes.shift();
      clickPositions.shift();
    }

    // Check rage burst: 3+ clicks in proximity
    if (clickTimes.length >= 3) {
      const recentPos = clickPositions.slice(-3);
      const spread = recentPos.reduce((acc, p) => {
        return acc + Math.abs(p.x - e.clientX) + Math.abs(p.y - e.clientY);
      }, 0);
      if (spread < 180) {
        showRageBadge(e.clientX, e.clientY);
      }
    }
  });

  function showRageBadge(x, y) {
    const badge = document.createElement("div");
    badge.textContent = "RAGE CLICK";
    badge.style.cssText = `
      position:fixed; left:${x}px; top:${y - 40}px;
      transform:translateX(-50%);
      background:#ef4444; color:#fff;
      font:700 11px/1 monospace; letter-spacing:.08em;
      padding:5px 10px; border-radius:4px;
      pointer-events:none; z-index:99999;
      animation: rcFade 1.2s forwards;
    `;
    if (!document.getElementById("rc-style")) {
      const s = document.createElement("style");
      s.id = "rc-style";
      s.textContent = `@keyframes rcFade { 0%{opacity:1;transform:translateX(-50%) translateY(0)} 100%{opacity:0;transform:translateX(-50%) translateY(-20px)} }`;
      document.head.appendChild(s);
    }
    document.body.appendChild(badge);
    setTimeout(() => badge.remove(), 1200);
  }

  // ── Auto-analyze session on page unload ───────────────────────
  window.addEventListener("beforeunload", () => {
    fetch(`http://localhost:5000/api/analyze/${SESSION_ID}`, {
      method: "POST", keepalive: true
    }).catch(() => {});
  });

  console.log("[RageTracker] Session:", SESSION_ID);
})();
