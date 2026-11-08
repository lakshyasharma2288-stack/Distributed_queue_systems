import JobsTable from "../components/JobsTable.jsx";
import { RefreshIcon } from "./Dashboard.jsx";

const statuses = [
  "all",
  "pending",
  "processing",
  "retry_scheduled",
  "completed",
  "failed",
];

export default function Jobs({
  jobs,
  searchTerm,
  statusFilter,
  onSearchChange,
  onStatusChange,
  onViewDetails,
  onRefresh,
  loading,
  refreshing,
  error,
}) {
  return (
    <section className="panel">
      <div className="panel-heading panel-heading--jobs">
        <div>
          <h2 className="panel-title">All jobs</h2>
          <p className="panel-description">
            Search and review every job in the queue.
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

      <div className="table-toolbar">
        <label className="search-field">
          <span className="visually-hidden">Search by job ID</span>
          <svg viewBox="0 0 20 20" aria-hidden="true">
            <circle cx="8.8" cy="8.8" r="5.8" />
            <path d="m13.2 13.2 4 4" />
          </svg>
          <input
            type="search"
            value={searchTerm}
            onChange={(event) => onSearchChange(event.target.value)}
            placeholder="Search by job ID"
          />
        </label>

        <label className="filter-field">
          <span>Status</span>
          <select
            value={statusFilter}
            onChange={(event) => onStatusChange(event.target.value)}
          >
            {statuses.map((status) => (
              <option key={status} value={status}>
                {status === "all"
                  ? "All statuses"
                  : status === "retry_scheduled"
                    ? "Retry scheduled"
                    : status[0].toUpperCase() + status.slice(1)}
              </option>
            ))}
          </select>
        </label>
      </div>

      <JobsTable
        jobs={jobs}
        onViewDetails={onViewDetails}
        loading={loading}
        error={error}
      />
    </section>
  );
}
