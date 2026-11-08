import { useEffect, useRef, useState } from "react";
import { uploadImage } from "../services/jobService.js";

const taskTypes = [
  { value: "send_email", label: "Send email" },
  { value: "resize_image", label: "Resize image" },
  { value: "generate_pdf", label: "Generate PDF" },
];

const MAX_IMAGE_UPLOAD_SIZE_BYTES = 10 * 1024 * 1024;
const MAX_IMAGE_PIXELS = 40_000_000;
const supportedImageTypes = new Set(["image/jpeg", "image/png", "image/webp"]);
const supportedImageExtensions = /\.(jpe?g|png|webp)$/i;

const initialEmail = {
  to: "",
  subject: "",
  body: "",
  body_type: "text",
};

const initialImage = {
  width: "",
  height: "",
  keep_aspect_ratio: true,
  allow_upscale: false,
  output_filename: "",
  output_format: "",
};

function createInitialPdf() {
  return {
    title: "",
    sections: [{ heading: "", paragraphs: "" }],
    output_filename: "",
  };
}

function optionalOutputFilename(value) {
  const filename = value.trim();
  if (!filename) return undefined;
  if (filename.length > 255) {
    throw new Error("Output filename must be at most 255 characters.");
  }
  if (/[\\/]/.test(filename) || filename === "." || filename === "..") {
    throw new Error("Output filename must be a filename, not a path.");
  }
  const extensionIndex = filename.lastIndexOf(".");
  const stem = extensionIndex > 0 ? filename.slice(0, extensionIndex) : filename;
  const safeStem = stem
    .replace(/[^A-Za-z0-9._-]/g, "_")
    .replace(/^[._-]+|[._-]+$/g, "");
  if (!safeStem) {
    throw new Error("Output filename must contain a safe filename character.");
  }
  return filename;
}

function buildEmailPayload(email) {
  const recipients = email.to
    .split(",")
    .map((recipient) => recipient.trim())
    .filter(Boolean);

  if (recipients.length === 0 || recipients.length > 50) {
    throw new Error("Enter between 1 and 50 email recipients.");
  }
  if (!email.subject.trim() || email.subject.length > 255) {
    throw new Error("Subject must be nonblank and at most 255 characters.");
  }
  if (!email.body.trim() || email.body.length > 50_000) {
    throw new Error("Body must be nonblank and at most 50000 characters.");
  }

  return {
    to: recipients.length === 1 ? recipients[0] : recipients,
    subject: email.subject.trim(),
    body: email.body,
    body_type: email.body_type,
  };
}

function buildPdfPayload(pdf) {
  const title = pdf.title.trim();
  if (!title || title.length > 200) {
    throw new Error("Title must be nonblank and at most 200 characters.");
  }
  if (pdf.sections.length < 1 || pdf.sections.length > 50) {
    throw new Error("A PDF must contain between 1 and 50 sections.");
  }

  let totalContentLength = title.length;
  const sections = pdf.sections.map((section, index) => {
    const heading = section.heading.trim();
    const paragraphs = section.paragraphs
      .split(/\r?\n\s*\r?\n/)
      .map((paragraph) => paragraph.trim())
      .filter(Boolean);

    if (heading.length > 200) {
      throw new Error(`Section ${index + 1} heading must be at most 200 characters.`);
    }
    if (!heading && paragraphs.length === 0) {
      throw new Error(`Section ${index + 1} needs a heading or paragraph.`);
    }
    if (paragraphs.length > 100) {
      throw new Error(`Section ${index + 1} can contain at most 100 paragraphs.`);
    }
    if (paragraphs.some((paragraph) => paragraph.length > 10_000)) {
      throw new Error(`Each paragraph in section ${index + 1} must be at most 10000 characters.`);
    }

    totalContentLength += heading.length;
    paragraphs.forEach((paragraph) => {
      totalContentLength += paragraph.length;
    });

    return {
      ...(heading ? { heading } : {}),
      paragraphs,
    };
  });

  if (totalContentLength > 100_000) {
    throw new Error("Total PDF text must be at most 100000 characters.");
  }

  const outputFilename = optionalOutputFilename(pdf.output_filename);
  return {
    title,
    sections,
    ...(outputFilename ? { output_filename: outputFilename } : {}),
  };
}

