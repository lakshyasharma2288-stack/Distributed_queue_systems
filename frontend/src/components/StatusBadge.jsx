const statusLabels = {
  pending: "Pending",
  processing: "Processing",
  retry_scheduled: "Retry scheduled",
  completed: "Completed",
  failed: "Failed",
};

export default function StatusBadge({ status }) {
  return (
    <span className={`status-badge status-badge--${status}`}>
      <span className="status-dot" aria-hidden="true" />
      {statusLabels[status] ?? status}
    </span>
  );
}
