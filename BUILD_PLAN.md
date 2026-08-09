# aeroQ — Build Plan

Airport security wait-time predictions derived from departure schedule density.

Derived from `project_plan.md`, with decisions resolved and spec ambiguities settled.

---

## 1. Decisions locked

| Decision | Choice | Rationale |
|---|---|---|
| Backend layout | Structured package under `backend/app/` | Testable; provider is swappable |
| Data source | **Mock-first.** `MockProvider` is the default; `AirLabsProvider` is written and wired but only activates when `AIRLABS_API_KEY` is set | Zero API calls consumed during the entire build |
| Host | The Fedora GPD Pocket itself — develop and run in place | No artifact shipping, no deploy pipeline |
| Public access | **Tailscale Funnel** | Stable HTTPS URL, no domain, no port forwarding, works behind CGNAT |
| Quota protection | Monthly call cap + **airport allowlist** | Allowlist makes the cap unreachable by construction (see §7) |
| PythonAnywhere | Dropped | Redundant when self-hosting; app stays portable ASGI regardless |
| Extras | Pytest suite, README + `.env.example`, estimated wait in minutes | All selected |

Explicitly **not** building: per-IP rate limiting, passphrase gate, daily call ceiling. The
allowlist bound in §7 covers the drain vector these would have addressed.

---

## 2. Architecture

```
friend's phone
      │  https://<host>.<tailnet>.ts.net
      ▼
Tailscale edge  (TLS termination)
      │  outbound-initiated tunnel — no inbound ports open
      ▼
GPD Pocket ── tailscaled (systemd)
           └─ uvicorn :8000 (systemd)
                 ├─ /            → React dist/ (static)
                 ├─ /api/*       → FastAPI
                 └─ aeroq.db     → SQLite (WAL)
                        │  on cache miss / staleness
                        ▼
                  AirLabs /schedules
```

Single origin: FastAPI serves both the API and the built frontend, so there is one port to
tunnel and no CORS in production.

---

## 3. Repository structure

```
aeroQ/
├── backend/
│   ├── app/
│   │   ├── __init__.py
│   │   ├── main.py          # FastAPI app, routes, static mount, lifespan
│   │   ├── config.py        # env vars + all tunable constants
│   │   ├── db.py            # connection, schema init, WAL pragma
│   │   ├── models.py        # pydantic request/response schemas
│   │   ├── providers.py     # ScheduleProvider ABC, AirLabsProvider, MockProvider
│   │   ├── cache.py         # get_or_refresh, quota guard, per-airport locks
│   │   └── predict.py       # rush window filter + prediction math
│   ├── tests/
│   │   ├── conftest.py      # temp-db fixture, fake provider
│   │   ├── test_cache.py
│   │   ├── test_predict.py
│   │   └── test_api.py
│   └── requirements.txt
├── frontend/
│   ├── src/
│   │   ├── main.jsx
│   │   ├── App.jsx
│   │   ├── api.js
│   │   ├── App.css
│   │   └── components/
│   │       ├── PredictForm.jsx
│   │       └── ResultCard.jsx
│   ├── index.html
│   ├── vite.config.js       # dev proxy /api → :8000
│   └── package.json
├── deploy/
│   ├── aeroq.service        # systemd unit
│   └── rebuild.sh           # npm build + restart service
├── Dockerfile               # optional alternate path
├── docker-compose.yml
├── .env.example
├── .gitignore
├── README.md
└── BUILD_PLAN.md            # this file
```

---

## 4. Database schema

SQLite in WAL mode, short-lived connections, path from `DB_PATH`.

