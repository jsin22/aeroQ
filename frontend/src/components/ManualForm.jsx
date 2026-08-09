import { useEffect, useState } from 'react'
import { getAirports } from '../api'
import { toDateTimeInput } from '../datetime'

/**
 * Fallback for when flight resolution fails — an unlisted charter, a codeshare
 * the provider does not carry, or a flight so far out that nothing is
 * published.
 *
 * The airport is a <select> rather than free text: it makes an invalid code
 * unrepresentable, which is also the cheapest possible quota guard, since a
 * typo that reaches a provider spends a call to return nothing.
 */
export default function ManualForm({ onSubmit, loading }) {
  const [airports, setAirports] = useState([])
  const [airport, setAirport] = useState('')
  const [terminal, setTerminal] = useState('')
  const [when, setWhen] = useState(() => {
    const t = new Date(Date.now() + 3 * 3600 * 1000)
    t.setMinutes(0, 0, 0)
    return toDateTimeInput(t)
  })

  useEffect(() => {
    getAirports()
      .then(setAirports)
      .catch(() => setAirports([]))
  }, [])

  const submit = (e) => {
    e.preventDefault()
    if (airport && when) onSubmit(airport, when, terminal.trim())
  }

  return (
    <form className="card" onSubmit={submit}>
      <div className="field">
        <label htmlFor="airport">Airport</label>
        <select
          id="airport"
          value={airport}
          onChange={(e) => setAirport(e.target.value)}
          required
        >
          <option value="">Choose an airport…</option>
          {airports.map((a) => (
            <option key={a.iata} value={a.iata}>
              {a.iata} — {a.city}, {a.country}
            </option>
          ))}
        </select>
      </div>

      <div className="row">
        <div className="field">
          <label htmlFor="departure">Departure time</label>
          <input
            id="departure"
            type="datetime-local"
            value={when}
            onChange={(e) => setWhen(e.target.value)}
            required
          />
        </div>
        <div className="field">
          <label htmlFor="terminal">Terminal</label>
          <input
            id="terminal"
            value={terminal}
            onChange={(e) => setTerminal(e.target.value)}
            placeholder="Optional"
            autoCorrect="off"
          />
        </div>
      </div>

      <p className="hint" style={{ marginTop: '-0.4rem', marginBottom: '0.9rem' }}>
        Leave the terminal blank and we will estimate across the whole airport.
      </p>

      <button className="primary" type="submit" disabled={loading || !airport}>
        {loading ? 'Checking…' : 'Estimate my wait'}
      </button>
    </form>
  )
}
