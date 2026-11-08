const apiBaseUrl = (import.meta.env.VITE_API_BASE_URL || "/api").replace(/\/+$/, "");

function errorMessage(detail, status) {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail
      .map((item) => {
        const location = Array.isArray(item.loc) ? item.loc.join(".") : "";
        return location ? `${location}: ${item.msg}` : item.msg;
      })
      .filter(Boolean)
      .join("; ");
  }
  return `Request failed with status ${status}.`;
}

async function request(path, options = {}) {
  let response;
  const isFormData =
    typeof FormData !== "undefined" && options.body instanceof FormData;
  try {
    response = await fetch(`${apiBaseUrl}${path}`, {
      ...options,
      headers: {
        Accept: "application/json",
        ...(options.body && !isFormData
          ? { "Content-Type": "application/json" }
          : {}),
        ...options.headers,
      },
    });
  } catch (error) {
    if (error instanceof TypeError) {
      throw new Error("Cannot reach the job API. Check that the backend is running.");
    }
    throw error;
  }

  const contentType = response.headers.get("content-type") || "";
  const responseBody = contentType.includes("application/json")
    ? await response.json()
    : null;
  if (!response.ok) {
    throw new Error(errorMessage(responseBody?.detail, response.status));
  }
  return responseBody;
}

export async function uploadImage(file) {
  const formData = new FormData();
  formData.append("file", file);

  const uploaded = await request("/uploads/images", {
    method: "POST",
    body: formData,
  });
  if (typeof uploaded?.input_path !== "string" || !uploaded.input_path) {
    throw new Error("The image upload API returned an unexpected response.");
  }
  return uploaded;
}

export async function getJobs() {
  const jobs = await request("/jobs");
  if (!Array.isArray(jobs)) {
    throw new Error("The jobs API returned an unexpected response.");
  }
  return jobs;
}

export async function getJob(jobId) {
  return request(`/jobs/${encodeURIComponent(jobId)}`);
}

export function jobOutputUrl(jobId) {
  return `${apiBaseUrl}/jobs/${encodeURIComponent(jobId)}/output`;
}

export function imageFileUrl(imageId) {
  return `${apiBaseUrl}/images/${encodeURIComponent(imageId)}/file`;
}

export async function listImages() {
  const images = await request("/images");
  if (!Array.isArray(images)) {
    throw new Error("The images API returned an unexpected response.");
  }
  return images;
}

export async function createJob(taskType, payload, idempotencyKey) {
  const created = await request("/jobs", {
    method: "POST",
    headers: idempotencyKey ? { "Idempotency-Key": idempotencyKey } : {},
    body: JSON.stringify({ task_type: taskType, payload }),
  });
  if (typeof created?.id !== "string" || typeof created?.status !== "string") {
    throw new Error("The job API returned an unexpected creation response.");
  }
  return created;
}