function invalidField(message, fieldRef) {
  const error = new Error(message);
  error.fieldRef = fieldRef;
  throw error;
}

function parseDimension(value, label, fieldRef) {
  if (value === "") return undefined;
  const dimension = Number(value);
  if (!Number.isInteger(dimension) || dimension < 1 || dimension > 10_000) {
    invalidField(
      `${label} must be a whole number from 1 to 10000.`,
      fieldRef,
    );
  }
  return dimension;
}

function buildImagePayload(image, fieldRefs) {
  const width = parseDimension(image.width, "Width", fieldRefs.width);
  const height = parseDimension(image.height, "Height", fieldRefs.height);
  if (width === undefined && height === undefined) {
    invalidField("Enter at least one target dimension.", fieldRefs.width);
  }
  if (!image.keep_aspect_ratio && (width === undefined || height === undefined)) {
    invalidField(
      "Both dimensions are required when keeping aspect ratio is off.",
      width === undefined ? fieldRefs.width : fieldRefs.height,
    );
  }

  const outputFilename = optionalOutputFilename(image.output_filename);
  return {
    ...(width === undefined ? {} : { width }),
    ...(height === undefined ? {} : { height }),
    keep_aspect_ratio: image.keep_aspect_ratio,
    allow_upscale: image.allow_upscale,
    ...(outputFilename ? { output_filename: outputFilename } : {}),
    ...(image.output_format ? { output_format: image.output_format } : {}),
  };
}

function buildPayload(taskType, email, pdf, image, imageFieldRefs) {
  if (taskType === "send_email") return buildEmailPayload(email);
  if (taskType === "generate_pdf") return buildPdfPayload(pdf);
  if (taskType === "resize_image") {
    return buildImagePayload(image, imageFieldRefs);
  }
  throw new Error("Select a supported task type.");
}

function validateImageFile(file, dimensions, previewError, fieldRef) {
  if (!file) invalidField("Choose an image to upload.", fieldRef);
  if (file.size === 0) invalidField("The selected image is empty.", fieldRef);
  if (file.size > MAX_IMAGE_UPLOAD_SIZE_BYTES) {
    invalidField("Image must be 10 MiB or smaller.", fieldRef);
  }
  if (
    !supportedImageTypes.has(file.type.toLowerCase()) &&
    !supportedImageExtensions.test(file.name)
  ) {
    invalidField("Choose a JPEG, PNG, or WEBP image.", fieldRef);
  }
  if (previewError) {
    invalidField("The selected file is not a valid previewable image.", fieldRef);
  }
  if (!dimensions) {
    invalidField("Wait for the image preview to load, then try again.", fieldRef);
  }
  if (dimensions.width * dimensions.height > MAX_IMAGE_PIXELS) {
    invalidField("Image exceeds the 40-million-pixel limit.", fieldRef);
  }
}

