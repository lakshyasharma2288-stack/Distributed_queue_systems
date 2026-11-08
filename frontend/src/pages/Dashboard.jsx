import JobsTable from "../components/JobsTable.jsx";
import StatCard from "../components/StatCard.jsx";

const statDefinitions = [
  { key: "total", label: "Total jobs", tone: "total" },
  { key: "pending", label: "Pending", tone: "pending" },
  { key: "processing", label: "Processing", tone: "processing" },
  { key: "completed", label: "Completed", tone: "completed" },
  { key: "failed", label: "Failed", tone: "failed" },
];

export default function Dashboard({
  jobs,
  stats,
  onViewDetails,
  onRefresh,
  loading,
  refreshing,
  error,
}) {
  return (
    <div className="page-stack">
      <section className="stats-grid" aria-label="Job statistics">
        {statDefinitions.map((stat) => (
          <StatCard
            key={stat.key}
            label={stat.label}
            value={stats[stat.key]}
            tone={stat.tone}
          />
        ))}
      </section>

      <section className="panel">
        <div className="panel-heading">
          <div>
            <h2 className="panel-title">Recent jobs</h2>
            <p className="panel-description">
              The latest activity across your queue.
            </p>
          </div>
          <button
            type="button"
            className="button button--secondary"
            onClick={onRefresh}
            disabled={refreshing}
          >
            <RefreshIcon />
            {refreshing ? "Refreshing…" : "Refresh"}
          </button>
        </div>
        {!loading && !error && jobs.length === 0 ? (
          <div className="dashboard-empty-state" role="status">
            <div className="empty-state-icon" aria-hidden="true">
              <svg viewBox="0 0 24 24">
                <path d="M5 6.5h14M5 12h9M5 17.5h6" />
                <circle cx="18" cy="17.5" r="2" />
              </svg>
            </div>
            <p className="empty-state-title">No jobs yet</p>
            <p className="empty-state-copy">
              Jobs you submit will appear here with their latest status.
            </p>
          </div>
        ) : (
          <JobsTable
            jobs={jobs}
            onViewDetails={onViewDetails}
            loading={loading}
            error={error}
          />
        )}
      </section>
    </div>
  );
}

export function RefreshIcon() {
  return (
    <svg className="button-icon" viewBox="0 0 20 20" aria-hidden="true">
      <path d="M16.5 9.2A6.6 6.6 0 0 0 5.1 5.1L3.5 6.8M3.5 6.8V3.9m0 2.9h2.9M3.5 10.8a6.6 6.6 0 0 0 11.4 4.1l1.6-1.7m0 0v2.9m0-2.9h-2.9" />
    </svg>
  );
}
