/**
 * Shown when one flight number covers several legs on the same date.
 *
 * The backend returns the options rather than guessing, because guessing could
 * send someone to the wrong airport. Choosing here costs no API call — the
 * legs are already cached from the original lookup.
 */
export default function DisambiguationPrompt({ message, options, onChoose, onCancel }) {
  const formatTime = (iso) =>
    iso ? new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : '—'

  return (
    <div className="card">
      <div className="banner">
        <span className="icon" aria-hidden="true">✱</span>
        <span>{message}</span>
      </div>

      <div className="options">
        {options.map((o) => (
          <button
            key={`${o.dep_iata}-${o.departure_local}`}
            className="option"
            onClick={() => onChoose(o.dep_iata)}
          >
            <div className="airport">
              {o.dep_iata} — {o.dep_airport_name || 'Unknown airport'}
            </div>
            <div className="meta">
              Departs {formatTime(o.departure_local)}
              {o.dep_terminal ? ` · Terminal ${o.dep_terminal}` : ''}
              {o.arr_iata ? ` · to ${o.arr_iata}` : ''}
            </div>
          </button>
        ))}
      </div>

      <p style={{ marginBottom: 0, marginTop: '1rem' }}>
        <button className="link" onClick={onCancel}>
          Start over
        </button>
      </p>
    </div>
  )
}