```sql
CREATE TABLE airport_schedule_cache (
    iata          TEXT PRIMARY KEY,
    fetched_at    INTEGER NOT NULL,   -- unix seconds
    flight_count  INTEGER NOT NULL,
    window_start  TEXT,               -- earliest dep_time in payload (coverage bound)
    window_end    TEXT,               -- latest dep_time in payload
    raw_payload   TEXT NOT NULL       -- provider response, for debugging
);

CREATE TABLE flights (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    iata              TEXT NOT NULL,
    flight_iata       TEXT,
    airline_iata      TEXT,
    dep_terminal      TEXT,           -- as provided
    dep_terminal_norm TEXT,           -- normalized: uppercase, leading 'T' stripped
    dep_time_local    TEXT NOT NULL,  -- 'YYYY-MM-DD HH:MM'
    dep_time_utc      TEXT,
    status            TEXT,
    aircraft_icao     TEXT
);
CREATE INDEX idx_flights_lookup ON flights(iata, dep_time_local);

CREATE TABLE api_usage (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    provider   TEXT NOT NULL,
    endpoint   TEXT NOT NULL,
    called_at  INTEGER NOT NULL,
    month_key  TEXT NOT NULL,         -- 'YYYY-MM'
    status     TEXT NOT NULL          -- ok | error | blocked
);
CREATE INDEX idx_usage_month ON api_usage(month_key);
```

`flights` rows for an airport are deleted and re-inserted inside a single transaction on
each refresh, so the table never holds a mix of two fetches.

---

## 5. Cache layer (`cache.py`)

`async get_or_refresh(iata) -> CacheResult`:

1. Reject `iata` not in `ALLOWED_AIRPORTS` → `403`.
2. Read `airport_schedule_cache`. Fresh (`now - fetched_at < CACHE_TTL_HOURS`) → serve,
   `data_source="cache"`.
3. Stale or missing → acquire the per-airport `asyncio.Lock`.
4. **Re-check freshness after acquiring** — a concurrent request may have just refreshed it.
   This is what makes 5 simultaneous users on one airport cost 1 API call, not 5.
5. Check the monthly cap against `api_usage`. Over cap → log `blocked`; serve stale data if
   any exists (`data_source="stale"`), otherwise `503` with a clear message.
6. Fetch from the provider. On success: replace rows transactionally, log `ok`.
   On failure: log `error`; serve stale if present, else `502`.

Every response carries `data_source` (`cache` | `fresh` | `stale`) and `cache_age_minutes`,
so the UI can be honest about what the user is looking at.

---

## 6. Prediction math (`predict.py`)

### Constants — all in `config.py`, all env-overridable

```python
SEATS_PER_FLIGHT      = 150    # average aircraft seats
ORIGIN_PAX_FACTOR     = 0.75   # 25% are connecting, skip origin security
RUSH_WINDOW_HOURS     = 2
SECURITY_LANES        = 5
PAX_PER_LANE_PER_HOUR = 150    # → 750/hour total capacity
LIGHT_MAX_RATIO       = 0.6
MODERATE_MAX_RATIO    = 1.0
BASE_WAIT_MIN         = 5
SATURATED_WAIT_MIN    = 25     # wait at exactly 100% utilization
MAX_WAIT_MIN          = 120    # clamp
GATE_BUFFER_MIN       = 45     # check-in, bag drop, walk to gate
```

### Steps

1. **Rush window** — `[flight_time - 2h, flight_time)`, filtered to the user's normalized
   terminal at the given airport.
2. **Estimated passengers** — `flights_in_window × 150 × 0.75`.
3. **Demand rate** — `estimated_passengers ÷ RUSH_WINDOW_HOURS`.

   > **Spec ambiguity, resolved.** `project_plan.md` says to compare estimated passengers
   > against "750/hour capacity", but passengers accumulate over a *2-hour* window.
   > Comparing a 2-hour total against a 1-hour capacity would double every result. We
   > convert demand to a per-hour rate before comparing. The raw 2-hour total is still
   > returned as `estimated_passengers`, exactly as the spec requires.

4. **Load ratio** — `demand_per_hour ÷ 750`.
5. **Category** — `< 0.6` Light · `0.6–1.0` Moderate · `> 1.0` Severe.
6. **Estimated wait** —
   - `ratio ≤ 1`: `BASE + (SATURATED - BASE) × ratio` → 5 min idle, 25 min at saturation.
   - `ratio > 1`: demand exceeds throughput, so a backlog forms.
     `backlog = estimated_passengers - (750 × 2)`;
     `wait = SATURATED + backlog ÷ (750/60)`. Clamped to `MAX_WAIT_MIN`.
