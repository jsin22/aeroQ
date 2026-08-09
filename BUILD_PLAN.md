# aeroQ — Build Plan (v2)

Airport security wait-time predictions derived from departure schedule density.

Derived from `project_plan.md`. **v2** replaces the single-provider, airport-allowlist design
with a multi-provider failover architecture and flight-number-first input.

> **Changes from v1:** multi-provider abstraction with pre-emptive budget routing (§4) ·
> flight-number input via two-step resolution, both steps cached (§5) · airport allowlist
> dropped in favor of metered cache-misses (§8) · self-building historical corpus for
> far-future flights and terminal inference (§7) · **capacity now scales with fallback
> scope** — a v1 bug that would have reported "Severe" for every whole-airport fallback (§6).

---

## 1. Decisions locked

| Decision | Choice | Rationale |
|---|---|---|
| Backend layout | Structured package under `backend/app/` | Testable; provider is swappable |
| Providers | **AeroDataBox primary, AirLabs secondary**, pooled budgets with failover | Pooled free tiers; see §3 for the selection metric |
| Development | **Mock-first.** `MockProvider` is default; real providers activate only when their key is set | Zero API calls consumed during the build |
| Primary input | **Flight number + date**, with manual airport/terminal as fallback | Users know their flight number, not their terminal |
| Host | The Fedora GPD Pocket itself — develop and run in place | No artifact shipping |
| Public access | **Tailscale Funnel** | Stable HTTPS URL, no domain, no port forwarding, works behind CGNAT |
| Quota protection | Metered cache-misses + self-balancing daily allowance | Open airport input without an allowlist (§8) |
| PythonAnywhere | Dropped | Redundant when self-hosting; app stays portable ASGI |
| Extras | Pytest suite, README + `.env.example`, estimated wait in minutes | All selected |

**Open decisions** — flagged in §12, do not block phases 1–4.

---

## 2. Architecture

```
friend's phone
      │  https://<host>.<tailnet>.ts.net
      ▼
Tailscale edge (TLS)
      │  outbound-initiated tunnel — no inbound ports open
      ▼
GPD Pocket ── tailscaled (systemd)
           └─ uvicorn :8000 (systemd)
                 ├─ /        → React dist/
                 └─ /api/*   → FastAPI
                        │
                        ▼
                   resolver.py  ── step 1: flight# → airport + terminal
                        │              (cached, dynamic TTL)
                        ▼
                   cache.py     ── step 2: airport → departure board
                        │              (cached, 4h TTL)
                        ▼
                   router.py    ── pre-emptive budget routing
                        ├─ AeroDataBoxProvider
                        ├─ AirLabsProvider
                        └─ MockProvider
                        │
                        ▼
                   SQLite (WAL) ── cache + usage ledger + history corpus
```

---

## 3. Provider selection metric

Raw monthly call count is the wrong comparison unit. The correct one is **complete airport
pictures per month**:

```
pictures/month = monthly_call_budget ÷ calls_per_complete_picture
```

| Provider | Endpoint shape | Calls per picture | Notes |
|---|---|---|---|
| AeroDataBox | `/flights/airports/icao/{icao}/{fromLocal}/{toLocal}` — **time-windowed**, up to a 12h block | **1**, regardless of flight count | Whole picture per credit |
| AirLabs | `/schedules` — **count-paginated** via `offset` | `ceil(flights ÷ page_size)` — 6+ at a large hub | Small airports still cost 1 |

A time-windowed endpoint returns the complete picture for one credit whether the airport has
10 departures or 800. A count-paginated one charges proportionally to airport size — exactly
the wrong scaling for density calculation, since large hubs are both the most expensive to
fetch and the most interesting to predict.

**This drives provider ordering, not provider exclusivity.** AirLabs remains valuable as
failover and is competitive at small airports (1 page = 1 call = 1 picture). §4's router can
later route by expected airport size; not in v1 scope.

