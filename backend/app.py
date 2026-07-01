"""
app.py - FastAPI backend for the vectorize web app.

Endpoints:
    POST   /api/jobs            start a vectorize job (multipart upload + settings)
    GET    /api/jobs/{job_id}   poll job status (progress, stage, eta, state)
    POST   /api/jobs/{job_id}/cancel   request cancellation of a running job
    GET    /api/jobs/{job_id}/download download the finished SVG
    DELETE /api/jobs/{job_id}   drop a finished/cancelled/errored job from memory

Jobs run on a background thread (the vectorizer is CPU-bound numpy/opencv
work, not async-friendly) and report progress into an in-memory job store
that the frontend polls. Nothing is persisted to disk; everything lives in
memory for the lifetime of the process, which is fine for a small single
instance deployment.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response

from eta import ETATracker
from vectorizer import VectorizeCancelled, VectorizeSettings, vectorize_to_svg_string

MAX_UPLOAD_BYTES = 20 * 1024 * 1024  # 20 MB
JOB_TTL_SECONDS = 30 * 60  # drop finished jobs after 30 minutes if nobody collects them

app = FastAPI(title="vectorize API")

# Static frontend (GitHub Pages, or any origin) talks to this API cross-origin.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@dataclass
class Job:
    id: str
    filename: str
    state: str = "queued"  # queued | running | done | error | cancelled
    progress: float = 0.0
    stage: str = "queued"
    eta_seconds: Optional[float] = None
    error: Optional[str] = None
    svg: Optional[str] = None
    created_at: float = field(default_factory=time.perf_counter)
    updated_at: float = field(default_factory=time.perf_counter)
    cancel_event: threading.Event = field(default_factory=threading.Event)
    thread: Optional[threading.Thread] = None


JOBS: Dict[str, Job] = {}
JOBS_LOCK = threading.Lock()


def _get_job(job_id: str) -> Job:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return job


def _sweep_old_jobs() -> None:
    now = time.perf_counter()
    with JOBS_LOCK:
        stale = [
            jid
            for jid, j in JOBS.items()
            if j.state in ("done", "error", "cancelled") and now - j.updated_at > JOB_TTL_SECONDS
        ]
        for jid in stale:
            JOBS.pop(jid, None)


def _run_job(job: Job, image_bytes: bytes, settings: VectorizeSettings) -> None:
    tracker = ETATracker()
    job.state = "running"

    def on_progress(fraction: float, stage: str) -> None:
        job.progress = max(0.0, min(1.0, fraction))
        job.stage = stage
        job.eta_seconds = tracker.update(job.progress)
        job.updated_at = time.perf_counter()

    try:
        svg = vectorize_to_svg_string(
            image_bytes,
            settings,
            progress=on_progress,
            cancel_event=job.cancel_event,
            source_name=job.filename,
        )
        job.svg = svg
        job.state = "done"
        job.progress = 1.0
        job.stage = "done"
        job.eta_seconds = 0.0
    except VectorizeCancelled:
        job.state = "cancelled"
        job.stage = "cancelled"
        job.eta_seconds = None
    except Exception as exc:  # noqa: BLE001 - surface any failure to the client
        job.state = "error"
        job.error = str(exc)
        job.stage = "error"
        job.eta_seconds = None
    finally:
        job.updated_at = time.perf_counter()


def _parse_settings(
    detail: int,
    colors: int,
    seam_fix: bool,
    preserve_transparency: bool,
    bg_r: int,
    bg_g: int,
    bg_b: int,
) -> VectorizeSettings:
    if not (1 <= detail <= 10):
        raise HTTPException(status_code=400, detail="detail must be between 1 and 10")
    if not (4 <= colors <= 256):
        raise HTTPException(status_code=400, detail="colors must be between 4 and 256")

    return VectorizeSettings(
        detail=detail,
        colors=colors,
        seam_fix=seam_fix,
        preserve_transparency=preserve_transparency,
        background=(
            max(0, min(255, bg_r)),
            max(0, min(255, bg_g)),
            max(0, min(255, bg_b)),
        ),
    )


@app.post("/api/jobs")
async def create_job(
    file: UploadFile = File(...),
    detail: int = Form(7),
    colors: int = Form(72),
    seam_fix: bool = Form(True),
    preserve_transparency: bool = Form(True),
    bg_r: int = Form(255),
    bg_g: int = Form(255),
    bg_b: int = Form(255),
):
    _sweep_old_jobs()

    image_bytes = await file.read()
    if not image_bytes:
        raise HTTPException(status_code=400, detail="empty upload")
    if len(image_bytes) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="file too large (20 MB limit)")

    settings = _parse_settings(detail, colors, seam_fix, preserve_transparency, bg_r, bg_g, bg_b)

    job_id = uuid.uuid4().hex
    job = Job(id=job_id, filename=file.filename or "image")

    with JOBS_LOCK:
        JOBS[job_id] = job

    thread = threading.Thread(target=_run_job, args=(job, image_bytes, settings), daemon=True)
    job.thread = thread
    thread.start()

    return {"job_id": job_id}


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str):
    job = _get_job(job_id)
    return {
        "job_id": job.id,
        "state": job.state,
        "progress": job.progress,
        "stage": job.stage,
        "eta_seconds": job.eta_seconds,
        "error": job.error,
        "filename": job.filename,
    }


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str):
    job = _get_job(job_id)
    if job.state in ("done", "error", "cancelled"):
        return {"job_id": job.id, "state": job.state}
    job.cancel_event.set()
    return {"job_id": job.id, "state": "cancelling"}


@app.get("/api/jobs/{job_id}/download")
def download_job(job_id: str):
    job = _get_job(job_id)
    if job.state != "done" or job.svg is None:
        raise HTTPException(status_code=409, detail=f"job is not finished (state: {job.state})")

    stem = job.filename.rsplit(".", 1)[0] if "." in job.filename else job.filename
    filename = f"{stem}_vectorized.svg"

    return Response(
        content=job.svg,
        media_type="image/svg+xml",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str):
    with JOBS_LOCK:
        JOBS.pop(job_id, None)
    return {"ok": True}


@app.get("/api/health")
def health():
    return {"ok": True}
