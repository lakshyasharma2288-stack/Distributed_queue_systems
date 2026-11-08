const navigationItems = [
  { id: "dashboard", label: "Dashboard", icon: "grid" },
  { id: "jobs", label: "Jobs", icon: "list" },
  { id: "submit", label: "Submit Job", icon: "plus" },
  { id: "images", label: "Images", icon: "grid" },
];

function NavigationIcon({ name }) {
  if (name === "grid") {
    return (
      <svg viewBox="0 0 20 20" aria-hidden="true">
        <rect x="3" y="3" width="5" height="5" rx="1" />
        <rect x="12" y="3" width="5" height="5" rx="1" />
        <rect x="3" y="12" width="5" height="5" rx="1" />
        <rect x="12" y="12" width="5" height="5" rx="1" />
      </svg>
    );
  }
  if (name === "list") {
    return (
      <svg viewBox="0 0 20 20" aria-hidden="true">
        <path d="M7 5h10M7 10h10M7 15h10" />
        <circle cx="3.5" cy="5" r=".7" />
        <circle cx="3.5" cy="10" r=".7" />
        <circle cx="3.5" cy="15" r=".7" />
      </svg>
    );
  }
  return (
    <svg viewBox="0 0 20 20" aria-hidden="true">
      <path d="M10 4v12M4 10h12" />
    </svg>
  );
}

export default function Sidebar({ activePage, onNavigate }) {
  return (
    <aside className="sidebar">
      <div className="brand">
        <div className="brand-mark" aria-hidden="true">
          <span />
          <span />
          <span />
        </div>
        <div>
          <p className="brand-name">Distributed Queue</p>
          <p className="brand-caption">Job operations</p>
        </div>
      </div>

      <div className="sidebar-section-label">Workspace</div>
      <nav className="sidebar-nav" aria-label="Main navigation">
        {navigationItems.map((item) => (
          <button
            className={`nav-item ${activePage === item.id ? "is-active" : ""}`}
            key={item.id}
            type="button"
            aria-current={activePage === item.id ? "page" : undefined}
            onClick={() => onNavigate(item.id)}
          >
            <NavigationIcon name={item.icon} />
            <span>{item.label}</span>
          </button>
        ))}
      </nav>

      <div className="sidebar-footer">
        <span className="connection-indicator" />
        <span>FastAPI integration</span>
      </div>
    </aside>
  );
}
