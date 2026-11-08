import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import JobDetailsModal from "./components/JobDetailsModal.jsx";
import Sidebar from "./components/Sidebar.jsx";
import Dashboard from "./pages/Dashboard.jsx";
import Images from "./pages/Images.jsx";
import Jobs from "./pages/Jobs.jsx";
import SubmitJob from "./pages/SubmitJob.jsx";
import { createJob, getJob, getJobs } from "./services/jobService.js";

const pageDetails = {
  dashboard: {
    eyebrow: "Overview",
    title: "Dashboard",
    description: "Monitor job activity and queue health at a glance.",
  },
  jobs: {
    eyebrow: "Queue",
    title: "Jobs",
    description: "Search, filter, and inspect jobs in your queue.",
  },
  submit: {
    eyebrow: "New work",
    title: "Submit job",
    description: "Add a task to the distributed job queue.",
  },
  images: {
    eyebrow: "Storage",
    title: "Images",
    description: "Browse images uploaded for resize jobs.",
  },
};

function sortNewestFirst(jobs) {
  return [...jobs].sort(
    (first, second) =>
      new Date(second.created_at).getTime() -
      new Date(first.created_at).getTime(),
  );
}

export default function App() {
  const [activePage, setActivePage] = useState("dashboard");
  const [jobs, setJobs] = useState([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [dataError, setDataError] = useState("");
  const [selectedJob, setSelectedJob] = useState(null);
  const [detailsLoading, setDetailsLoading] = useState(false);
  const [detailsError, setDetailsError] = useState("");
  const [searchTerm, setSearchTerm] = useState("");
  const [statusFilter, setStatusFilter] = useState("all");
  const [taskType, setTaskType] = useState("");
  const [formError, setFormError] = useState("");
  const [successMessage, setSuccessMessage] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const requestInFlight = useRef(null);
  const submitInFlight = useRef(false);

  const loadJobs = useCallback(async (manual = false) => {
    if (requestInFlight.current) {
      if (manual) setRefreshing(true);
      return requestInFlight.current;
    }
    if (manual) setRefreshing(true);
    const pendingRequest = getJobs()
      .then((currentJobs) => {
        setJobs(currentJobs);
        setDataError("");
        setSelectedJob((current) =>
          current
            ? currentJobs.find((job) => job.id === current.id) ?? current
            : null,
        );
        return currentJobs;
      })
      .catch((error) => {
        setDataError(error.message || "Could not load jobs.");
        return null;
      })
      .finally(() => {
        requestInFlight.current = null;
        setLoading(false);
        setRefreshing(false);
      });
    requestInFlight.current = pendingRequest;
    return pendingRequest;
  }, []);

  useEffect(() => {
    void loadJobs();
  }, [loadJobs]);

  useEffect(() => {
    if (activePage === "submit") return undefined;
    const intervalId = window.setInterval(() => {
      if (document.visibilityState === "visible") void loadJobs();
    }, 5000);
    return () => window.clearInterval(intervalId);
  }, [activePage, loadJobs]);

  const sortedJobs = useMemo(() => sortNewestFirst(jobs), [jobs]);
  const stats = useMemo(
    () => ({
      total: jobs.length,
      pending: jobs.filter(
        (job) => job.status === "pending" || job.status === "retry_scheduled",
      ).length,
      processing: jobs.filter((job) => job.status === "processing").length,
      completed: jobs.filter((job) => job.status === "completed").length,
      failed: jobs.filter((job) => job.status === "failed").length,
    }),
    [jobs],
  );
  const filteredJobs = useMemo(() => {
    const query = searchTerm.trim().toLowerCase();
    return sortedJobs.filter((job) => {
      const matchesId = !query || job.id.toLowerCase().includes(query);
      const matchesStatus =
        statusFilter === "all" || job.status === statusFilter;
      return matchesId && matchesStatus;
    });
  }, [searchTerm, sortedJobs, statusFilter]);
  const page = pageDetails[activePage];

  async function handleViewDetails(job) {
    setSelectedJob(job);
    setDetailsLoading(true);
    setDetailsError("");
    try {
      const refreshedJob = await getJob(job.id);
      setSelectedJob((current) =>
        current?.id === job.id ? refreshedJob : current,
      );
    } catch (error) {
      setDetailsError(error.message || "Could not load job details.");
    } finally {
      setDetailsLoading(false);
    }
  }

  function handleViewCreatedJob(jobId) {
    setActivePage("jobs");
    setSearchTerm(jobId);
    setStatusFilter("all");
    void handleViewDetails({ id: jobId });
  }

  function updateTaskType(value) {
    setTaskType(value);
    setFormError("");
    setSuccessMessage("");
  }

  function clearFormFeedback() {
    setFormError("");
    setSuccessMessage("");
  }

  async function handleSubmit(submittedTaskType, payload) {
    if (submitInFlight.current) {
      return {
        ok: false,
        error: "A job submission is already in progress.",
      };
    }
    setFormError("");
    setSuccessMessage("");

    if (!submittedTaskType) {
      const error = "Select a task type before submitting.";
      setFormError(error);
      return { ok: false, error };
    }

    submitInFlight.current = true;
    setSubmitting(true);
    try {
      const idempotencyKey = globalThis.crypto?.randomUUID?.();
      const createdJob = await createJob(
        submittedTaskType,
        payload,
        idempotencyKey,
      );
      const now = new Date().toISOString();
      const optimisticJob = {
        ...createdJob,
        task_type: submittedTaskType,
        payload,
        priority: 0,
        attempts: 0,
        max_attempts: null,
        created_at: now,
        updated_at: now,
        started_at: null,
        completed_at: null,
        result: null,
        error_message: null,
      };
      setJobs((currentJobs) => [optimisticJob, ...currentJobs]);
      setSuccessMessage(
        `Job ${createdJob.id} was created with status ${createdJob.status}.`,
      );
      setTaskType("");
      const currentJobs = await loadJobs();
      if (currentJobs && !currentJobs.some((job) => job.id === createdJob.id)) {
        setJobs((current) =>
          current.some((job) => job.id === createdJob.id)
            ? current
            : sortNewestFirst([optimisticJob, ...current]),
        );
      }
      return { ok: true, job: createdJob };
    } catch (error) {
      const message = error.message || "Could not submit the job.";
      setFormError(message);
      return { ok: false, error: message };
    } finally {
      submitInFlight.current = false;
      setSubmitting(false);
    }
  }

  let pageContent;
  if (activePage === "jobs") {
    pageContent = (
      <Jobs
        jobs={filteredJobs}
        searchTerm={searchTerm}
        statusFilter={statusFilter}
        onSearchChange={setSearchTerm}
        onStatusChange={setStatusFilter}
        onViewDetails={handleViewDetails}
        onRefresh={() => loadJobs(true)}
        loading={loading}
        refreshing={refreshing}
        error={dataError}
      />
    );
  } else if (activePage === "images") {
    pageContent = <Images />;
  } else if (activePage === "submit") {
    pageContent = (
      <SubmitJob
        taskType={taskType}
        formError={formError}
        successMessage={successMessage}
        submitting={submitting}
        onTaskTypeChange={updateTaskType}
        onClearFeedback={clearFormFeedback}
        onSubmit={handleSubmit}
        onViewJob={handleViewCreatedJob}
      />
    );
  } else {
    pageContent = (
      <Dashboard
        jobs={sortedJobs.slice(0, 5)}
        stats={stats}
        onViewDetails={handleViewDetails}
        onRefresh={() => loadJobs(true)}
        loading={loading}
        refreshing={refreshing}
        error={dataError}
      />
    );
  }

  return (
    <div className="app-shell">
      <Sidebar activePage={activePage} onNavigate={setActivePage} />
      <main className="main-content">
        <header className="page-header">
          <div>
            <p className="eyebrow">{page.eyebrow}</p>
            <h1 className="page-title">{page.title}</h1>
            <p className="page-description">{page.description}</p>
          </div>
          <div className="environment-pill">
            <span className="environment-dot" />
            FastAPI
          </div>
        </header>
        {dataError && (
          <div className="error-banner" role="alert">
            <span>{dataError}</span>
            <button
              type="button"
              className="button button--secondary"
              onClick={() => loadJobs(true)}
              disabled={refreshing}
            >
              Retry
            </button>
          </div>
        )}
        {pageContent}
      </main>
      <JobDetailsModal
        job={selectedJob}
        loading={detailsLoading}
        error={detailsError}
        onClose={() => setSelectedJob(null)}
      />
    </div>
  );
}
