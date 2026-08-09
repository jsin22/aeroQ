import { useState } from 'react'
import './App.css'
import { ApiError, MultipleMatches, predictFlight, predictManual } from './api'
import DisambiguationPrompt from './components/DisambiguationPrompt'
import FlightForm from './components/FlightForm'
import ManualForm from './components/ManualForm'
import ResultCard from './components/ResultCard'

/**
 * A small state machine rather than a pile of booleans: the four states are
 * mutually exclusive, and modelling them as separate flags is how you end up
 * rendering a result and an error at the same time.
 */
const IDLE = 'idle'
const LOADING = 'loading'
const CHOOSING = 'choosing'
const DONE = 'done'

export default function App() {
  const [mode, setMode] = useState('flight')
  const [state, setState] = useState(IDLE)
  const [result, setResult] = useState(null)
  const [error, setError] = useState(null)
  const [choices, setChoices] = useState(null)
  const [pending, setPending] = useState(null)

  const reset = () => {
    setState(IDLE)
    setResult(null)
    setError(null)
    setChoices(null)
    setPending(null)
  }

  async function run(fn, context) {
    setState(LOADING)
    setError(null)
    setChoices(null)
    try {
      const data = await fn()
      setResult(data)
      setState(DONE)
    } catch (e) {
      if (e instanceof MultipleMatches) {
        // A question, not a failure.
        setChoices({ message: e.message, options: e.options })
        setPending(context)
        setState(CHOOSING)
        return
      }
      setError(
        e instanceof ApiError
          ? { code: e.code, message: e.message }
          : { code: 'unknown', message: 'Something went wrong. Please try again.' }
      )
      setState(IDLE)
    }
  }

  const onFlight = (flightNo, date) =>
    run(() => predictFlight(flightNo, date), { flightNo, date })

  const onManual = (airport, when, terminal) =>
    run(() => predictManual(airport, when, terminal), null)

  const onChoose = (depIata) =>
    run(() => predictFlight(pending.flightNo, pending.date, depIata), pending)

  const loading = state === LOADING

  return (
    <div className="app">
      <header className="masthead">
        <h1>aeroQ</h1>
        <p>How busy will security be when you get there?</p>
      </header>

      {state !== DONE && state !== CHOOSING && (
        <>
          <div className="tabs" role="tablist">
            <button
              role="tab"
              aria-selected={mode === 'flight'}
              onClick={() => setMode('flight')}
            >
              By flight number
            </button>
            <button
              role="tab"
              aria-selected={mode === 'manual'}
              onClick={() => setMode('manual')}
            >
              By airport
            </button>
          </div>

          {error && (
            <div className="banner error">
              <span className="icon" aria-hidden="true">✕</span>
              <span>{error.message}</span>
            </div>
          )}

          {mode === 'flight' ? (
            <FlightForm onSubmit={onFlight} loading={loading} />
          ) : (
            <ManualForm onSubmit={onManual} loading={loading} />
          )}

          {error && mode === 'flight' && (
            <p style={{ textAlign: 'center', fontSize: '0.88rem' }}>
              Can’t find your flight?{' '}
              <button className="link" onClick={() => setMode('manual')}>
                Enter the airport instead
              </button>
            </p>
          )}
        </>
      )}

      {state === CHOOSING && (
        <DisambiguationPrompt
          message={choices.message}
          options={choices.options}
          onChoose={onChoose}
          onCancel={reset}
        />
      )}

      {state === DONE && result && <ResultCard result={result} onReset={reset} />}

      <footer className="site">
        Estimates are derived from scheduled departure volume, not live queue data.
        <br />
        Always follow your airline’s advice on when to arrive.
      </footer>
    </div>
  )
}
