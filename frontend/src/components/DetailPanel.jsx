import { useEffect, useRef, useState } from 'react'
import { latency, usd } from '../lib/format'

const VERDICT_LABEL = { PASS: 'TRECUT', FAIL: 'RESPINS', PENDING: 'IN ASTEPTARE', ERROR: 'NEDECIS' }
const VERDICT_CLASS = { PASS: 'pass', FAIL: 'reject' }

/** Foaia de detalii pentru evenimentul selectat din feed. */
export default function DetailPanel({ event, onClose }) {
  if (!event) return null
  const checks = event.checks || {}

  return (
    <section className="detail-sheet">
      <div className="detail-head">
        <div className="detail-title">{event.symbol || event.name || 'Detalii'}</div>
        <span className={`verdict-badge${VERDICT_CLASS[event.verdict] ? ' ' + VERDICT_CLASS[event.verdict] : ''}`}>
          {VERDICT_LABEL[event.verdict] || event.verdict}
        </span>
        <button type="button" className="detail-close" onClick={onClose} aria-label="Inchide">
          ×
        </button>
      </div>

      <div className="detail-sections">
        <Section title="Token">
          <Row k="Nume" v={event.name || '—'} />
          <Row k="Simbol" v={event.symbol || '—'} />
          <Row k="Mint" v={event.mint} mono copy />
          <Row k="Creator" v={event.creator} mono copy />
          <Row k="Semnatura" v={event.signature} mono copy />
          {event.mint && (
            <div className="link-row">
              {/* Phantom primul: pe telefon linkul deschide direct aplicatia. */}
              <a
                className="link-chip"
                href={`https://phantom.com/tokens/solana/${event.mint}`}
                target="_blank"
                rel="noreferrer"
              >
                Phantom
              </a>
              <a className="link-chip" href={`https://pump.fun/coin/${event.mint}`} target="_blank" rel="noreferrer">
                pump.fun
              </a>
              <a className="link-chip" href={`https://solscan.io/token/${event.mint}`} target="_blank" rel="noreferrer">
                Solscan
              </a>
              <a
                className="link-chip"
                href={`https://dexscreener.com/solana/${event.mint}`}
                target="_blank"
                rel="noreferrer"
              >
                DexScreener
              </a>
              <a
                className="link-chip"
                href={`https://rugcheck.xyz/tokens/${event.mint}`}
                target="_blank"
                rel="noreferrer"
              >
                RugCheck
              </a>
            </div>
          )}
        </Section>

        {checks.authority && (
          <Section title="Autoritati">
            <Row
              k="Poate emite in plus"
              v={checks.authority.mint_authority ? 'Da' : 'Nu'}
              tone={checks.authority.mint_authority ? 'bad' : 'good'}
            />
            <Row
              k="Poate ingheta"
              v={checks.authority.freeze_authority ? 'Da' : 'Nu'}
              tone={checks.authority.freeze_authority ? 'bad' : 'good'}
            />
            <Row k="Decimale" v={checks.authority.decimals ?? '—'} />
            <Row k="Program" v={checks.authority.token_program || '—'} mono />
          </Section>
        )}

        {checks.liquidity && (
          <Section title="Lichiditate">
            <Row
              k="Lichiditate"
              v={
                checks.liquidity.liquidity_known === true
                  ? usd(checks.liquidity.liquidity_usd)
                  : 'Necunoscuta (token prea nou)'
              }
            />
            <Row k="DEX" v={checks.liquidity.dex || '—'} />
            <Row k="FDV" v={usd(checks.liquidity.fdv)} />
            <Row k="Volum 24h" v={usd(checks.liquidity.volume_h24)} />
          </Section>
        )}

        {checks.rugcheck && (
          <Section title="Risc">
            <Row
              k="Scor"
              v={checks.rugcheck.no_report ? 'Fara raport inca' : (checks.rugcheck.rugcheck_score ?? '—')}
            />
            <Row k="Semnale" v={(checks.rugcheck.risks || []).join(', ') || 'niciunul'} />
          </Section>
        )}

        <Section title="Verificare">
          <Row k="Verdict" v={VERDICT_LABEL[event.verdict] || event.verdict || '—'} />
          <Row k="Motiv" v={event.reason || '—'} />
          <Row k="Instanta" v={event.bot_id} />
          <Row k="Timp pana la verdict" v={latency(event.latency_ms)} />
        </Section>
      </div>
    </section>
  )
}

function Section({ title, children }) {
  return (
    <div className="detail-section">
      <div className="detail-section-title">{title}</div>
      {children}
    </div>
  )
}

function Row({ k, v, mono, tone, copy }) {
  const [copied, setCopied] = useState(false)
  const timerRef = useRef(null)

  // Randul poate disparea inainte sa expire mesajul "copiat".
  useEffect(() => () => clearTimeout(timerRef.current), [])

  if (v === undefined || v === null || v === '') return null

  async function doCopy() {
    try {
      await navigator.clipboard.writeText(String(v))
      setCopied(true)
      clearTimeout(timerRef.current)
      timerRef.current = setTimeout(() => setCopied(false), 1200)
    } catch {
      // clipboard indisponibil (http fara localhost) - nu e critic
      setCopied(false)
    }
  }

  return (
    <div className="kv-row">
      <div className="kv-key">{k}</div>
      <div className={`kv-value${mono ? ' mono' : ''}${tone ? ' ' + tone : ''}`}>
        {String(v)}
        {copy && (
          <button type="button" className="copy-btn" onClick={doCopy}>
            {copied ? 'copiat' : 'copiaza'}
          </button>
        )}
      </div>
    </div>
  )
}