> ### ⚠ Verify before locking primary (see §12)
> The exact free-tier numbers determine the ordering and must be confirmed against current
> provider docs — treat any figure quoted from memory as unreliable:
> 1. **Monthly quota** for each provider's free plan.
> 2. **AirLabs page size** — the `limit` default and maximum, which sets `calls_per_picture`.
> 3. **Whether the AeroDataBox FIDS/departures endpoint is included in the free plan**, and
>    whether it is metered at a higher weight than other endpoints.
> 4. **Overage behavior — the one that can actually cost money.** Confirm exceeding the free
>    tier returns `429` and hard-fails rather than auto-billing. AeroDataBox is distributed
>    via RapidAPI, whose plans vary on this. If overage bills, set a hard local cap well
>    under the tier limit and treat it as non-negotiable.
>
> The architecture is written so provider ordering is a config change (`PROVIDER_ORDER`),
> not a rewrite. Nothing in phases 1–4 depends on the answer.

---

## 4. Provider abstraction & failover (`providers.py`, `router.py`)

### Interface

```python
class ScheduleProvider(ABC):
    name: str
    supports_flight_lookup: bool        # capability declaration — the router
    supports_airport_departures: bool   # never attempts an unsupported call

    async def resolve_flight(self, flight_no, date) -> list[FlightResolution]
    async def fetch_departures(self, iata, window_start, window_end) -> list[NormalizedFlight]
```

Every provider returns the same internal `NormalizedFlight` dataclass. Each provider owns
its own mapper; nothing provider-shaped reaches the database.

### Routing: pre-emptive, not reactive

**Do not fail over on `429` alone.** Two reasons:

1. By the time a `429` arrives, the call is already spent — reactive failover wastes one
   call per request for the remainder of the month.
2. A transient `500` or timeout is indistinguishable from quota exhaustion if you only
   watch for failure. Marking a provider dead on a transient blip needlessly burns the
   pooled budget.

Instead the router consults the local `api_usage` ledger *before* dispatching and picks the
first provider in `PROVIDER_ORDER` with remaining budget. `429` remains a backstop that
corrects local drift.

**Quota-exhausted and transient-error are distinct states:**

| State | Trigger | Duration | Effect |
|---|---|---|---|
| `exhausted` | `429`, or local ledger at cap | **Sticky until month rollover** | Skipped by router entirely |
| `degraded` | `5xx`, timeout, connection error | Exponential backoff, 5 min max | Retried after backoff |
| `healthy` | — | — | Eligible |

Provider state lives in `provider_state`, so it survives a restart — otherwise a crash loop
re-probes exhausted providers and burns the pooled budget.

### Terminal normalization

A **shared** normalizer, not per-provider — the mapping is a property of aviation, not of
any vendor:

```
"T1" · "Terminal 1" · "1" · "t1"     → "1"
"Terminal A" · "A" · "Concourse A"    → "A"
"" · null · "-" · "N/A"               → None
```

Tested against a fixture matrix of real observed strings from every provider.

> **Normalization cannot fix semantic mismatch.** If one provider reports *terminal* where
> another reports *concourse* (ATL, DFW), the strings normalize cleanly but mean different
> things. Mitigation: `flights.source_provider` is stored per row, and **a single cached
> airport picture is always from exactly one provider** — never stitched across providers.
> Mixing would silently corrupt the terminal filter.

---

## 5. Two-step resolution flow

### Step 1 — Flight resolution (`resolver.py`)

`"UA 123"` + date → `{airport: "SFO", terminal: "2", scheduled_departure: "14:30"}`

**This step is cached.** In the naive design it costs one call on *every* query — five
friends on the same flight, or one friend checking five times, is five calls even when the
airport board is a perfect cache hit. Caching resolutions roughly halves total spend.

**Dynamic TTL.** The airport assignment never changes; the terminal cannot exist until
~48–72h before departure. So do not repeatedly ask a question the API definitionally cannot
answer yet:

