import { useCallback, useEffect, useRef, useState } from 'react'

/** Cate evenimente pastram in memorie in browser (feed-ul e infinit altfel). */
const MAX_EVENTS = 600
/** Backoff pentru reconectarea WebSocket, in ms. */
const BACKOFF_MIN = 1000
const BACKOFF_MAX = 15000
/** Intervalul de ping ca sa detectam conexiuni moarte (proxy-uri, tunel, mobil). */
const PING_INTERVAL = 20000

/**
 * Token-ul de dashboard (optional, doar daca backend-ul are DASHBOARD_TOKEN setat).
 * Se preia o singura data din URL (`?token=...`), se pastreaza in sessionStorage
 * si se sterge din bara de adrese ca sa nu ramana in istoric sau in capturi de ecran.
 */
function readToken() {
  const KEY = 'pumpmonitor.token'
  try {
    const params = new URLSearchParams(window.location.search)
    const fromUrl = params.get('token')
    if (fromUrl) {
      sessionStorage.setItem(KEY, fromUrl)
      params.delete('token')
      const rest = params.toString()
      window.history.replaceState(
        {},
        '',
        window.location.pathname + (rest ? `?${rest}` : '') + window.location.hash,
      )
      return fromUrl
    }
    return sessionStorage.getItem(KEY) || ''
  } catch {
    // sessionStorage poate fi blocat (mod privat, iframe restrictionat)
    return new URLSearchParams(window.location.search).get('token') || ''
  }
}

const TOKEN = typeof window !== 'undefined' ? readToken() : ''

function wsUrl() {
  const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  const qs = TOKEN ? `?token=${encodeURIComponent(TOKEN)}` : ''
  return `${proto}//${window.location.host}/ws${qs}`
}

/** Elimina duplicatele dupa event_id si pastreaza ordinea descrescatoare dupa ts. */
function mergeEvents(prev, incoming) {
  if (!incoming?.length) return prev
  const seen = new Set(prev.map((e) => e.event_id))
  const fresh = incoming.filter((e) => e?.event_id && !seen.has(e.event_id))
  if (!fresh.length) return prev
  return [...fresh, ...prev].sort((a, b) => b.ts - a.ts).slice(0, MAX_EVENTS)
}

/**
 * Conexiunea live cu backend-ul.
 * Se reconecteaza singura, tine evenimentele si lista de instante.
 */
