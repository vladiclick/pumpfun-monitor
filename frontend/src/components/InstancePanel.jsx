import { useState } from 'react'
import { compact, uptime } from '../lib/format'

const BUSY_STATES = ['starting', 'stopping']

const STATE_LABEL = {
  running: 'Pornit',
  stopped: 'Oprit',
  starting: 'Porneste',
  stopping: 'Se opreste',
  crashed: 'A cazut',
  orphan: 'Ramas in urma',
}

export default function InstancePanel({ instances, selected, onSelect, onAction, error }) {
  return (
    <aside className="panel">
      <div className="panel-title">
        Instante
        <span className="count">{instances.length}</span>
      </div>

      {error && <div className="instance-error">{error}</div>}

      {instances.map((inst) => (
        <InstanceRow
          key={inst.bot_id}
          inst={inst}
          selected={selected === inst.bot_id}
          onSelect={onSelect}
          onAction={onAction}
        />
      ))}
    </aside>
  )
}

function InstanceRow({ inst, selected, onSelect, onAction }) {
  const [busy, setBusy] = useState(false)
  const running = inst.status === 'running'
  const stats = inst.stats || {}
  const disabled = busy || BUSY_STATES.includes(inst.status)
  const stateOff = inst.status === 'crashed' || inst.status === 'orphan'

  async function run(event, what) {
    // Butoanele sunt in interiorul cardului clicabil - oprim propagarea.
    event.stopPropagation()
    setBusy(true)
    try {
      await onAction(inst.bot_id, what)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div
      className={`instance-card${selected ? ' selected' : ''}`}
      role="button"
      tabIndex={0}
      onClick={() => onSelect(selected ? null : inst.bot_id)}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault()
          onSelect(selected ? null : inst.bot_id)
        }
      }}
    >
      <div className="instance-head">
        <span className="instance-name">{inst.label || inst.bot_id}</span>
        <span className={`instance-state${running ? ' on' : stateOff ? ' off' : ''}`}>
          {STATE_LABEL[inst.status] || inst.status}
        </span>
      </div>

      <div className="instance-meta">
        {running
          ? `Pornit de ${uptime(inst.uptime_s)} · ${compact(stats.passed ?? 0)} au trecut`
          : stats.detected
            ? `${compact(stats.detected)} token-uri scanate`
            : 'Nu a rulat inca'}
      </div>

      <div className="instance-actions">
        <button
          type="button"
          /* Accentul sta pe actiunea disponibila: cand botul ruleaza, butonul
             evidentiat trebuie sa fie "Opreste", nu un "Porneste" inactiv. */
          className={`btn${running ? '' : ' btn-primary'}`}
          disabled={disabled || running}
          onClick={(e) => run(e, 'start')}
        >
          Porneste
        </button>
        <button
          type="button"
          className={`btn${running ? ' btn-primary' : ''}`}
          disabled={disabled || !running}
          onClick={(e) => run(e, 'stop')}
        >
          Opreste
        </button>
      </div>

      {inst.last_error && <div className="instance-error">{inst.last_error}</div>}
    </div>
  )
}