```python
if terminal is not None:
    recheck_after = departure - 6h          # terminals do get reassigned late
elif departure - now > 72h:
    recheck_after = departure - 48h         # the earliest terminals appear
else:
    recheck_after = now + 6h                # inside the window, poll gently
```

This converts the "terminal is null far out" edge case from a problem into a scheduling
rule, and costs zero calls in the dead zone.

**Flight number parsing.** Accept `UA123`, `UA 123`, `ua-123`. Reject anything not matching
`^[A-Z]{2,3}\s*-?\s*\d{1,4}$` before it reaches a provider — malformed input must never
spend a call.

**Ambiguity is real and must be handled:**
- One flight number can cover **multiple legs** on the same date (UA123 SFO→ORD→EWR).
  Resolution returns a *list*; more than one departure → return `300 multiple_matches` with
  the options and let the UI disambiguate. Silently picking the first is wrong.
- **Codeshares.** A ticket may say LH7823 while the operating flight is UA123. Providers
  vary on whether they resolve marketing numbers. If resolution fails, fall through to
  manual entry rather than dead-ending.

### Step 2 — Density lookup (`cache.py`)

Now holding `(airport, terminal)`, check `airport_schedule_cache`:

- **Fresh (<4h)** → serve, **zero API calls**.
- **Stale / missing** → acquire per-airport `asyncio.Lock`, re-check after acquiring (5
  concurrent users on SFO = 1 call, not 5), check budget, fetch a 12h block via the router,
  replace rows transactionally, record usage.
- **Budget exhausted** → serve stale if present (`data_source: "stale"`), else fall through
  to the historical baseline (§7), else a clear error.

### Cost per query

| Scenario | Step 1 | Step 2 | Total |
|---|---|---|---|
| Both cached | 0 | 0 | **0** |
| Flight cached, airport stale | 0 | 1 | **1** |
| Fully novel flight + airport | 1 | 1 | **2** |
| Second person on the same flight | 0 | 0 | **0** |

Worst case is 2 calls per genuinely novel query; steady-state among a friend group
converging on the same airports is near zero.

---

## 6. Prediction math (`predict.py`)

### Constants — all in `config.py`, env-overridable

```python
SEATS_PER_FLIGHT      = 150
ORIGIN_PAX_FACTOR     = 0.75
RUSH_WINDOW_HOURS     = 2
LANES_PER_TERMINAL    = 5
PAX_PER_LANE_PER_HOUR = 150      # → 750/hour per terminal
LIGHT_MAX_RATIO       = 0.6
MODERATE_MAX_RATIO    = 1.0
BASE_WAIT_MIN         = 5
SATURATED_WAIT_MIN    = 25
MAX_WAIT_MIN          = 120
GATE_BUFFER_MIN       = 45
```

### Steps

1. **Rush window** — `[departure - 2h, departure)`, filtered to the normalized terminal.
2. **Estimated passengers** — `flights_in_window × 150 × 0.75`.
3. **Demand rate** — `estimated_passengers ÷ RUSH_WINDOW_HOURS`.

   > **Spec ambiguity, resolved.** `project_plan.md` compares a *2-hour* passenger total to
   > a *1-hour* capacity, which would double every result. We convert demand to a per-hour
   > rate first. `estimated_passengers` is still returned as the raw 2-hour total, as the
   > spec requires.

4. **Capacity — scales with scope.** See below.
5. **Load ratio** — `demand_per_hour ÷ capacity_per_hour`.
6. **Category** — `< 0.6` Light · `0.6–1.0` Moderate · `> 1.0` Severe.
7. **Estimated wait** —
   - `ratio ≤ 1`: `BASE + (SATURATED − BASE) × ratio` → 5 min idle, 25 min at saturation.
   - `ratio > 1`: backlog forms. `backlog = est_pax − (capacity_per_hour × 2)`;
     `wait = SATURATED + backlog ÷ (capacity_per_hour ÷ 60)`. Clamped to `MAX_WAIT_MIN`.
8. **Recommended arrival** — `departure − wait − GATE_BUFFER_MIN`.

### ⚠ Capacity must scale with fallback scope

