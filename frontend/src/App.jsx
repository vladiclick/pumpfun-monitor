import { useEffect, useMemo, useState } from 'react'
import DetailPanel from './components/DetailPanel'
import FeedTable from './components/FeedTable'
import InstancePanel from './components/InstancePanel'
import MetricsBar from './components/MetricsBar'
import TopBar from './components/TopBar'
import { useBackend } from './hooks/useBackend'

/** Sub aceasta latime panoul de instante porneste inchis (spatiu vertical). */
const MOBILE_QUERY = '(max-width: 900px)'

/* Etichete in limbaj obisnuit, nu jargon de terminal. */
const FILTERS = [
  { id: 'verdicts', label: 'Toate verdictele' },
  { id: 'pass', label: 'Doar cele trecute' },
  { id: 'fail', label: 'Doar respinse' },
  { id: 'all', label: 'Tot ce se intampla' },
]

export default function App() {
  const { status, events, instances, server, stats, action, refreshInstances, refreshStats } =
    useBackend()

  const [filter, setFilter] = useState('verdicts')
  const [search, setSearch] = useState('')
  const [selectedBot, setSelectedBot] = useState(null)
  const [selectedEvent, setSelectedEvent] = useState(null)
  const [actionError, setActionError] = useState('')
  const [sidebarOpen, setSidebarOpen] = useState(
    () => !(typeof window !== 'undefined' && window.matchMedia?.(MOBILE_QUERY).matches),
  )
  // `now` avanseaza o data pe secunda: marcam randurile proaspete si uptime-ul.
  const [now, setNow] = useState(Date.now())

  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(t)
  }, [])

  // La rotirea telefonului / redimensionare, revenim la starea potrivita ecranului.
  useEffect(() => {
    if (typeof window === 'undefined' || !window.matchMedia) return undefined
    const mq = window.matchMedia(MOBILE_QUERY)
    const onChange = (e) => setSidebarOpen(!e.matches)
    mq.addEventListener('change', onChange)
    return () => mq.removeEventListener('change', onChange)
  }, [])

  // Statusul instantelor nu vine prin WebSocket decat la actiuni; il reimprospatam periodic.
  useEffect(() => {
    const tick = () => {
      refreshInstances().catch(() => {})
      refreshStats(selectedBot).catch(() => {})
    }
    tick()
    const t = setInterval(tick, 8000)
    return () => clearInterval(t)
  }, [refreshInstances, refreshStats, selectedBot])

  const rows = useMemo(() => {
    const q = search.trim().toLowerCase()
    return events.filter((ev) => {
      if (selectedBot && ev.bot_id !== selectedBot) return false

      if (filter === 'verdicts' && ev.type !== 'verdict') return false
      if (filter === 'pass' && ev.verdict !== 'PASS') return false
      if (filter === 'fail' && !['FAIL', 'ERROR'].includes(ev.verdict)) return false

      if (q) {
        const hay = `${ev.symbol || ''} ${ev.name || ''} ${ev.mint || ''} ${ev.reason || ''} ${ev.message || ''}`
        if (!hay.toLowerCase().includes(q)) return false
      }
      return true
    })
  }, [events, filter, search, selectedBot])

  // Agregatele istorice vin din backend; latenta live o calculam din feed,
  // ca sa se vada imediat efectul unei schimbari de configuratie.
  const metrics = useMemo(
    () => buildMetrics(stats, events, selectedBot),
    [stats, events, selectedBot],
  )

  const detail = useMemo(
    () => events.find((e) => e.event_id === selectedEvent) || null,
    [events, selectedEvent],
  )

  async function handleAction(botId, what) {
    setActionError('')
    try {
      await action(botId, what)
    } catch (err) {
      setActionError(`${what} ${botId}: ${err.message}`)
    }
  }

  return (
    <div className={`app${sidebarOpen ? '' : ' sidebar-collapsed'}`}>
      <TopBar
        status={status}
        server={server}
        sidebarOpen={sidebarOpen}
        onToggleSidebar={() => setSidebarOpen((v) => !v)}
        instanceCount={instances.length}
      />
      <MetricsBar metrics={metrics} />

      <div className="body">
        <InstancePanel
          instances={instances}
          selected={selectedBot}
          onSelect={setSelectedBot}
          onAction={handleAction}
          error={actionError}
        />

        <main className="feed">
          <div className="feed-toolbar">
            {FILTERS.map((f) => (
              <button
                key={f.id}
                className={`chip${filter === f.id ? ' chip-active' : ''}`}
                onClick={() => setFilter(f.id)}
              >
                {f.label}
              </button>
            ))}
            {selectedBot && (
              <button className="chip chip-active" onClick={() => setSelectedBot(null)}>
                {selectedBot} ×
              </button>
            )}
            <input
              className="search-input"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Cauta dupa simbol sau adresa"
              spellCheck={false}
            />
          </div>

          <FeedTable
            rows={rows}
            selectedId={selectedEvent}
            onSelect={setSelectedEvent}
            now={now}
          />

          <DetailPanel event={detail} onClose={() => setSelectedEvent(null)} />
        </main>
      </div>
    </div>
  )
}

/**
 * Metrici afisate sus: totalurile vin din SQLite (stabile, nu depind de cat
 * timp e pagina deschisa), latenta mediana se calculeaza din feed-ul live.
 */
function buildMetrics(stats, events, botId) {
  const scoped = botId ? events.filter((e) => e.bot_id === botId) : events
  const liveVerdicts = scoped.filter((e) => e.type === 'verdict')

  const lats = liveVerdicts
    .map((e) => e.latency_ms)
    .filter((v) => typeof v === 'number' && v >= 0)
    .sort((a, b) => a - b)

  const verdicts = stats?.verdicts ?? liveVerdicts.length
  const passed = stats?.passed ?? liveVerdicts.filter((e) => e.verdict === 'PASS').length

  return {
    detected: stats?.detected ?? scoped.filter((e) => e.type === 'token_detected').length,
    verdicts,
    passed,
    failed: stats?.failed ?? liveVerdicts.filter((e) => e.verdict === 'FAIL').length,
    medianLatency: lats.length ? lats[Math.floor(lats.length / 2)] : (stats?.avg_latency_ms ?? null),
    passRate: verdicts ? (passed / verdicts) * 100 : 0,
  }
}
