import { useCallback, useEffect, useState } from "react";
import { imageFileUrl, listImages } from "../services/jobService.js";

export default function Images() {
  const [images, setImages] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      setImages(await listImages());
    } catch (loadError) {
      setError(loadError.message || "Could not load images.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  return (
    <section className="panel">
      <div className="panel-heading">
        <div>
          <h2 className="panel-title">Uploaded images</h2>
          <p className="panel-description">
            Images uploaded for resize jobs, newest first.
          </p>
        </div>
        <button
          type="button"
          className="button button--secondary"
          onClick={load}
          disabled={loading}
        >
          {loading ? "Loading…" : "Refresh"}
        </button>
      </div>
      {error && (
        <div className="error-banner" role="alert">
          {error}
        </div>
      )}
      {!loading && !error && images.length === 0 && (
        <div className="table-state">No images uploaded yet.</div>
      )}
      <div className="image-gallery">
        {images.map((image) => (
          <figure className="image-gallery-item" key={image.id}>
            <img
              src={imageFileUrl(image.id)}
              alt={`Uploaded ${image.width}x${image.height} image`}
              loading="lazy"
            />
            <figcaption>
              {image.width}×{image.height} · {Math.ceil(image.size_bytes / 1024)} KB
            </figcaption>
          </figure>
        ))}
      </div>
    </section>
  );
}