**This was a bug in v1.** Capacity was fixed at 750/hour — one terminal's worth — while the
terminal fallback widened *demand* to the whole airport. Airport-wide demand against
single-terminal capacity returns **"Severe" for essentially every fallback**, which is worse
than useless: it is confidently wrong precisely when confidence is lowest.

```python
if scope == "terminal":
    capacity = LANES_PER_TERMINAL * PAX_PER_LANE_PER_HOUR          # 750
else:  # whole airport
    n_terminals = count_distinct_terminals(iata)                   # from cached board
    capacity = LANES_PER_TERMINAL * PAX_PER_LANE_PER_HOUR * max(n_terminals, 1)
```

`n_terminals` is derived from the cached departure board — free, and self-correcting as the
corpus grows. Tested explicitly: an airport-wide fallback on a normal day must **not** yield
Severe.

### Terminal fallback ladder

Applied in order, with the outcome surfaced in the response and the UI:

| # | Condition | Behavior | `confidence` |
|---|---|---|---|
| 1 | Provider returns a terminal | Terminal-scoped calculation | `high` |
| 2 | Terminal null, **history has this flight** (§7) | Use modal terminal — "UA123 usually departs T2 (14 of 15 observations)" | `medium` |
| 3 | No history | Whole-airport scope, **capacity scaled** | `low` |
| 4 | Departure beyond board coverage | Historical baseline (§7) | `low` |
| 5 | No history and beyond coverage | `422 out_of_range` — "check back within 48h" | — |

Never silently degrade. Every level below 1 renders a visible UI notice.

---

## 7. The self-building historical corpus

Every departure board fetched is already stored. Aggregating those snapshots yields two
features **at zero additional API cost** — the corpus builds itself as a side effect of
normal use.

### Terminal inference

`flight_terminal_history(flight_iata, terminal_norm, observed_count, last_seen)` — every
resolved flight with a known terminal increments a counter. Powers ladder level 2. After a
few weeks of use, most repeat flights resolve without needing a live terminal.

### Density baseline for far-future flights

`terminal_density_history(iata, terminal_norm, day_of_week, hour, avg_flights, sample_count)`
— each fetched board contributes its per-hour flight counts.

This addresses a problem deeper than null terminals: **departure boards only cover a few
days out.** A flight three weeks away has no board to filter, so no amount of terminal data
helps. The baseline answers from *typical* patterns instead — "based on 6 previous Fridays
at SFO T2" — labeled distinctly in the UI, never presented as a live reading.

Requires `MIN_SAMPLES_FOR_BASELINE = 3` before use; below that, ladder level 5 applies.

**Bootstrapping:** the corpus is empty on day one, so far-future queries return level 5
until real usage accumulates. That is honest and self-resolving — worth stating in the
README so early behavior isn't mistaken for a bug.

---

## 8. Quota model

The v1 airport allowlist is **dropped** — it was incompatible with flight-number input,
since users' airports cannot be known in advance.

**Core principle: cache hits are free and unlimited; only cache misses are metered.** Meter
the misses, not the requests. Friends re-checking the same flight are never throttled.

### Self-balancing daily allowance

```
daily_allowance = clamp(remaining_this_month ÷ days_left_in_month, 10, 60)
```

Underuse early and later days get more headroom; a burst throttles subsequent days but never
to zero, so the month cannot be emptied on day 3. Applies to the **pooled** budget across
providers; each provider additionally has its own hard cap in `provider_state`.

### Degradation ladder when budget is spent

1. Serve stale cache (`data_source: "stale"`, age shown).
2. Fall back to the historical baseline (§7).
3. Clear message: "Live data budget reached — cached airports still work."

**Already-cached airports keep working normally.** Friends mid-trip are never cut off; only
genuinely novel lookups are refused.

### Input validation as a first-class guard

Malformed input must never reach a provider: flight numbers are regex-validated, and IATA
codes are checked against a **bundled offline airport list** before any call. Typos cost
nothing.

