/**
 * API client.
 *
 * Two response shapes need special handling:
 *
 * - **300 Multiple Choices** is a success with a question attached, not an
 *   error. The flight number matched several legs and the user has to pick one.
 * - Everything >= 400 uses the backend's single error envelope, so one branch
 *   here covers every failure the UI can encounter.
 */

const ERROR_FALLBACK = 'Something went wrong. Please try again.'

export class ApiError extends Error {
  constructor(code, message, detail, status) {
    super(message || ERROR_FALLBACK)
    this.code = code
    this.detail = detail
    this.status = status
  }
}

export class MultipleMatches extends Error {
  constructor(message, options) {
    super(message)
    this.options = options
  }
}

async function request(path, params = {}) {
  const query = new URLSearchParams(
    Object.entries(params).filter(([, v]) => v !== null && v !== undefined && v !== '')
  )
  const url = `${path}?${query}`

  let response
  try {
    response = await fetch(url)
  } catch {
    // Network-level failure: the tunnel or the box itself is unreachable.
    throw new ApiError(
      'network',
      'Could not reach the server. It may be offline.',
      null,
      0
    )
  }

  let body = null
  try {
    body = await response.json()
  } catch {
    body = null
  }

  if (response.status === 300 && body?.code === 'multiple_matches') {
    throw new MultipleMatches(body.message, body.options || [])
  }

  if (!response.ok) {
    const err = body?.error || {}
    throw new ApiError(err.code || 'unknown', err.message, err.detail, response.status)
  }

  return body
}

export const predictFlight = (flightNo, date, depIata) =>
  request('/api/predict/flight', { flight_no: flightNo, date, dep_iata: depIata })

export const predictManual = (airport, flightTime, terminal) =>
  request('/api/predict/manual', { airport, flight_time: flightTime, terminal })

export const getAirports = () => request('/api/airports')
export const getHealth = () => request('/api/health')
