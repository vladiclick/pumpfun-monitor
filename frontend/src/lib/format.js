/** Functii de formatare partajate de componente. */

export function shortMint(mint, head = 4, tail = 4) {
  if (!mint) return '—'
  if (mint.length <= head + tail + 2) return mint
  return `${mint.slice(0, head)}…${mint.slice(-tail)}`
}

export function clockTime(ts) {
  if (!ts) return '--:--:--'
  const d = new Date(ts)
  return d.toLocaleTimeString('ro-RO', { hour12: false })
}

/** Latenta in format compact: 420ms / 6.6s */
export function latency(ms) {
  if (ms === null || ms === undefined) return '—'
  if (ms < 1000) return `${ms}ms`
  return `${(ms / 1000).toFixed(1)}s`
}

export function latencyClass(ms) {
  if (ms === null || ms === undefined) return ''
  if (ms < 1000) return 'lat-fast'
  if (ms > 5000) return 'lat-slow'
  return ''
}

/** Uptime: 45s / 12m 03s / 3h 12m */
export function uptime(seconds) {
  if (!seconds || seconds < 1) return '—'
  const s = Math.floor(seconds)
  if (s < 60) return `${s}s`
  if (s < 3600) return `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, '0')}s`
  const h = Math.floor(s / 3600)
  return `${h}h ${String(Math.floor((s % 3600) / 60)).padStart(2, '0')}m`
}

export function usd(value) {
  if (value === null || value === undefined || Number.isNaN(value)) return '—'
  const n = Number(value)
  if (n >= 1_000_000) return `$${(n / 1_000_000).toFixed(2)}M`
  if (n >= 1000) return `$${(n / 1000).toFixed(1)}k`
  return `$${n.toFixed(n < 1 ? 4 : 0)}`
}

export function compact(n) {
  if (n === null || n === undefined) return '—'
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`
  if (n >= 10_000) return `${(n / 1000).toFixed(1)}k`
  return String(n)
}
