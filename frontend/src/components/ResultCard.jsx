/**
 * The result.
 *
 * Deliberate hierarchy: the category is the hero, but **recommended arrival
 * time is the second-largest thing on screen** because it is the only
 * actionable number. "Severe" tells you to worry; "leave by 12:38" tells you
 * what to do.
 *
 * Anything below full confidence gets a visible banner. The estimate is a
 * heuristic, not a fitted model, so the UI must never present a degraded
 * answer as though it were a measurement.
 */

const CAVEAT =
  'This is an estimate from how many flights depart before yours, not a live ' +
  'measurement of the security queue. Treat it as a nudge, not a guarantee.'

function timeOf(iso) {
  if (!iso) return '—'
  return new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}

function ConfidenceBanner({ result }) {
  const { confidence, confidence_reason, basis, data_source, note } = result
  const banners = []

  if (basis === 'baseline') {
    banners.push({
      tone: 'warn',
      icon: '◷',
      text: `${confidence_reason}. This is a typical-day estimate, not today's actual schedule.`,
    })
  } else if (confidence !== 'high') {
    banners.push({ tone: 'warn', icon: '△', text: confidence_reason })
  }

  if (data_source === 'stale') {
    banners.push({
      tone: 'warn',
      icon: '⟳',
      text: `Showing cached data from ${result.cache_age_minutes} minutes ago — live data is unavailable right now.`,
    })
  }

  if (note && basis !== 'baseline') {
    banners.push({ tone: '', icon: 'ⓘ', text: note })
  }

  return banners.map((b, i) => (
    <div className={`banner ${b.tone}`} key={i}>
      <span className="icon" aria-hidden="true">{b.icon}</span>
      <span>{b.text}</span>
    </div>
  ))
}

function Workings({ result }) {
  const a = result.assumptions
  const seats = result.flights_in_window * a.seats_per_flight
  const fmt = (n) => Math.round(n).toLocaleString()

  const scopeLabel =
    result.scope === 'terminal' ? `Terminal ${result.terminal}` : 'whole airport'

  return (
    <details className="workings">
      <summary>How this was calculated</summary>

      <p className="calc">
{`Departures ${timeOf(result.rush_window.start)}–${timeOf(result.rush_window.end)}
  from ${scopeLabel}          ${result.flights_in_window}
× ${a.seats_per_flight} seats per flight        ${fmt(seats)}
× ${a.origin_passenger_factor} (rest are connecting)  ${fmt(result.estimated_passengers)} passengers
÷ ${a.rush_window_hours} hour window            ${fmt(result.demand_per_hour)} per hour
÷ ${fmt(result.capacity_per_hour)} per hour capacity   ${result.load_ratio}× load
                            → ${result.wait_category}`}
      </p>

      <dl>
        <div className="assumption">
          <dt>Security lanes assumed</dt>
          <dd>
            {a.lanes_per_terminal} per terminal, {a.passengers_per_lane_per_hour}/hour each
          </dd>
        </div>
        <div className="assumption">
          <dt>Checkpoint capacity used</dt>
          <dd>{fmt(result.capacity_per_hour)} passengers/hour</dd>
        </div>
        <div className="assumption">
          <dt>Buffer after security</dt>
          <dd>{a.gate_buffer_minutes} min to the gate</dd>
        </div>
        <div className="assumption">
          <dt>Estimate based on</dt>
          <dd>{result.basis === 'baseline' ? 'past weeks' : 'live schedule'}</dd>
        </div>
      </dl>

      <p className="caveat">{CAVEAT}</p>
    </details>
  )
}

export default function ResultCard({ result, onReset }) {
  const airportLabel = result.airport_name
    ? `${result.airport} · ${result.airport_name}`
    : result.airport

  return (
    <div className="card">
      <ConfidenceBanner result={result} />

      <p className="route">
        {result.flight ? (
          <>
            <strong>{result.flight.flight_no}</strong> from <strong>{airportLabel}</strong>
            {result.terminal ? `, Terminal ${result.terminal}` : ''}
            <br />
            departs {timeOf(result.flight.departure_local)}
          </>
        ) : (
          <>
            <strong>{airportLabel}</strong>
            {result.terminal ? `, Terminal ${result.terminal}` : ''}
            <br />
            departure {timeOf(result.rush_window.end)}
          </>
        )}
      </p>

      <div className={`verdict ${result.wait_category}`}>
        <div className="label">Expected security queue</div>
        <div className="category">{result.wait_category}</div>
        <div className="wait">around {result.estimated_wait_minutes} minutes</div>
      </div>

      <div className="arrival">
        <div className="label">Be at security by</div>
        <div className="time">{timeOf(result.recommended_arrival_local)}</div>
        <div className="sub">
          {result.estimated_wait_minutes} min queue +{' '}
          {result.assumptions.gate_buffer_minutes} min to reach the gate
        </div>
      </div>

      <div className="stats">
        <div className="stat">
          <div className="value">{result.flights_in_window}</div>
          <div className="name">flights before yours</div>
        </div>
        <div className="stat">
          <div className="value">{result.estimated_passengers.toLocaleString()}</div>
          <div className="name">est. passengers</div>
        </div>
        <div className="stat">
          <div className="value">{result.load_ratio}×</div>
          <div className="name">of capacity</div>
        </div>
      </div>

      <Workings result={result} />

      <div className="status-strip">
        <span>
          Data: {result.data_source}
          {result.cache_age_minutes != null && result.data_source !== 'fresh'
            ? ` (${result.cache_age_minutes} min old)`
            : ''}
        </span>
        <span>Source: {result.source_provider || 'history'}</span>
        <span>API calls used: {result.api_calls_used}</span>
      </div>

      <p style={{ marginBottom: 0, marginTop: '1rem', textAlign: 'center' }}>
        <button className="link" onClick={onReset}>
          Check another flight
        </button>
      </p>
    </div>
  )
}
