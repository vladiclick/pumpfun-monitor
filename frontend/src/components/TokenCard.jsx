import { memo } from 'react'
import { clockTime, latency, shortMint, usd } from '../lib/format'

/** Etichete prietenoase pentru evenimente fara verdict (dupa ev.type). */
const TYPE_LABELS = {
  token_detected: 'DETECTAT',
  bot_started: 'BOT PORNIT',
  bot_stopped: 'BOT OPRIT',
  ws_connected: 'CONECTAT',
  ws_disconnected: 'DECONECTAT',
  error: 'EROARE',
  stats: 'STARE',
}

/** Text insigna, clasa modificator si nota explicativa, pentru orice tip de eveniment. */
function describeVerdict(ev) {
  switch (ev.verdict) {
    case 'PASS':
      return {
        text: 'TRECUT',
        cls: 'pass',
        note: 'A trecut verificarile de baza. Nu e o garantie.',
        tooltip:
          'Autoritatile de emitere si de inghetare sunt revocate si nu au aparut semnale de risc. Un token poate trece toate verificarile si tot sa fie rug pull.',
      }
    case 'FAIL':
      return {
        text: 'RESPINS',
        cls: 'reject',
        note: ev.reason || 'A picat una dintre verificari.',
      }
    case 'PENDING':
      return {
        text: 'IN ASTEPTARE',
        cls: '',
        note: 'Nu s-a putut afla inca lichiditatea — tokenul e prea nou ca sa fie indexat.',
      }
    case 'ERROR':
      return {
        text: 'NEDECIS',
        cls: '',
        note: ev.reason || 'Verificarea nu s-a putut face. Nu inseamna ca tokenul e bun sau rau.',
      }
    default:
      // Evenimente fara verdict (token_detected, bot_started, etc).
      return {
        text: TYPE_LABELS[ev.type] || ev.type,
        cls: '',
        note: ev.message || '',
      }
  }
}

export default memo(function TokenCard({ ev, selected, fresh, onSelect }) {
  const { text, cls, note, tooltip } = describeVerdict(ev)
  const liq = ev.checks?.liquidity
  const rug = ev.checks?.rugcheck

  // Cel mult trei stat-uri, doar cele disponibile.
  const stats = []
  if (typeof ev.latency_ms === 'number') {
    stats.push({ label: 'Timp pana la verdict', value: latency(ev.latency_ms) })
  }
  if (liq?.liquidity_known === true) {
    stats.push({ label: 'Lichiditate', value: usd(liq.liquidity_usd) })
  } else if (liq?.liquidity_known === false) {
    stats.push({ label: 'Lichiditate', value: 'necunoscuta' })
  }
  if (typeof rug?.rugcheck_score === 'number') {
    stats.push({ label: 'Scor risc', value: rug.rugcheck_score })
  } else if (rug?.no_report) {
    stats.push({ label: 'Scor risc', value: 'fara raport' })
  }

  const showName = ev.name && ev.name !== ev.symbol

  return (
    <button
      type="button"
      className={`token-card${selected ? ' selected' : ''}${fresh ? ' fresh' : ''}`}
      onClick={() => onSelect(selected ? null : ev.event_id)}
    >
      <div className="token-card-main">
        <div className="token-identity">
          <div className="token-symbol">{ev.symbol || '—'}</div>
          {showName && <div className="token-name">{ev.name}</div>}
        </div>
        <span className={`verdict-badge${cls ? ` ${cls}` : ''}`} // Nota de sub insigna e scurta; explicatia completa sta in tooltip.
          title={tooltip || note || undefined}>
          {text}
        </span>
      </div>

      {note && <div className="verdict-note">{note}</div>}

      {ev.mint && <div className="token-mint">mint {shortMint(ev.mint)}</div>}

      {stats.length > 0 && (
        <div className="token-stats">
          {stats.map((s) => (
            <div className="stat" key={s.label}>
              <div className="stat-label">{s.label}</div>
              <div className="stat-value">{s.value}</div>
            </div>
          ))}
        </div>
      )}

      <div className="token-card-foot">
        <span className="token-bot">{ev.bot_id}</span>
        <span className="token-time">{clockTime(ev.ts)}</span>
      </div>
    </button>
  )
})
