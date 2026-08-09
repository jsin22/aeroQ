/**
 * Local-time formatting for `date` and `datetime-local` inputs.
 *
 * The obvious `new Date().toISOString().slice(0, 16)` is wrong here:
 * toISOString converts to UTC, so a user in PDT would see a default seven
 * hours off, and near midnight the *date* would be wrong outright. These
 * inputs want wall-clock local time, so it has to be built from the local
 * getters.
 */

const pad = (n) => String(n).padStart(2, '0')

export function toDateInput(date = new Date()) {
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`
}

export function toDateTimeInput(date = new Date()) {
  return `${toDateInput(date)}T${pad(date.getHours())}:${pad(date.getMinutes())}`
}
