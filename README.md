# aeroQ

Estimates how busy airport security will be when you get there, from how many
flights depart just before yours.

You type a flight number. It works out the airport and terminal, counts the
departures in the two hours before yours, converts that to a passenger load,
compares it against checkpoint capacity, and tells you **what time to be at
security**.

It runs on a single small machine and shares one public URL with friends.

---

## What it actually knows

This is a **heuristic, not a measurement**. There is no live queue sensor
anywhere in it. It infers pressure from schedule density and a set of stated
assumptions — 150 seats per flight, 75% of passengers originating rather than
connecting, 5 lanes per terminal at 150 passengers/hour each.

Every response carries those assumptions and a `confidence` level, and the UI
shows the arithmetic. That is deliberate: an estimate presented without its
provenance implies an authority this does not have.

**It is honest about degrading.** Five levels, each visible in the UI:

| | Situation | What you get |
|---|---|---|
| 1 | Terminal published | Terminal-level estimate, high confidence |
| 2 | Terminal unknown, flight seen before | "UA123 usually departs Terminal 2", medium |
| 3 | Terminal unknown entirely | Whole-airport estimate, low |
| 4 | Departure too far out for any schedule | Typical-day estimate from accumulated history, low |
| 5 | No schedule and no history | An honest "check back closer to your flight" |

---

## Quick start

```bash
git clone git@github.com:jsin22/aeroQ.git && cd aeroQ

python3 -m venv backend/.venv
backend/.venv/bin/pip install -r backend/requirements.txt

cd frontend && npm install && npm run build && cd ..

cd backend && .venv/bin/python -m uvicorn app.main:app --port 8000
```

Open <http://127.0.0.1:8000>. **No API key is needed** — with no keys set it
runs entirely on a deterministic mock provider that generates realistic
schedules (~477 departures/day at SFO). That is also how the whole thing was
built, so no live quota was consumed.

### Development

```bash
cd backend && .venv/bin/python -m uvicorn app.main:app --reload --port 8000
cd frontend && npm run dev     # :5173, proxies /api to :8000
cd backend && .venv/bin/python -m pytest      # 201 tests
```

---

## Going live with real data

Copy `.env.example` to `.env` and set at least one provider key.

> ### ⚠ Verify these before spending anything
>
> The provider numbers below were **not** verified during the build, and the
> economics depend on them. Check current docs first:
>
> 1. **Does exceeding the free tier hard-fail, or auto-bill?** AeroDataBox is
>    distributed via RapidAPI, whose plans differ on this. This is the only
>    failure mode here that costs real money — everything else costs at most a
>    wasted month of quota. Set `*_MONTHLY_CAP` below the real tier regardless.
> 2. **Is the airport-departures (FIDS) endpoint included in the free plan**, or
>    metered at a higher weight?
> 3. **AirLabs' page size** (`limit` default and maximum), which determines how
>    many calls a large airport costs.

### Why two providers

The useful metric is not monthly quota but **calls per complete airport
picture**:

```
pictures/month = monthly_budget ÷ calls_per_complete_picture
```

AeroDataBox's departures endpoint is *time-windowed* — one call returns a 12-hour
block whether the airport has 10 departures or 800. AirLabs' is
*count-paginated*, so a large hub costs one call per page. That is the wrong
scaling: big hubs are both the most expensive to fetch and the most interesting
to predict.

So AeroDataBox leads and AirLabs backs it up. At a small airport one page *is*
the whole picture, so AirLabs costs exactly the same there.

Reorder freely — `PROVIDER_ORDER` is just config:

```bash
PROVIDER_ORDER=aerodatabox,airlabs,mock
```

Providers without a key are dropped at startup, so running with no keys is a
supported mode rather than an error.

---

## How the budget survives contact with friends

A public URL means anyone who finds it can trigger API calls, and free tiers are
small. Four things keep that bounded.

**Only cache misses are metered.** A cache hit is free and never counted, so
friends re-checking the same flight are never throttled. Only a genuinely novel
lookup spends anything.

| Scenario | Step 1 | Step 2 | Total |
|---|---|---|---|
| Both cached | 0 | 0 | **0** |
| Flight cached, board stale | 0 | 1 | **1** |
| Novel flight and airport | 1 | 1 | **2** |
| Second person on the same flight | 0 | 0 | **0** |

**A self-balancing daily allowance:**

```
daily_allowance = clamp(remaining_this_month ÷ days_left_in_month, MIN, MAX)
```

Underuse early and later days get more headroom. A burst throttles the following
days but never to zero, so the month cannot be emptied on day three.

**Dead-zone scheduling.** Airlines do not assign terminals until ~48–72h before
departure. Rather than re-asking a question the provider cannot answer, the
re-check is *scheduled* for the first moment an answer could exist. A flight
three weeks out costs nothing in the interim no matter how often it is looked
up.

**Concurrency collapse.** Five friends asking about SFO in the same second cost
one call, not five, via a per-airport lock that re-checks the cache after
acquiring.

