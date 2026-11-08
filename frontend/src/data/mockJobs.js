const minutesAgo = (minutes) =>
  new Date(Date.now() - minutes * 60_000).toISOString();

function completedJob(id, taskType, priority, minutesAgoCreated, payload, data) {
  const createdAt = minutesAgo(minutesAgoCreated);
  const completedAt = minutesAgo(Math.max(minutesAgoCreated - 1, 0));

  return {
    id,
    task_type: taskType,
    payload,
    status: "completed",
    priority,
    attempts: 1,
    max_attempts: 3,
    created_at: createdAt,
    updated_at: completedAt,
    started_at: minutesAgo(Math.max(minutesAgoCreated - 2, 0)),
    completed_at: completedAt,
    result: {
      success: true,
      task_type: taskType,
      data,
    },
    error_message: null,
  };
}

function activeJob(id, taskType, status, priority, minutesAgoCreated, payload) {
  return {
    id,
    task_type: taskType,
    payload,
    status,
    priority,
    attempts: status === "processing" ? 1 : 0,
    max_attempts: 3,
    created_at: minutesAgo(minutesAgoCreated),
    updated_at: minutesAgo(status === "processing" ? 1 : minutesAgoCreated),
    started_at: status === "processing" ? minutesAgo(1) : null,
    completed_at: null,
    result: null,
    error_message: null,
  };
}

function failedJob(id, taskType, priority, minutesAgoCreated, payload, message) {
  const completedAt = minutesAgo(Math.max(minutesAgoCreated - 3, 0));

  return {
    id,
    task_type: taskType,
    payload,
    status: "failed",
    priority,
    attempts: 3,
    max_attempts: 3,
    created_at: minutesAgo(minutesAgoCreated),
    updated_at: completedAt,
    started_at: minutesAgo(Math.max(minutesAgoCreated - 5, 0)),
    completed_at: completedAt,
    result: null,
    error_message: message,
  };
}

export const mockJobs = [
  activeJob(
    "job_1056",
    "generate_pdf",
    "processing",
    5,
    4,
    {
      title: "November operations summary",
      sections: [
        {
          heading: "Queue health",
          paragraphs: ["All primary queue checks completed."],
        },
      ],
    },
  ),
  completedJob(
    "job_1055",
    "send_email",
    5,
    12,
    {
      to: "ops@example.com",
      subject: "Daily queue summary",
      body: "Your queue summary is ready.",
      body_type: "text",
    },
    { provider_message_id: "msg_8f12a" },
  ),
  activeJob(
    "job_1054",
    "resize_image",
    "pending",
    2,
    18,
    {
      input_path: "uploads/product-photo.png",
      width: 1200,
      height: 900,
      keep_aspect_ratio: true,
    },
  ),
  completedJob(
    "job_1053",
    "generate_pdf",
    8,
    29,
    {
      title: "Quarterly service report",
      sections: [
        {
          heading: "Highlights",
          paragraphs: ["Service availability remained within target."],
        },
      ],
    },
    { output_path: "reports/job_1053/report.pdf", file_size: 18432 },
  ),
  failedJob(
    "job_1052",
    "send_email",
    2,
    41,
    {
      to: "alerts@example.com",
      subject: "Worker pool alert",
      body: "A worker needs attention.",
      body_type: "text",
    },
    "PROVIDER_ERROR: The email provider could not accept the request.",
  ),
  completedJob(
    "job_1051",
    "resize_image",
    5,
    55,
    {
      input_path: "uploads/hero-banner.jpg",
      width: 1600,
      keep_aspect_ratio: true,
      output_format: "webp",
    },
    { output_path: "images/job_1051/hero-banner.webp", width: 1600 },
  ),
  activeJob(
    "job_1050",
    "send_email",
    "processing",
    8,
    68,
    {
      to: ["team@example.com", "support@example.com"],
      subject: "Scheduled maintenance notice",
      body: "Maintenance begins at 22:00 UTC.",
      body_type: "text",
    },
  ),
  completedJob(
    "job_1049",
    "generate_pdf",
    2,
    83,
    {
      title: "Customer activity report",
      sections: [
        {
          heading: "Overview",
          paragraphs: ["Activity increased compared with last week."],
        },
      ],
    },
    { output_path: "reports/job_1049/report.pdf", file_size: 22304 },
  ),
  failedJob(
    "job_1048",
    "resize_image",
    5,
    96,
    {
      input_path: "uploads/archive/old-logo.png",
      width: 640,
      height: 640,
      keep_aspect_ratio: false,
    },
    "FILE_NOT_FOUND: The input image was not found.",
  ),
  activeJob(
    "job_1047",
    "generate_pdf",
    "pending",
    2,
    112,
    {
      title: "Deployment checklist",
      sections: [
        {
          heading: "Release steps",
          paragraphs: ["Confirm deployment and smoke-test results."],
        },
      ],
    },
  ),
  completedJob(
    "job_1046",
    "send_email",
    5,
    129,
    {
      to: "billing@example.com",
      subject: "Invoice ready",
      body: "Your monthly invoice is attached.",
      body_type: "text",
    },
    { provider_message_id: "msg_4b672" },
  ),
  completedJob(
    "job_1045",
    "resize_image",
    8,
    144,
    {
      input_path: "uploads/team-photo.jpg",
      width: 1000,
      height: 700,
      keep_aspect_ratio: true,
      allow_upscale: false,
    },
    { output_path: "images/job_1045/team-photo.jpg", width: 1000, height: 667 },
  ),
  failedJob(
    "job_1044",
    "generate_pdf",
    2,
    161,
    {
      title: "Weekly activity report",
      sections: [
        {
          heading: "Activity",
          paragraphs: ["The report could not be completed."],
        },
      ],
    },
    "MISSING_CONFIG: PDF_OUTPUT_DIR is not configured.",
  ),
  completedJob(
    "job_1043",
    "send_email",
    5,
    179,
    {
      to: "engineering@example.com",
      subject: "Build completed",
      body: "The nightly build completed successfully.",
      body_type: "text",
    },
    { provider_message_id: "msg_d9371" },
  ),
  activeJob(
    "job_1042",
    "resize_image",
    "pending",
    5,
    204,
    {
      input_path: "uploads/catalog/item-42.png",
      width: 800,
      height: 800,
      keep_aspect_ratio: true,
    },
  ),
];
