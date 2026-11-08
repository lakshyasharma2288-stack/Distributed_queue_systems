import { useEffect } from "react";
import StatusBadge from "./StatusBadge.jsx";
import { formatDate, priorityLabel } from "./JobsTable.jsx";
import { jobOutputUrl } from "../services/jobService.js";

export default function JobDetailsModal({ job, loading, error, onClose }) {
  useEffect(() => {
    function handleKeyDown(event) {
      if (event.key === "Escape") onClose();
    }

    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [onClose]);

  if (!job) return null;

  return (
    <div
      className="modal-backdrop"
      role="presentation"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <section
        className="details-modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="job-details-title"
      >
        <header className="modal-header">
          <div>
            <p className="eyebrow">Job details</p>
            <h2 id="job-details-title" className="modal-title">
              {job.id}
            </h2>
          </div>
          <button
            type="button"
            className="icon-button"
            aria-label="Close job details"
            onClick={onClose}
          >
            <svg viewBox="0 0 20 20" aria-hidden="true">
              <path d="m5 5 10 10M15 5 5 15" />
            </svg>
          </button>
        </header>

        <div className="modal-body">
          <div className="detail-grid">
            <div className="detail-item">
              <span className="detail-label">Task type</span>
              <span className="detail-value">{job.task_type}</span>
            </div>
            <div className="detail-item">
              <span className="detail-label">Status</span>
              <StatusBadge status={job.status} />
            </div>
            <div className="detail-item">
              <span className="detail-label">Priority</span>
              <span className="detail-value">
                {priorityLabel(job.priority)}
              </span>
            </div>
            <div className="detail-item">
              <span className="detail-label">Attempts</span>
              <span className="detail-value">
                {job.max_attempts == null
                  ? job.attempts
                  : `${job.attempts} / ${job.max_attempts}`}
              </span>
            </div>
            <div className="detail-item">
              <span className="detail-label">Created at</span>
              <span className="detail-value">{formatDate(job.created_at)}</span>
            </div>
            <div className="detail-item">
              <span className="detail-label">Completed at</span>
              <span className="detail-value">
                {formatDate(job.completed_at)}
              </span>
            </div>
          </div>

          {loading && (
            <div className="table-state" role="status">
              Refreshing job details…
            </div>
          )}
          {error && (
            <div className="error-banner error-banner--modal" role="alert">
              {error}
            </div>
          )}

          <div className="detail-section">
            <h3>Payload</h3>
            <pre className="code-block">
              <code>{JSON.stringify(job.payload, null, 2)}</code>
            </pre>
          </div>

          <div className="detail-section">
            <h3>{job.error_message ? "Error" : "Result"}</h3>
            {job.error_message ? (
              <div className="detail-message detail-message--error">
                {job.error_message}
              </div>
            ) : job.result ? (
              <>
                {job.task_type === "resize_image" &&
                  job.status === "completed" && (
                    <img
                      className="image-preview"
                      src={jobOutputUrl(job.id)}
                      alt="Resized output"
                    />
                  )}
                <pre className="code-block">
                  <code>{JSON.stringify(job.result, null, 2)}</code>
                </pre>
              </>
            ) : (
              <div className="detail-message">No result available yet.</div>
            )}
          </div>
        </div>

        <footer className="modal-footer">
          <button type="button" className="button button--secondary" onClick={onClose}>
            Close
          </button>
        </footer>
      </section>
    </div>
  );
}