### Still open

Whether to add a link passphrase (§12). A Funnel URL is public, and open input means a
scanner cycling IATA codes hits real budget. The daily allowance caps the damage at one
wasted month — no billing risk, assuming §3's overage check passes.

---

## 9. Database schema

SQLite, WAL mode, short-lived connections, path from `DB_PATH`.

```sql
-- Step 2 cache: one airport picture, always from exactly one provider
CREATE TABLE airport_schedule_cache (
    iata            TEXT PRIMARY KEY,
    fetched_at      INTEGER NOT NULL,
    source_provider TEXT NOT NULL,
    flight_count    INTEGER NOT NULL,
    window_start    TEXT NOT NULL,      -- coverage bounds, for out_of_range checks
    window_end      TEXT NOT NULL,
    raw_payload     TEXT NOT NULL
);

CREATE TABLE flights (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    iata              TEXT NOT NULL,
    flight_iata       TEXT,
    airline_iata      TEXT,
    dep_terminal      TEXT,             -- as provided
    dep_terminal_norm TEXT,             -- shared normalizer output
    dep_time_local    TEXT NOT NULL,    -- 'YYYY-MM-DD HH:MM'
    dep_time_utc      TEXT,
    status            TEXT,
    source_provider   TEXT NOT NULL
);
CREATE INDEX idx_flights_lookup ON flights(iata, dep_time_local);

-- Step 1 cache: dynamic TTL via recheck_after
CREATE TABLE flight_resolution_cache (
    flight_no       TEXT NOT NULL,
    flight_date     TEXT NOT NULL,
    dep_iata        TEXT,
    dep_terminal    TEXT,
    dep_time_local  TEXT,
    resolved_at     INTEGER NOT NULL,
    recheck_after   INTEGER NOT NULL,   -- §5 dynamic TTL
    source_provider TEXT NOT NULL,
    PRIMARY KEY (flight_no, flight_date)
);

-- Corpus (§7)
CREATE TABLE flight_terminal_history (
    flight_iata    TEXT NOT NULL,
    terminal_norm  TEXT NOT NULL,
    observed_count INTEGER NOT NULL DEFAULT 1,
    last_seen      INTEGER NOT NULL,
    PRIMARY KEY (flight_iata, terminal_norm)
);

CREATE TABLE terminal_density_history (
    iata          TEXT NOT NULL,
    terminal_norm TEXT NOT NULL,
    day_of_week   INTEGER NOT NULL,     -- 0=Mon
    hour          INTEGER NOT NULL,
    avg_flights   REAL NOT NULL,
    sample_count  INTEGER NOT NULL,
    PRIMARY KEY (iata, terminal_norm, day_of_week, hour)
);

-- Budget ledger (§8)
CREATE TABLE api_usage (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    provider  TEXT NOT NULL,
    endpoint  TEXT NOT NULL,            -- resolve | departures
    called_at INTEGER NOT NULL,
    day_key   TEXT NOT NULL,            -- 'YYYY-MM-DD'
    month_key TEXT NOT NULL,            -- 'YYYY-MM'
    status    TEXT NOT NULL             -- ok | error | blocked
);
CREATE INDEX idx_usage_month ON api_usage(month_key);
CREATE INDEX idx_usage_day   ON api_usage(day_key);

-- Router state, persisted so a crash loop can't re-probe exhausted providers (§4)
CREATE TABLE provider_state (
    provider     TEXT PRIMARY KEY,
    state        TEXT NOT NULL,         -- healthy | degraded | exhausted
    state_until  INTEGER,
    last_error   TEXT,
    updated_at   INTEGER NOT NULL
);
```

`flights` rows for an airport are deleted and re-inserted in a single transaction per
refresh, so the table never mixes two fetches.

---

