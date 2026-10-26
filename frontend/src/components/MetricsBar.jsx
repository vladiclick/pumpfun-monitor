import { compact, latency } from '../lib/format'

/**
 * Banda cu cele patru cifre esentiale ale monitorizarii.
 * Calculul se face in useMemo la nivelul App, ca sa nu refacem la fiecare render.
 */
export default function MetricsBar({ metrics }) {
  const items = [
    {
      label: 'Token-uri scanate',
      value: compact(metrics.detected),
      title: 'Numarul total de token-uri noi detectate de bot.',
    },
    {
      label: 'Au trecut',
      value: compact(metrics.passed),
      accent: true,
      title: 'Token-uri care au trecut toate verificarile de siguranta.',
    },
    {
      label: 'Respinse',
      value: compact(metrics.failed),
      title: 'Token-uri respinse de verificarile de siguranta.',
    },
    {
      label: 'Timp pana la verdict',
      value: latency(metrics.medianLatency),
      title: 'Mediana timpului de la detectarea tokenului pana la verdict.',
    },
  ]

  return (
    <div className="summary">
      {items.map((it) => (
        <div className="summary-item" key={it.label} title={it.title}>
          <div className="summary-label">{it.label}</div>
          <div className={`summary-value${it.accent ? ' accent' : ''}`}>{it.value}</div>
        </div>
      ))}
    </div>
  )
}
