"""FastAPI application: routes, error envelope, and the static frontend mount.

Single origin by design — the API and the built React app are served from one
process on one port. That is what makes the deployment a single Tailscale
Funnel target, and it removes CORS from production entirely.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__, airports, budget, cache, db, history, predict, resolver
from .config import BACKEND_ROOT, settings
from .models import (
    AirportInfo,
    ErrorResponse,
    FlightInfo,
    FlightOption,
    HealthResponse,
    ManualQuery,
    PredictionResponse,
    QuotaResponse,
    RushWindow,
    iso,
)
from .predict import PredictionUnavailable
from .providers import build_providers
from .providers.base import FlightNotFound
from .resolver import InvalidFlightNumber
from .router import AllProvidersUnavailable, ProviderRouter

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger(__name__)

FRONTEND_DIST = BACKEND_ROOT.parent / "frontend" / "dist"


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    providers = build_providers()
    app.state.router = ProviderRouter(providers)
    log.info("providers active: %s", ", ".join(p.name for p in providers))
    if [p.name for p in providers] == ["mock"]:
        log.warning(
            "running on MOCK data only — set a provider API key in .env for live schedules"
        )
    try:
        yield
    finally:
        await app.state.router.aclose()


app = FastAPI(
    title="aeroQ",
    version=__version__,
    description="Airport security wait predictions from departure density.",
    lifespan=lifespan,
)

if settings.cors_origin_list:
    # Dev only: production is single-origin and needs no CORS.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_methods=["GET"],
        allow_headers=["*"],
    )


def get_router(request: Request) -> ProviderRouter:
    return request.app.state.router


def error(code: str, message: str, status: int, detail: dict | None = None) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message, "detail": detail}},
    )


# --- Error handlers ---------------------------------------------------------
# One envelope for every failure, so the frontend has a single shape to render.

@app.exception_handler(InvalidFlightNumber)
async def _invalid_flight(_: Request, exc: InvalidFlightNumber):
    return error("invalid_flight_number", str(exc), 400)


@app.exception_handler(FlightNotFound)
async def _not_found(_: Request, exc: FlightNotFound):
    return error("flight_not_found", str(exc), 404)


@app.exception_handler(airports.UnknownAirportError)
async def _unknown_airport(_: Request, exc: airports.UnknownAirportError):
    return error("airport_unknown", str(exc), 400, {"iata": exc.iata})


@app.exception_handler(PredictionUnavailable)
async def _unavailable(_: Request, exc: PredictionUnavailable):
    return error(exc.code, str(exc), 422)


@app.exception_handler(AllProvidersUnavailable)
async def _no_providers(_: Request, exc: AllProvidersUnavailable):
    # Budget exhaustion is the expected case here and deserves its own code,
    # so the UI can say "try again tomorrow" rather than "something broke".
    exhausted = all(
        "quota" in reason or "allowance" in reason or "cap" in reason
        for reason in exc.reasons.values()
    )
    if exhausted and exc.reasons:
        return error(
            "budget_exhausted",
            "The daily live-data budget is used up. Airports already cached "
            "still work; new ones will be available again tomorrow.",
            503,
            {"providers": exc.reasons},
        )
    return error(
        "all_providers_down",
        "No flight data provider is reachable right now.",
        502,
        {"providers": exc.reasons},
    )


# --- Prediction -------------------------------------------------------------

def _build_response(
    prediction: predict.Prediction,
    board: cache.BoardResult,
    *,
    flight: FlightInfo | None = None,
    calls_used: int = 0,
) -> PredictionResponse:
    return PredictionResponse(
        flight=flight,
        airport=prediction.airport,
        airport_name=airports.display_name(prediction.airport),
        terminal=prediction.terminal,
        scope=prediction.scope,
        confidence=prediction.confidence,
        confidence_reason=prediction.confidence_reason,
        basis=prediction.basis,
        terminal_matched=prediction.terminal_matched,
        rush_window=RushWindow(
            start=iso(prediction.rush_window_start),
            end=iso(prediction.rush_window_end),
        ),
        flights_in_window=prediction.flights_in_window,
        estimated_passengers=prediction.estimated_passengers,
        demand_per_hour=prediction.demand_per_hour,
        capacity_per_hour=prediction.capacity_per_hour,
        load_ratio=prediction.load_ratio,
        wait_category=prediction.wait_category,
        estimated_wait_minutes=prediction.estimated_wait_minutes,
        recommended_arrival_local=iso(prediction.recommended_arrival_local),
        data_source=board.data_source,
        cache_age_minutes=board.cache_age_minutes,
        source_provider=board.source_provider,
        api_calls_used=calls_used,
        note=prediction.note,
        assumptions=prediction.assumptions,
    )


@app.get(
    "/api/predict/flight",
    response_model=PredictionResponse,
    responses={300: {"model": dict}, 400: {"model": ErrorResponse}},
    summary="Predict the wait for a flight number",
)
async def predict_flight(
    request: Request,
    flight_no: str = Query(..., description="e.g. UA123"),
    date: str = Query(..., description="YYYY-MM-DD"),
    dep_iata: str | None = Query(
        None, description="Chosen departure airport when a flight has several legs"
    ),
):
    router = get_router(request)

    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        return error("invalid_request", "date must be YYYY-MM-DD", 400)

    outcome = await resolver.resolve(router, flight_no, date, prefer_iata=dep_iata)

    if not outcome.resolutions:
        return error("flight_not_found", f"No flight {flight_no} on {date}", 404)

    if outcome.ambiguous:
        # Returned rather than guessed: picking a leg silently could strand a
        # user at the wrong airport. Disambiguation costs no extra API call,
        # since the legs are already cached.
        return JSONResponse(
            status_code=300,
            content={
                "code": "multiple_matches",
                "message": (
                    f"{flight_no} has more than one departure on {date}. "
                    "Which one are you taking?"
                ),
                "options": [
                    FlightOption(
                        dep_iata=r.dep_iata,
                        dep_airport_name=airports.display_name(r.dep_iata),
                        dep_terminal=r.dep_terminal_norm,
                        departure_local=iso(r.dep_time_local),
                        arr_iata=r.arr_iata,
                    ).model_dump()
                    for r in outcome.resolutions
                ],
            },
        )

    leg = outcome.single
    if leg.dep_time_local is None:
        return error(
            "out_of_range",
            f"No departure time is published for {flight_no} on {date} yet.",
            422,
        )

    board = await cache.get_board(router, leg.dep_iata, leg.dep_time_local)
    prediction = predict.predict(
        board,
        leg.dep_time_local,
        terminal_norm=leg.dep_terminal_norm,
        flight_iata=leg.flight_no,
    )

    return _build_response(
        prediction,
        board,
        flight=FlightInfo(
            flight_no=leg.flight_no,
            date=leg.flight_date,
            departure_local=iso(leg.dep_time_local),
            arrival_airport=leg.arr_iata,
        ),
        calls_used=outcome.calls_used + board.calls_used,
    )


@app.get(
    "/api/predict/manual",
    response_model=PredictionResponse,
    responses={400: {"model": ErrorResponse}},
    summary="Predict the wait from an airport, terminal and time",
)
async def predict_manual(
    request: Request,
    airport: str = Query(..., description="IATA code, e.g. SFO"),
    flight_time: str = Query(..., description="Local time, e.g. 2026-08-10T14:30"),
    terminal: str | None = Query(None),
):
    router = get_router(request)

    try:
        query = ManualQuery(airport=airport, flight_time=flight_time, terminal=terminal)
    except ValueError as exc:
        return error("invalid_request", str(exc), 400)

    # Strict validation: a manually typed code is where typos originate, and a
    # typo that reaches a provider spends budget for a guaranteed-empty answer.
    iata = airports.validate_manual_iata(query.airport)
    departure = datetime.strptime(query.flight_time, "%Y-%m-%dT%H:%M")

    from .normalize import normalize_terminal

    board = await cache.get_board(router, iata, departure)
    prediction = predict.predict(
        board, departure, terminal_norm=normalize_terminal(query.terminal)
    )
    return _build_response(prediction, board, calls_used=board.calls_used)


# --- Introspection ----------------------------------------------------------

@app.get("/api/health", response_model=HealthResponse)
async def health(request: Request):
    router = get_router(request)
    return HealthResponse(
        status="ok",
        version=__version__,
        live_data=any(p != "mock" for p in router.names),
        providers=router.status_snapshot(),
        corpus=history.corpus_stats(),
    )


@app.get("/api/quota", response_model=QuotaResponse)
async def quota(request: Request):
    return budget.snapshot(get_router(request).names)


@app.get("/api/airports", response_model=list[AirportInfo])
async def list_airports():
    """Known airports, for the manual form's picker."""
    return airports.all_airports()