7. **Recommended arrival** — `flight_time - estimated_wait - GATE_BUFFER_MIN`.

### Worked example — SFO Terminal 2, 14:30 departure

```
window                12:30 – 14:30
flights in window     18
estimated_passengers  18 × 150 × 0.75      = 2025
demand_per_hour       2025 ÷ 2             = 1012
load_ratio            1012 ÷ 750           = 1.35   → Severe
backlog               2025 − 1500          = 525
estimated_wait        25 + (525 ÷ 12.5)    = 67 min
recommended_arrival   14:30 − 67 − 45      = 12:38
```

### Honesty constraints

- This is a **heuristic**, not a model fitted to observed wait data. Every response returns
  its `assumptions` block, and the UI exposes it under "how this was calculated".
- **Terminal fallback.** AirLabs `dep_terminal` is frequently null or inconsistent
  (`"1"` vs `"T1"` vs `"A"`). Normalization handles format drift; when a terminal still
  yields zero flights we fall back to the whole airport and return
  `terminal_matched: false`, which the UI shows as a warning. Reporting a confidently empty
  airport would be worse than admitting the fallback.
- **Coverage bound.** AirLabs `/schedules` returns a near-term rolling window, not arbitrary
  future dates. If the requested flight time falls outside the cached payload's
  `window_start`/`window_end`, return `422 out_of_range` rather than a silently wrong
  "Light". The mock provider generates data for any date, so development isn't blocked.

---

## 7. Quota budget

The allowlist converts the monthly cap from a guard into a guarantee:

```
max calls/month = (#allowlisted airports) × (24 ÷ TTL_hours) × 30
                = N × 180        at TTL = 4h
```

| Airports | TTL | Worst-case calls/month |
|---|---|---|
| 5 | 4h | 900 |
| 5 | 6h | 600 |
| 8 | 6h | 960 |

**Ship with ≤ 5 airports at a 4h TTL.** Under that configuration, no volume of traffic
from friends can exceed the free tier — the ceiling is set by cache expiry, not by request
count. To add airports, raise `CACHE_TTL_HOURS` first and re-check the formula. The
`MONTHLY_CALL_CAP` (default 900) remains as a backstop against misconfiguration.

---

## 8. API contract

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/health` | Liveness + provider mode |
| `GET` | `/api/airports` | Allowlisted IATA codes — populates the form dropdown |
| `GET` | `/api/predict` | The prediction. Params: `airport`, `terminal`, `flight_time` |
| `GET` | `/api/quota` | Calls used this month, cap, remaining |
| `GET` | `/api/schedule/{iata}` | Debug: cached flights + cache age |

`GET /api/predict?airport=SFO&terminal=2&flight_time=2026-08-10T14:30`

```json
{
  "airport": "SFO",
  "terminal": "2",
  "terminal_matched": true,
  "flight_time_local": "2026-08-10T14:30",
  "rush_window": { "start": "2026-08-10T12:30", "end": "2026-08-10T14:30" },
  "flights_in_window": 18,
  "estimated_passengers": 2025,
  "demand_per_hour": 1012,
  "capacity_per_hour": 750,
  "load_ratio": 1.35,
  "wait_category": "Severe",
  "estimated_wait_minutes": 67,
  "recommended_arrival_local": "2026-08-10T12:38",
  "data_source": "cache",
  "cache_age_minutes": 37,
  "assumptions": {
    "seats_per_flight": 150,
    "origin_passenger_factor": 0.75,
    "security_lanes": 5,
    "passengers_per_lane_per_hour": 150,
    "rush_window_hours": 2
  }
}
```

Errors use a consistent envelope: `{"error": {"code": "out_of_range", "message": "..."}}`
with codes `airport_not_allowed`, `out_of_range`, `quota_exhausted`, `provider_error`.

---

## 9. Frontend

Single page, mobile-first — friends will open this on a phone at 5am.

- **PredictForm** — airport `<select>` populated from `/api/airports` (invalid codes become
  unrepresentable), terminal text input, `datetime-local` for flight time. Submit disabled
  while in flight.
- **ResultCard** — category as the hero element with a color ramp (Light green / Moderate
  amber / Severe red), then recommended arrival time as the second-most prominent value,
  since that is the actually actionable number. Passenger estimate and flights-in-window
  below. A collapsible "how this was calculated" renders the `assumptions` block and the
  arithmetic.
- **Status strip** — cache age and `data_source`; a visible warning banner when
  `terminal_matched` is false or `data_source` is `stale`.
- Dev: Vite proxies `/api` → `localhost:8000`. Prod: `npm run build` → FastAPI serves
  `dist/`.

---

## 10. Deployment on the GPD Pocket

**Run in place.** `deploy/aeroq.service`:

```ini
[Unit]
Description=aeroQ
After=network-online.target

