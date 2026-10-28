import { useState } from 'react';
import { Building2, CircleHelp, House, Menu, Settings, X } from 'lucide-react';

const NAV = [
  { id: 'top', label: 'Home', icon: House },
  { id: 'step-upload', label: 'Settings', icon: Settings },
  { id: 'help', label: 'Help', icon: CircleHelp },
];

function scrollTo(id: string) {
  if (id === 'top') window.scrollTo({ top: 0, behavior: 'smooth' });
  else document.getElementById(id)?.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

export function Sidebar() {
  const [active, setActive] = useState('top');
  const [open, setOpen] = useState(false);

  return (
    <aside className={`sidebar${open ? ' open' : ''}`}>
      <div className="sidebar-logo">
        <img src="/dim-logo.png" alt="Durga Infra Mining Pvt Ltd" />
        <button type="button" className="sidebar-toggle" aria-label="Menu" onClick={() => setOpen(!open)}>
          {open ? <X size={22} /> : <Menu size={22} />}
        </button>
      </div>
      <nav className="sidebar-nav">
        {NAV.map(({ id, label, icon: Icon }) => (
          <button
            key={id}
            type="button"
            className={`nav-item${active === id ? ' active' : ''}`}
            onClick={() => {
              setActive(id);
              setOpen(false);
              scrollTo(id);
            }}
          >
            <Icon size={20} />
            {label}
          </button>
        ))}
      </nav>
      <div className="sidebar-foot">
        <div className="sidebar-art" />
        <p>
          Building Tomorrow
          <br />
          With Resources
        </p>
      </div>
    </aside>
  );
}

export function Topbar({ partyName }: { partyName: string | null }) {
  return (
    <header className="topbar">
      <div>
        <h1 className="topbar-title">Ledger Reconciliation</h1>
        <div className="topbar-sub">Match · Reconcile · Report</div>
      </div>
      <div className="topbar-right">
        <div className="company-chip">
          <span className="company-avatar">
            <Building2 size={20} />
          </span>
          <span>
            <strong>{partyName ?? 'Durga Infra Mining Pvt Ltd'}</strong>
            <small>{partyName ? 'Party' : 'Our company'}</small>
          </span>
        </div>
      </div>
    </header>
  );
}

export function Hero() {
  return (
    <section className="hero">
      <div className="hero-text">
        <h2>Ledger Reconciliation</h2>
        <p>Ensure accurate records. Build financial confidence.</p>
      </div>
    </section>
  );
}
