import StatusBadge from "./StatusBadge.jsx";

const dateFormatter = new Intl.DateTimeFormat(undefined, {
  dateStyle: "medium",
  timeStyle: "short",
});

export function formatDate(value) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "—" : dateFormatter.format(date);
}

export function priorityLabel(value) {
  return value === 0 ? "Default (0)" : `Priority ${value}`;
}

export default function JobsTable({ jobs, onViewDetails, loading, error }) {
  if (loading) {
    return <div className="table-state" role="status">Loading jobs…</div>;
  }

  if (jobs.length === 0) {
    return (
      <div className="empty-state">
        <div className="empty-state-icon" aria-hidden="true">
          <svg viewBox="0 0 24 24">
            <circle cx="10.8" cy="10.8" r="6.8" />
            <path d="m16 16 4 4M8 11h5.5" />
          </svg>
        </div>
        <p className="empty-state-title">
          {error ? "Jobs could not be loaded" : "No jobs found"}
        </p>
        <p className="empty-state-copy">
          {error
            ? "Check the API connection and try again."
            : "Try adjusting your search or status filter."}
        </p>
      </div>
    );
  }

  return (
    <div className="table-scroll">
      <table className="jobs-table">
        <thead>
          <tr>
            <th scope="col">Job ID</th>
            <th scope="col">Task type</th>
            <th scope="col">Status</th>
            <th scope="col">Priority</th>
            <th scope="col">Created at</th>
            <th scope="col">
              <span className="visually-hidden">Actions</span>
            </th>
          </tr>
        </thead>
        <tbody>
          {jobs.map((job) => (
            <tr key={job.id}>
              <td>
                <span className="job-id">{job.id}</span>
              </td>
              <td>
                <span className="task-name">{job.task_type}</span>
              </td>
              <td>
                <StatusBadge status={job.status} />
              </td>
              <td>
                <span className="priority-label">
                  <span className="priority-dot" aria-hidden="true" />
                  {priorityLabel(job.priority)}
                </span>
              </td>
              <td className="date-cell">{formatDate(job.created_at)}</td>
              <td className="action-cell">
                <button
                  type="button"
                  className="button button--text"
                  onClick={() => onViewDetails(job)}
                  aria-label={`View details for ${job.id}`}
                >
                  View details
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