[Service]
User=jsin
WorkingDirectory=/home/jsin/dev/aeroQ/backend
EnvironmentFile=/home/jsin/dev/aeroQ/.env
ExecStart=/home/jsin/dev/aeroQ/backend/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
Restart=always

[Install]
WantedBy=multi-user.target
```

Bound to `127.0.0.1` — Tailscale is the only path in.

**Public URL:**

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
sudo tailscale funnel --bg 8000
```

Prints `https://<hostname>.<tailnet>.ts.net`. First run also prints a console link to enable
HTTPS certs and Funnel — one click each. `--bg` persists across reboots.

**Two operational notes for a GPD Pocket:**

- It is a handheld with a lid. `sudo systemctl mask sleep.target suspend.target` (or set
  `HandleLidSwitch=ignore` in `logind.conf`) or the URL goes dark when it's closed.
- Funnel URLs are fully public. Anyone with the link reaches the app; no Tailscale account
  needed on their end. That is the intent here, but worth being deliberate about.

`Dockerfile` and `docker-compose.yml` ship as an alternate path (multi-stage: node builds
`dist/`, python serves it) but are not the primary route.

---

## 11. Build phases

Each phase ends in a working state and a commit.

| # | Phase | Deliverable | Verification |
|---|---|---|---|
| 1 | Scaffold | Repo layout, `config.py`, `db.py`, `requirements.txt`, `.env.example`, `.gitignore` | Schema initializes; `python -c "from app import db; db.init()"` |
| 2 | Providers | `ScheduleProvider` ABC, `MockProvider` with seeded realistic schedules, `AirLabsProvider` | Mock returns a plausible day of departures for any date |
| 3 | Cache | `get_or_refresh`, quota guard, per-airport locks, `api_usage` logging | `test_cache.py`: hit / stale / miss / concurrent / over-cap / provider-error paths |
| 4 | Prediction | Rush window filter, math, terminal normalization + fallback | `test_predict.py`: worked example above, boundary ratios, empty window, terminal fallback |
| 5 | API | Routes, pydantic models, error envelope, static mount | `test_api.py` via `TestClient`; manual curl of all five endpoints |
| 6 | Frontend | Vite app, form, result card, status strip | Runs against the mock provider end to end in a browser |
| 7 | Deploy | systemd unit, `rebuild.sh`, Dockerfile, README | Service survives a reboot; Funnel URL loads from a phone on cellular |

Phases 3 and 4 are independent and can be built in either order.

---

## 12. Open risks

| Risk | Impact | Mitigation |
|---|---|---|
| AirLabs `/schedules` coverage narrower than hoped | Predictions unavailable for flights >24h out | `422 out_of_range` with an explicit message; validate against the live API in phase 7 |
| `dep_terminal` mostly null at real airports | Terminal filtering degrades to whole-airport | Normalization + `terminal_matched: false`; if it's null *everywhere*, reconsider whether terminal input is worth keeping |
| GPD sleeps or drops off Wi-Fi | URL dies silently | Mask sleep targets; `Restart=always`; both tailscaled and uvicorn are systemd-managed |
| Heuristic is simply wrong vs. real waits | Friends miss flights | Every response ships its assumptions; UI frames output as an estimate, never a guarantee; recommended arrival includes a 45-min buffer |
| Live response shape differs from the mock | Phase 7 surprises | `AirLabsProvider` is written in phase 2 against documented fields; first live call is a deliberate, single, inspected request |
