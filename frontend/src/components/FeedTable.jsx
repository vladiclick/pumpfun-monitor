import { memo } from 'react'
import TokenCard from './TokenCard'

/** Un card este "proaspat" (marcat cu accent) daca a intrat in ultimele 8 secunde. */
const FRESH_MS = 8000

export default memo(function FeedTable({ rows, selectedId, onSelect, now }) {
  if (rows.length === 0) {
    return (
      <div className="empty-state">
        <div className="empty-title">Niciun token de afisat</div>
        <div className="empty-text">Porneste o instanta sau schimba filtrele de mai sus.</div>
      </div>
    )
  }

  return (
    <div className="cards">
      {rows.map((ev) => (
        <TokenCard
          key={ev.event_id}
          ev={ev}
          selected={ev.event_id === selectedId}
          fresh={now - ev.ts < FRESH_MS}
          onSelect={onSelect}
        />
      ))}
    </div>
  )
})