When the budget does run out, already-cached airports keep working normally.
Only novel lookups are refused, and the message says so.

Watch it with `curl localhost:8000/api/quota`.

---

## The corpus builds itself

Every board fetched is stored anyway, so aggregating those snapshots costs
nothing and yields two things:

- **Terminal inference** — which terminal each flight number actually departs
  from, powering level 2 above.
- **A density baseline** — typical flights per hour by airport, terminal, day of
  week and hour, powering level 4.

**It is empty on day one.** Far-future queries fail honestly for the first few
weeks until real usage accumulates. That is expected, not a bug.

This makes `backend/data/aeroq.db` genuinely valuable: it holds data that
cannot be recovered without spending quota. Back it up.

```bash
sqlite3 backend/data/aeroq.db ".backup '/home/jsin/backups/aeroq-$(date +%F).db'"
```

---

## Running it on the GPD Pocket

### 1. Service

```bash
sudo cp deploy/aeroq.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now aeroq
```

It binds to `127.0.0.1` — nothing is exposed on the LAN.

One worker, deliberately: the per-airport locks are in-process, so a second
worker would have its own set and could double-spend the budget.

### 2. Public URL via Tailscale Funnel

No port forwarding, no domain, works behind NAT and CGNAT:

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
sudo tailscale set --hostname=aeroq     # controls the first label
sudo tailscale funnel --bg 8000
```

That prints your URL — `https://aeroq.<tailnet>.ts.net`, stable across reboots,
TLS handled at Tailscale's edge. The first run also prints a console link to
enable HTTPS certs and Funnel; one click each.

**A Funnel URL is fully public.** Anyone with the link reaches the app — no
Tailscale account needed on their end. That is the point here, but be
deliberate about it.

### 3. Stop the lid killing it

It is a handheld. Closing it takes the URL down:

```bash
sudo systemctl mask sleep.target suspend.target hibernate.target
```

Or set `HandleLidSwitch=ignore` in `/etc/systemd/logind.conf`.

### Updating

```bash
git pull && ./deploy/rebuild.sh
```

Runs tests, rebuilds the frontend, restarts the service. Additive schema
migrations apply on startup, so the database survives upgrades in place.

---

## API

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/predict/flight` | `flight_no`, `date`, optional `dep_iata` |
| `GET` | `/api/predict/manual` | `airport`, `flight_time`, optional `terminal` |
| `GET` | `/api/quota` | Usage, remaining, today's allowance |
| `GET` | `/api/health` | Provider states and corpus size |
| `GET` | `/api/airports` | Known airports |
| `GET` | `/api/schedule/{iata}` | Debug: the cached board |

Interactive docs at `/docs`. Errors share one envelope:

```json
{"error": {"code": "budget_exhausted", "message": "…", "detail": {}}}
```

`300 Multiple Choices` is a question, not a failure: the flight number matched
several legs and the response lists them. Choosing costs no API call.

---

## Tuning

Every constant is in `.env`. The defaults come from the original spec and are
**not calibrated against observed waits** — expect to adjust them once you see
real output.

One known skew: at small airports the defaults read high. Nine flights in the
rush window at Boise gives "Moderate", where reality is close to empty. The
causes are `SEATS_PER_FLIGHT=150` applied uniformly to regional aircraft, and
`5 lanes × 150/hour` understating a real checkpoint. Start there.

| Variable | Default | Effect |
|---|---|---|
| `SEATS_PER_FLIGHT` | 150 | Biggest single lever on the estimate |
| `ORIGIN_PAX_FACTOR` | 0.75 | Share not connecting |
| `LANES_PER_TERMINAL` | 5 | With the next value, sets capacity |
| `PAX_PER_LANE_PER_HOUR` | 150 | 5 × 150 = 750/hour per terminal |
| `LIGHT_MAX_RATIO` | 0.6 | Below this, "Light" |
| `MODERATE_MAX_RATIO` | 1.0 | Above this, "Severe" |
| `GATE_BUFFER_MIN` | 45 | Added to the recommended arrival |
| `CACHE_TTL_HOURS` | 4 | Higher = fewer calls, staler boards |
| `BOARD_HORIZON_DAYS` | 7 | Past this, no call is made at all |

---

## Layout

```
backend/app/
  main.py       routes, error envelope, static mount
  config.py     every tunable
  db.py         schema, WAL, additive migrations
  normalize.py  shared terminal normalizer
  airports.py   offline IATA reference
  providers/    base ABC, aerodatabox, airlabs, mock
  router.py     pre-emptive budget routing, provider state
  resolver.py   step 1: flight -> airport/terminal, dynamic TTL
  cache.py      step 2: airport board, locks, degradation
  history.py    the self-building corpus
  predict.py    rush window, scoped capacity, fallback ladder
frontend/src/   React app
deploy/         systemd unit, rebuild script
```

`BUILD_PLAN.md` records the design decisions and why they were made.
