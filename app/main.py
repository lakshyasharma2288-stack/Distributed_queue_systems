from fastapi import FastAPI

from app.api.routes.images import router as images_router
from app.api.routes.jobs import router as jobs_router
from app.api.routes.queue import router as queue_router
from app.api.routes.uploads import router as uploads_router

app = FastAPI(title="Distributed Job Queue System", version="0.1.0")

app.include_router(jobs_router)
app.include_router(queue_router)
app.include_router(uploads_router)
app.include_router(images_router)


@app.get("/health")
def health_check() -> dict[str, str]:
    return {"status": "ok"}
