const statStyles = {
  total: "stat-card--total",
  pending: "stat-card--pending",
  processing: "stat-card--processing",
  completed: "stat-card--completed",
  failed: "stat-card--failed",
};

export default function StatCard({ label, value, tone }) {
  return (
    <article className={`stat-card ${statStyles[tone] ?? ""}`}>
      <div className="stat-card-top">
        <span className="stat-label">{label}</span>
        <span className="stat-dot" aria-hidden="true" />
      </div>
      <p className="stat-value">{value}</p>
    </article>
  );
}