export default function SubmitJob({
  taskType,
  formError,
  successMessage,
  submitting,
  onTaskTypeChange,
  onClearFeedback,
  onSubmit,
  onViewJob,
}) {
  const [email, setEmail] = useState(initialEmail);
  const [pdf, setPdf] = useState(createInitialPdf);
  const [image, setImage] = useState(initialImage);
  const [imageFile, setImageFile] = useState(null);
  const [previewUrl, setPreviewUrl] = useState("");
  const [previewDimensions, setPreviewDimensions] = useState(null);
  const [previewError, setPreviewError] = useState(false);
  const [validationError, setValidationError] = useState("");
  const [workflowError, setWorkflowError] = useState("");
  const [uploadedImage, setUploadedImage] = useState(null);
  const [uploading, setUploading] = useState(false);
  const [processing, setProcessing] = useState(false);
  const [submittedJob, setSubmittedJob] = useState(null);
  const imageFileInput = useRef(null);
  const previewUrlRef = useRef("");
  const taskTypeInput = useRef(null);
  const widthInput = useRef(null);
  const heightInput = useRef(null);
  const submitInFlight = useRef(false);
  const busy = submitting || processing;

  useEffect(
    () => () => {
      if (previewUrlRef.current) {
        URL.revokeObjectURL(previewUrlRef.current);
        previewUrlRef.current = "";
      }
    },
    [],
  );

  function setSelectedImageFile(file) {
    const nextPreviewUrl = file ? URL.createObjectURL(file) : "";
    if (previewUrlRef.current) {
      URL.revokeObjectURL(previewUrlRef.current);
    }
    previewUrlRef.current = nextPreviewUrl;
    setPreviewUrl(nextPreviewUrl);
    setImageFile(file);
    setPreviewDimensions(null);
    setPreviewError(false);
    setUploadedImage(null);
  }

  function updatePdfSection(index, field, value) {
    setPdf((current) => ({
      ...current,
      sections: current.sections.map((section, sectionIndex) =>
        sectionIndex === index
          ? { ...section, [field]: value }
          : section,
      ),
    }));
  }

  function addPdfSection() {
    setPdf((current) =>
      current.sections.length >= 50
        ? current
        : {
            ...current,
            sections: [...current.sections, { heading: "", paragraphs: "" }],
          },
    );
  }

  function removePdfSection(index) {
    setPdf((current) =>
      current.sections.length <= 1
        ? current
        : {
            ...current,
            sections: current.sections.filter(
              (_section, sectionIndex) => sectionIndex !== index,
            ),
          },
    );
  }

  function resetForm() {
    setEmail(initialEmail);
    setPdf(createInitialPdf());
    setImage(initialImage);
    setSelectedImageFile(null);
    setValidationError("");
    setWorkflowError("");
    if (imageFileInput.current) imageFileInput.current.value = "";
  }

  async function handleSubmit(event) {
    event.preventDefault();
    if (submitInFlight.current) return;
    submitInFlight.current = true;
    setProcessing(true);
    setValidationError("");
    setWorkflowError("");
    setSubmittedJob(null);
    onClearFeedback();

    let payload;
    let stagedUpload = null;
    try {
      if (taskType === "resize_image") {
        validateImageFile(
          imageFile,
          previewDimensions,
          previewError,
          imageFileInput,
        );
      }
      payload = buildPayload(taskType, email, pdf, image, {
        width: widthInput,
        height: heightInput,
      });

      if (taskType === "resize_image") {
        stagedUpload =
          uploadedImage?.file === imageFile ? uploadedImage : null;
        if (!stagedUpload) {
          setUploading(true);
          try {
            const response = await uploadImage(imageFile);
            stagedUpload = {
              file: imageFile,
              inputPath: response.input_path,
              imageId: response.id,
            };
            setUploadedImage(stagedUpload);
          } catch (error) {
            setWorkflowError(
              `Image was not uploaded: ${
                error instanceof Error
                  ? error.message
                  : "The upload request failed."
              }`,
            );
            return;
          } finally {
            setUploading(false);
          }
        }
        payload = {
          ...payload,
          ...(stagedUpload.imageId
            ? { image_id: stagedUpload.imageId }
            : { input_path: stagedUpload.inputPath }),
        };
      }

      const result = await onSubmit(taskType, payload);
      if (result?.ok) {
        resetForm();
        setSubmittedJob(result.job);
      } else if (taskType === "resize_image" && stagedUpload) {
        setWorkflowError(
          `Image was uploaded, but the job was not created: ${
            result?.error || "The job request failed."
          } The uploaded file may remain on the server. Retry to submit the job without uploading the image again.`,
        );
      }
    } catch (error) {
      if (error?.fieldRef) error.fieldRef.current?.focus();
      setValidationError(
        error instanceof Error ? error.message : "Check the task fields.",
      );
    } finally {
      submitInFlight.current = false;
      setProcessing(false);
      setUploading(false);
    }
  }

  function handleTaskTypeChange(event) {
    setValidationError("");
    setWorkflowError("");
    setSubmittedJob(null);
    onTaskTypeChange(event.target.value);
  }

  function handleFormChange() {
    setValidationError("");
    setWorkflowError("");
    setSubmittedJob(null);
    onClearFeedback();
  }

  function handleImageFileChange(event) {
    setSelectedImageFile(event.target.files?.[0] ?? null);
    setWorkflowError("");
  }

  function removeImage() {
    setSelectedImageFile(null);
    setValidationError("");
    setWorkflowError("");
    setSubmittedJob(null);
    if (imageFileInput.current) imageFileInput.current.value = "";
    onClearFeedback();
  }

  const displayedError = validationError || workflowError || formError;

  return (
    <section className="panel submit-panel">
      <div className="panel-heading">
        <div>
          <h2 className="panel-title">Submit a job</h2>
          <p className="panel-description">
            Choose a supported task and enter its required fields.
          </p>
        </div>
      </div>

      <form className="submit-form" onSubmit={handleSubmit} onChange={handleFormChange}>
        <label className="form-field">
          <span className="form-label">Task type</span>
          <select
            ref={taskTypeInput}
            value={taskType}
            onChange={handleTaskTypeChange}
            disabled={busy}
            required
            aria-describedby={displayedError ? "submit-form-error" : undefined}
          >
            <option value="">Select a task type</option>
            {taskTypes.map((task) => (
              <option key={task.value} value={task.value}>
                {task.label}
              </option>
            ))}
          </select>
        </label>

        {taskType === "send_email" && (
          <div className="task-form-fields">
            <label className="form-field">
              <span className="form-label">Recipient email(s)</span>
              <input
                type="email"
                multiple
                required
                disabled={submitting}
                value={email.to}
                onChange={(event) =>
                  setEmail((current) => ({ ...current, to: event.target.value }))
                }
                placeholder="person@example.com, another@example.com"
              />
              <span className="field-help">
                Separate multiple recipients with commas (maximum 50).
              </span>
            </label>

            <div className="form-row">
              <label className="form-field">
                <span className="form-label">Subject</span>
                <input
                  type="text"
                  required
                  maxLength={255}
                  disabled={submitting}
                  value={email.subject}
                  onChange={(event) =>
                    setEmail((current) => ({
                      ...current,
                      subject: event.target.value,
                    }))
                  }
                />
              </label>
              <label className="form-field">
                <span className="form-label">Body type</span>
                <select
                  value={email.body_type}
                  disabled={submitting}
                  onChange={(event) =>
                    setEmail((current) => ({
                      ...current,
                      body_type: event.target.value,
                    }))
                  }
                >
                  <option value="text">Plain text</option>
                  <option value="html">HTML</option>
                </select>
              </label>
            </div>

            <label className="form-field">
              <span className="form-label">Message body</span>
              <textarea
                required
                maxLength={50_000}
                rows={7}
                disabled={submitting}
                value={email.body}
                onChange={(event) =>
                  setEmail((current) => ({ ...current, body: event.target.value }))
                }
              />
            </label>
          </div>
        )}

        {taskType === "generate_pdf" && (
          <div className="task-form-fields">
            <label className="form-field">
              <span className="form-label">Document title</span>
              <input
                type="text"
                required
                maxLength={200}
                disabled={submitting}
                value={pdf.title}
                onChange={(event) =>
                  setPdf((current) => ({ ...current, title: event.target.value }))
                }
              />
            </label>

            <div className="form-field">
              <span className="form-label">Sections</span>
              {pdf.sections.map((section, index) => (
                <div className="task-form-section" key={index}>
                  <div className="task-form-section-header">
                    <span className="form-label">Section {index + 1}</span>
                    <button
                      type="button"
                      className="button button--text"
                      onClick={() => removePdfSection(index)}
                      disabled={submitting || pdf.sections.length <= 1}
                    >
                      Remove
                    </button>
                  </div>
                  <label className="form-field">
                    <span className="form-label">Heading (optional)</span>
                    <input
                      type="text"
                      maxLength={200}
                      disabled={submitting}
                      value={section.heading}
                      onChange={(event) =>
                        updatePdfSection(index, "heading", event.target.value)
                      }
                    />
                  </label>
                  <label className="form-field">
                    <span className="form-label">Paragraphs</span>
                    <textarea
                      rows={4}
                      disabled={submitting}
                      value={section.paragraphs}
                      onChange={(event) =>
                        updatePdfSection(index, "paragraphs", event.target.value)
                      }
                    />
                    <span className="field-help">
                      Separate paragraphs with a blank line. A section needs a heading or paragraph.
                    </span>
                  </label>
                </div>
              ))}
              <button
                type="button"
                className="button button--secondary"
                onClick={addPdfSection}
                disabled={submitting || pdf.sections.length >= 50}
              >
                Add section
              </button>
            </div>

            <label className="form-field">
              <span className="form-label">Output filename (optional)</span>
              <input
                type="text"
                maxLength={255}
                disabled={submitting}
                value={pdf.output_filename}
                onChange={(event) =>
                  setPdf((current) => ({
                    ...current,
                    output_filename: event.target.value,
                  }))
                }
                placeholder="report.pdf"
              />
              <span className="field-help">Enter a filename, not a directory path.</span>
            </label>
          </div>
        )}

        {taskType === "resize_image" && (
          <div className="task-form-fields">
            <div className="form-field">
              <label className="form-label" htmlFor="resize-image-file">
                Image file
              </label>
              <input
                id="resize-image-file"
                ref={imageFileInput}
                type="file"
                accept="image/jpeg,image/png,image/webp,.jpg,.jpeg,.png,.webp"
                disabled={busy}
                aria-describedby={displayedError ? "submit-form-error" : "resize-image-help"}
                onChange={handleImageFileChange}
              />
              <span className="field-help">
                JPEG, PNG, or WEBP; maximum 10 MiB.
              </span>
              <span id="resize-image-help" className="field-help">
                The image will be uploaded before the resize job is created.
              </span>
              {imageFile && (
                <div className="field-help" aria-live="polite">
                  <span>Filename: {imageFile.name}</span>
                  <br />
                  <span>
                    Original dimensions:{" "}
                    {previewDimensions
                      ? `${previewDimensions.width} × ${previewDimensions.height}`
                      : previewError
                        ? "Could not read image dimensions"
                        : "Loading…"}
                  </span>
                </div>
              )}
              {previewUrl && (
                <img
                  className="image-preview"
                  src={previewUrl}
                  alt={imageFile?.name || "Selected image preview"}
                  onLoad={(event) => {
                    setPreviewError(false);
                    setPreviewDimensions({
                      width: event.currentTarget.naturalWidth,
                      height: event.currentTarget.naturalHeight,
                    });
                  }}
                  onError={() => {
                    setPreviewError(true);
                    setPreviewDimensions(null);
                  }}
                />
              )}
              {imageFile && (
                <div className="form-row">
                  <button
                    type="button"
                    className="button button--secondary"
                    disabled={busy}
                    onClick={() => {
                      if (imageFileInput.current) {
                        imageFileInput.current.value = "";
                        imageFileInput.current.click();
                      }
                    }}
                  >
                    Replace image
                  </button>
                  <button
                    type="button"
                    className="button button--secondary"
                    disabled={busy}
                    onClick={removeImage}
                  >
                    Remove image
                  </button>
                </div>
              )}
            </div>

            <div className="form-row">
              <label className="form-field">
                <span className="form-label">Width (optional)</span>
                <input
                  type="number"
                  min="1"
                  max="10000"
                  step="1"
                  ref={widthInput}
                  disabled={busy}
                  value={image.width}
                  aria-describedby={displayedError ? "submit-form-error" : undefined}
                  onChange={(event) =>
                    setImage((current) => ({
                      ...current,
                      width: event.target.value,
                    }))
                  }
                />
              </label>
              <label className="form-field">
                <span className="form-label">Height (optional)</span>
                <input
                  type="number"
                  min="1"
                  max="10000"
                  step="1"
                  ref={heightInput}
                  disabled={busy}
                  value={image.height}
                  aria-describedby={displayedError ? "submit-form-error" : undefined}
                  onChange={(event) =>
                    setImage((current) => ({
                      ...current,
                      height: event.target.value,
                    }))
                  }
                />
              </label>
            </div>

            <div className="form-row">
              <label className="checkbox-field">
                <input
                  type="checkbox"
                  checked={image.keep_aspect_ratio}
                  disabled={busy}
                  onChange={(event) =>
                    setImage((current) => ({
                      ...current,
                      keep_aspect_ratio: event.target.checked,
                    }))
                  }
                />
                Keep aspect ratio
              </label>
              <label className="checkbox-field">
                <input
                  type="checkbox"
                  checked={image.allow_upscale}
                  disabled={busy}
                  onChange={(event) =>
                    setImage((current) => ({
                      ...current,
                      allow_upscale: event.target.checked,
                    }))
                  }
                />
                Allow upscaling
              </label>
            </div>

            <div className="form-row">
              <label className="form-field">
                <span className="form-label">Output format (optional)</span>
                <select
                  value={image.output_format}
                  disabled={busy}
                  onChange={(event) =>
                    setImage((current) => ({
                      ...current,
                      output_format: event.target.value,
                    }))
                  }
                >
                  <option value="">Keep input format</option>
                  <option value="JPEG">JPEG</option>
                  <option value="PNG">PNG</option>
                  <option value="WEBP">WEBP</option>
                </select>
              </label>
              <label className="form-field">
                <span className="form-label">Output filename (optional)</span>
                <input
                  type="text"
                  maxLength={255}
                  disabled={busy}
                  value={image.output_filename}
                  onChange={(event) =>
                    setImage((current) => ({
                      ...current,
                      output_filename: event.target.value,
                    }))
                  }
                  placeholder="resized-photo.png"
                />
              </label>
            </div>
          </div>
        )}

        {!taskType && (
          <p className="field-help">
            Select a task type to see its required fields.
          </p>
        )}

        {displayedError && (
          <div
            id="submit-form-error"
            className="form-feedback form-feedback--error"
            role="alert"
          >
            <span className="feedback-icon" aria-hidden="true">!</span>
            {displayedError}
          </div>
        )}
        {successMessage && (
          <div className="form-feedback form-feedback--success" role="status">
            <span className="feedback-icon" aria-hidden="true">✓</span>
            {successMessage}
          </div>
        )}
        {uploading && (
          <div className="table-state" role="status">
            Uploading image…
          </div>
        )}
        {submittedJob && (
          <button
            type="button"
            className="button button--secondary"
            onClick={() => onViewJob(submittedJob.id)}
          >
            View job
          </button>
        )}

        <div className="form-actions">
          <button
            type="submit"
            className="button button--primary"
            disabled={busy || !taskType}
          >
            <span className="button-plus" aria-hidden="true">+</span>
            {uploading
              ? "Uploading image…"
              : busy
                ? "Submitting…"
                : "Submit job"}
          </button>
        </div>
      </form>
    </section>
  );
}
