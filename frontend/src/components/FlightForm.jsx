import { useState } from 'react'
import { toDateInput } from '../datetime'

/**
 * The primary input: flight number and date. Two fields.
 *
 * Departure time is deliberately not asked for — resolution returns it. Asking
 * would be redundant, and a user's recollection of their departure time is
 * less reliable than the schedule's.
 */
export default function FlightForm({ onSubmit, loading }) {
  const [flightNo, setFlightNo] = useState('')
  const [date, setDate] = useState(() => toDateInput())

  const submit = (e) => {
    e.preventDefault()
    const trimmed = flightNo.trim()
    if (trimmed) onSubmit(trimmed, date)
  }

  return (
    <form className="card" onSubmit={submit}>
      <div className="field">
        <label htmlFor="flight-no">Flight number</label>
        <input
          id="flight-no"
          value={flightNo}
          onChange={(e) => setFlightNo(e.target.value)}
          placeholder="UA123"
          autoCapitalize="characters"
          autoCorrect="off"
          spellCheck="false"
          enterKeyHint="go"
          required
        />
        <p className="hint">Airline code and number, e.g. UA123 or BA 287.</p>
      </div>

      <div className="field">
        <label htmlFor="flight-date">Date of departure</label>
        <input
          id="flight-date"
          type="date"
          value={date}
          onChange={(e) => setDate(e.target.value)}
          required
        />
      </div>

      <button className="primary" type="submit" disabled={loading}>
        {loading ? 'Checking…' : 'Estimate my wait'}
      </button>
    </form>
  )
}