@app.get("/api/schedule/{iata}", summary="Debug: the cached board for an airport")
async def schedule(iata: str, limit: int = Query(50, le=500)):
    board = cache.load_board(iata)
    return {
        "iata": board.iata,
        "data_source": board.data_source,
        "cache_age_minutes": board.cache_age_minutes,
        "source_provider": board.source_provider,
        "window_start": iso(board.window_start),
        "window_end": iso(board.window_end),
        "flight_count": len(board.flights),
        "terminals": cache.count_terminals(board.iata),
        "flights": [
            {
                "flight_iata": f.flight_iata,
                "dep_time_local": iso(f.dep_time_local),
                "terminal": f.dep_terminal_norm,
                "status": f.status,
            }
            for f in board.flights[:limit]
        ],
    }


# --- Static frontend --------------------------------------------------------
# Mounted last so it never shadows /api. Absent in development, where Vite
# serves the frontend and proxies /api here.

if FRONTEND_DIST.is_dir():
    app.mount(
        "/assets",
        StaticFiles(directory=FRONTEND_DIST / "assets"),
        name="assets",
    )

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa(full_path: str):
        candidate = FRONTEND_DIST / full_path
        if full_path and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(FRONTEND_DIST / "index.html")

else:  # pragma: no cover - development convenience

    @app.get("/", include_in_schema=False)
    async def no_frontend():
        return {
            "status": "ok",
            "message": (
                "Frontend not built. Run `npm run build` in frontend/, or use "
                "the Vite dev server on :5173."
            ),
            "docs": "/docs",
        }