export function useBackend() {
  const [status, setStatus] = useState('connecting') // connecting | online | offline
  const [events, setEvents] = useState([])
  const [instances, setInstances] = useState([])
  const [server, setServer] = useState(null)
  const [stats, setStats] = useState(null)
  const [lastMessageAt, setLastMessageAt] = useState(null)

  const socketRef = useRef(null)
  const retryRef = useRef(BACKOFF_MIN)
  const timerRef = useRef(null)
  const pingRef = useRef(null)
  const closedByUs = useRef(false)

  const connect = useCallback(() => {
    if (closedByUs.current) return
    clearTimeout(timerRef.current)

    let ws
    try {
      ws = new WebSocket(wsUrl())
    } catch (err) {
      // URL invalid sau browser care blocheaza ws:// din pagina https://
      console.error('[ws] nu pot deschide conexiunea', err)
      setStatus('offline')
      timerRef.current = setTimeout(connect, retryRef.current)
      retryRef.current = Math.min(retryRef.current * 2, BACKOFF_MAX)
      return
    }
    socketRef.current = ws

    /**
     * Ignoram evenimentele venite de la un socket care nu mai e cel curent.
     * In dev, StrictMode monteaza componenta de doua ori: primul socket e
     * abandonat inca din starea CONNECTING, iar `onclose`-ul lui soseste mai
     * tarziu — fara garda asta ar opri ping-ul socketului bun, ar marca
     * dashboardul "offline" si ar deschide o a treia conexiune.
     */
    const isStale = () => socketRef.current !== ws

    ws.onopen = () => {
      if (isStale()) {
        ws.close()
        return
      }
      setStatus('online')
      retryRef.current = BACKOFF_MIN
      clearInterval(pingRef.current)
      pingRef.current = setInterval(() => {
        if (ws.readyState === WebSocket.OPEN) ws.send('ping')
      }, PING_INTERVAL)
    }

    ws.onmessage = (msg) => {
      if (isStale()) return
      let payload
      try {
        payload = JSON.parse(msg.data)
      } catch {
        return // mesaj corupt — il ignoram, nu stricam UI-ul
      }
      setLastMessageAt(Date.now())

      switch (payload.type) {
        case 'snapshot':
          setEvents(mergeEvents([], payload.events || []))
          setInstances(payload.instances || [])
          setServer(payload.server || null)
          break
        case 'events':
          setEvents((prev) => mergeEvents(prev, payload.events))
          break
        case 'instances':
          setInstances(payload.instances || [])
          break
        case 'pong':
          break
        default:
          break
      }
    }

    ws.onclose = (event) => {
      // Socket vechi (abandonat de StrictMode sau de o reconectare): nu are voie
      // sa atinga starea sau temporizatoarele conexiunii curente.
      if (isStale()) return
      clearInterval(pingRef.current)
      if (closedByUs.current) return

      // 1008 = token respins. Reconectarea ar bucla degeaba.
      if (event?.code === 1008) {
        setStatus('unauthorized')
        return
      }
      setStatus('offline')
      timerRef.current = setTimeout(connect, retryRef.current)
      retryRef.current = Math.min(retryRef.current * 2, BACKOFF_MAX)
    }

    ws.onerror = () => {
      // `onclose` se declanseaza oricum dupa `onerror`; acolo tratam reconectarea.
      if (isStale()) return
      setStatus('offline')
    }
  }, [])

  useEffect(() => {
    closedByUs.current = false
    connect()
    return () => {
      closedByUs.current = true
      clearTimeout(timerRef.current)
      clearInterval(pingRef.current)
      const ws = socketRef.current
      // Marcam intai ca nu mai avem socket curent, ca `onclose`-ul lui sa fie
      // recunoscut drept invechit si sa nu declanseze o reconectare.
      socketRef.current = null
      ws?.close()
    }
  }, [connect])

  /** Apel REST cu tratare uniforma a erorilor. */
  const call = useCallback(async (path, options = {}) => {
    const res = await fetch(path, {
      ...options,
      headers: {
        'content-type': 'application/json',
        ...(TOKEN ? { 'x-dashboard-token': TOKEN } : {}),
        ...(options.headers || {}),
      },
    })
    let body = null
    try {
      body = await res.json()
    } catch {
      body = null
    }
    if (!res.ok) {
      if (res.status === 401) {
        throw new Error('token de dashboard invalid sau lipsa (adauga ?token=... in URL)')
      }
      const detail = body?.detail
      throw new Error(typeof detail === 'string' ? detail : `HTTP ${res.status}`)
    }
    return body
  }, [])

  const refreshInstances = useCallback(async () => {
    const data = await call('/api/instances')
    setInstances(data.instances || [])
    setServer(data.server || null)
    return data
  }, [call])

  /**
   * Agregatele vin din SQLite, nu din bufferul browserului: altfel numerele
   * ar depinde de cat timp e tinuta pagina deschisa.
   */
  const refreshStats = useCallback(
    async (botId) => {
      const qs = botId ? `?bot_id=${encodeURIComponent(botId)}` : ''
      const data = await call(`/api/stats${qs}`)
      setStats(data.stats || null)
      if (data.server) setServer(data.server)
      return data
    },
    [call],
  )

  const action = useCallback(
    async (botId, what) => {
      const data = await call(`/api/instances/${encodeURIComponent(botId)}/${what}`, { method: 'POST' })
      await refreshInstances()
      return data
    },
    [call, refreshInstances],
  )

  return {
    status, events, instances, server, stats, lastMessageAt,
    action, refreshInstances, refreshStats, call,
  }
}
