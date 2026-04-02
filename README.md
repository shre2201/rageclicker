# Rage Click & Frustration Detector

An AI-powered web analytics mini-project that tracks user frustration signals (rage clicks,
dead clicks, U-turns) on a demo e-commerce site and displays them on a real-time dashboard.

## Quick start (5 minutes)

```bash
# 1. Install dependencies
pip install flask flask-cors

# 2. Run the app
python app.py

# 3. Open the demo site
#    http://localhost:5000

# 4. Open the dashboard
#    http://localhost:5000/dashboard
```

## Project structure

```
rageclicker/
├── app.py              ← Flask backend + all API endpoints
├── static/
│   └── js/
│       └── tracker.js  ← Click tracking script (embedded in every page)
├── templates/
│   ├── dashboard.html  ← Analytics dashboard
│   └── site/
│       ├── base.html   ← Shared layout + nav
│       ├── index.html  ← Homepage (broken CTA buttons)
│       ├── shop.html   ← Shop (broken filters, out-of-stock cart)
│       ├── contact.html← Contact (infinite spinner)
│       └── checkout.html← Checkout (broken promo, 4s pay lag)
└── data/
    └── events.db       ← SQLite database (auto-created)
```

## Broken UX elements (intentional, for data generation)

| Page     | Element                     | Frustration signal |
|----------|-----------------------------|--------------------|
| Home     | "View deals" button         | Dead click         |
| Home     | Newsletter subscribe        | Infinite spinner   |
| Home     | "Click to zoom" promo cards | Dead click         |
| Shop     | Filter buttons              | No DOM change      |
| Shop     | Sort dropdown               | No DOM change      |
| Shop     | Product 3 "Add to cart"     | Silent failure     |
| Contact  | "Start a chat" link         | Dead click         |
| Contact  | Submit form button          | Infinite spinner   |
| Checkout | Promo code                  | Always invalid     |
| Checkout | Pay button                  | 4-second lag       |

## API endpoints

| Method | Endpoint                      | Description                        |
|--------|-------------------------------|------------------------------------|
| POST   | /event                        | Ingest a tracker event             |
| GET    | /api/clicks?page=/            | Get click coords for heatmap       |
| GET    | /api/sessions                 | List all sessions with metadata    |
| GET    | /api/stats                    | Aggregate stats                    |
| POST   | /api/analyze/<session_id>     | Run frustration analysis on session|
| POST   | /api/analyze_all              | Analyze all sessions               |

## Frustration detection logic

A session is labeled **frustrated = 1** if:
- `rage_burst_count >= 1`  (3+ clicks within 1500ms in 60×60px area)
- OR `dead_click_count >= 3`  (clicks with no DOM change)

Frustration score (0–1):
```
score = min(1.0, rage_bursts × 0.4 + dead_clicks × 0.05 + u_turns × 0.2)
```

## References

- [R1] arXiv:2512.20438 — "Machine Learning to Predict Digital Frustration from Clickstream Data" (2025)
- [R2] Springer UMAI — "Generalisable sensor-free frustration detection in online learning" (2024)
- [R3] IEEE Xplore — "ML Approach to Leverage Mouse Interaction Behavior" (2018)
- [R4] KISSmetrics — Rage Click Definition and Industry Standards (2024)
- [R5] Dynatrace — Automatic Rage Click Detection (2024)
