const STATUS_LABEL = {
  online: 'Conectat',
  connecting: 'Se conecteaza',
  offline: 'Deconectat',
  unauthorized: 'Token lipsa',
}
const STATUS_DOT = {
  online: 'on',
  connecting: 'wait',
  offline: 'off',
  unauthorized: 'off',
}

export default function TopBar({ status, server, sidebarOpen, onToggleSidebar, instanceCount }) {
  return (
    <header className="topbar">
      <div className="brand">
        <span className="brand-mark" />
        Pump Monitor
        <span className="brand-sub">Solana / pump.fun</span>
      </div>

      {/* Vizibil doar pe ecrane inguste, ca sa deschidem/inchidem panoul de instante. */}
      <button
        type="button"
        className={`sidebar-toggle${sidebarOpen ? ' active' : ''}`}
        onClick={onToggleSidebar}
        aria-expanded={sidebarOpen}
      >
        {`Instante (${instanceCount})`}
      </button>

      <div className="topbar-spacer" />

      <div className="conn-pill" title={STATUS_LABEL[status] || status}>
        <span className={`conn-dot ${STATUS_DOT[status] || ''}`} />
        {/* Pe telefon ramane doar punctul colorat: cuvantul nu incape in bara. */}
        <span className="conn-text">{STATUS_LABEL[status] || status}</span>
      </div>
    </header>
  )
}