## 10. API contract

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/health` | Liveness, active providers, mock/live mode |
| `GET` | `/api/predict/flight` | **Primary.** Params: `flight_no`, `date` |
| `GET` | `/api/predict/manual` | Fallback. Params: `airport`, `terminal`, `flight_time` |
| `GET` | `/api/quota` | Per-provider usage, pooled remaining, today's allowance |
| `GET` | `/api/schedule/{iata}` | Debug: cached board + age + source provider |

`GET /api/predict/flight?flight_no=UA123&date=2026-08-10`

```json
{
  "flight": { "flight_no": "UA123", "date": "2026-08-10",
              "departure_local": "2026-08-10T14:30" },
  "airport": "SFO",
  "terminal": "2",
  "scope": "terminal",
  "confidence": "high",
  "confidence_reason": "Terminal reported by provider",
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
  "source_provider": "aerodatabox",
  "api_calls_used": 0,
  "assumptions": { "seats_per_flight": 150, "origin_passenger_factor": 0.75,
                   "lanes": 5, "passengers_per_lane_per_hour": 150,
                   "rush_window_hours": 2 }
}
```

Error envelope: `{"error": {"code": "...", "message": "...", "detail": {...}}}` with codes
`invalid_flight_number`, `flight_not_found`, `multiple_matches` (includes options),
`out_of_range`, `budget_exhausted`, `all_providers_down`.

---

## 11. Frontend

Single page, mobile-first — friends open this on a phone at 5am.

- **FlightForm** (primary) — flight number + date. Two fields. The departure time comes from
  resolution, so the user never types it.
- **DisambiguationPrompt** — on `multiple_matches`, list the legs and let the user pick.
- **ManualForm** (fallback) — airport + terminal + time, revealed when resolution fails or
  via "enter details manually".
- **ResultCard** — category as hero with a color ramp (Light green / Moderate amber /
  Severe red); **recommended arrival time second-most prominent**, since it is the
  actionable number. Passenger estimate and flights-in-window below.
- **ConfidenceBanner** — always visible below `high`. Distinct copy per ladder level:
  *"Terminal not published yet — UA123 usually departs Terminal 2"* ·
  *"Estimating for the whole airport"* ·
  *"Based on 6 previous Fridays, not today's live schedule."*
- **Collapsible "how this was calculated"** — renders `assumptions` plus the arithmetic.
- **Status strip** — cache age, source provider, `api_calls_used` for this request.

Dev: Vite proxies `/api` → `:8000`. Prod: `npm run build` → FastAPI serves `dist/`.

---

## 12. Open decisions

None block phases 1–4. Each is a config change or an additive module.

| # | Decision | Default if unanswered |
|---|---|---|
| 1 | **Verify free-tier numbers** (§3) — quotas, AirLabs page size, AeroDataBox FIDS inclusion, **overage vs hard-fail** | Build both providers; `PROVIDER_ORDER=aerodatabox,airlabs`; conservative local caps |
| 2 | Link passphrase (§8) | Not built; daily allowance is the only guard |
| 3 | Historical corpus in v1, or v2? | Tables created in phase 1; population in phase 5; read-path in phase 6 |
| 4 | Provider routing by airport size (§3) | Not built; static `PROVIDER_ORDER` |

---

## 13. Repository structure

```
aeroQ/
├── backend/
│   ├── app/
│   │   ├── main.py          # FastAPI app, routes, static mount, lifespan
│   │   ├── config.py        # env + all tunable constants
│   │   ├── db.py            # connection, schema init, WAL
│   │   ├── models.py        # pydantic schemas
│   │   ├── normalize.py     # shared terminal normalizer + NormalizedFlight
│   │   ├── providers/
│   │   │   ├── base.py      # ScheduleProvider ABC + capability flags
│   │   │   ├── aerodatabox.py
│   │   │   ├── airlabs.py
│   │   │   └── mock.py
│   │   ├── router.py        # pre-emptive budget routing, provider state
│   │   ├── resolver.py      # step 1: flight# → airport/terminal, dynamic TTL
│   │   ├── cache.py         # step 2: airport board, locks, budget
│   │   ├── history.py       # corpus writes + baseline reads
│   │   └── predict.py       # window filter, scoped capacity, fallback ladder
│   ├── tests/
│   ├── data/airports.json   # bundled offline IATA list
│   └── requirements.txt
├── frontend/src/            # App.jsx, api.js, components/
├── deploy/                  # aeroq.service, rebuild.sh
├── Dockerfile, docker-compose.yml, .env.example, README.md
└── BUILD_PLAN.md
```

---

## 14. Build phases

Each phase ends in a working state and a commit.

| # | Phase | Deliverable | Verification |
|---|---|---|---|
| 1 | Scaffold | Layout, `config.py`, `db.py` (full schema), `normalize.py`, bundled airport list | Schema initializes; terminal normalizer passes its fixture matrix |
| 2 | Providers | `base.py` ABC, `MockProvider` with seeded boards + resolutions, real provider skeletons | Mock returns plausible boards and resolutions for any date |
| 3 | Router | Pre-emptive budget routing, provider state machine, failover | Fake providers simulating `429` / `500` / timeout; **exhausted is sticky, degraded is not** |
| 4 | Resolution + cache | `resolver.py` (dynamic TTL, parsing, multi-leg), `cache.py` (locks, budget) | Hit/stale/miss/concurrent/over-budget paths; cost table in §5 verified per scenario |
| 5 | Prediction | Window filter, **scoped capacity**, fallback ladder, corpus writes | Worked example; boundary ratios; **airport-wide fallback must not yield Severe** |
| 6 | API + history reads | Routes, models, error envelope, baseline reads, static mount | `TestClient` across all endpoints and every error code |
| 7 | Frontend | Flight form, disambiguation, manual fallback, result card, confidence banner | End-to-end against mock in a browser |
| 8 | Live + deploy | Real provider validation, systemd unit, `rebuild.sh`, README | **One deliberate, inspected live call per provider**; Funnel URL loads from a phone on cellular |

Phases 3 and 5 are independent. Phase 8's live calls are the **first** real API usage in the
entire build.

---

## 15. Deployment on the GPD Pocket

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
`tailscale set --hostname=aeroq` controls the first label.

**Operational notes for a GPD Pocket:**

- It is a handheld with a lid. `sudo systemctl mask sleep.target suspend.target` (or
  `HandleLidSwitch=ignore` in `logind.conf`) or the URL goes dark when closed.
- Funnel URLs are fully public — anyone with the link reaches the app, no Tailscale account
  needed on their end. Intended here, but be deliberate about it.
- **Back up `aeroq.db`.** Once the corpus (§7) accumulates, the database holds data that
  cannot be re-fetched without spending quota. A weekly `sqlite3 .backup` cron is cheap
  insurance.

`Dockerfile` / `docker-compose.yml` ship as an alternate path, not the primary route.

---

## 16. Open risks

| Risk | Impact | Mitigation |
|---|---|---|
| Free-tier numbers differ from assumption | Provider ordering wrong; budget math off | §12.1 verification before phase 8; ordering is config |
| **Overage auto-bills instead of hard-failing** | **Real money** | Explicitly verified in §12.1; conservative local caps enforced independently of the provider |
| Terminal null at most airports | Ladder sits at level 2–3 | Corpus improves this over time; every level is labeled |
| Provider semantic mismatch (terminal vs concourse) | Wrong terminal filter | Single-provider-per-picture rule; `source_provider` stored per row |
| Board coverage narrower than hoped | Far-future flights unusable | Historical baseline (§7); honest `out_of_range` otherwise |
| Corpus empty at launch | Far-future flights fail for the first weeks | Documented in README as expected, self-resolving |
| GPD sleeps or drops Wi-Fi | URL dies silently | Mask sleep targets; `Restart=always`; both services systemd-managed |
| Heuristic wrong vs. real waits | Friends miss flights | Assumptions in every response; framed as an estimate, never a guarantee; 45-min buffer in recommended arrival |
| Scanner finds the public URL | One wasted month of quota | Metered misses + daily allowance; passphrase available (§12.2) |
