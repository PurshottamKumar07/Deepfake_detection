"""
api/app.py
==========
FastAPI backend for the Deepfake Detection prototype.

Endpoints:
    POST /detect       — upload one or more files, returns detection results
    GET  /health       — health check
    GET  /              — serves the frontend UI (via StaticFiles mount)

Usage:
    cd final_year_projet
    uvicorn api.app:app --reload --port 8000
"""

from __future__ import annotations

import logging
import shutil
import uuid
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from fastapi import FastAPI, File, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from scripts.main import process_upload

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("deepfake_api")

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------
app = FastAPI(
    title="Deepfake Detector API",
    description="Multi-modal deepfake detection: audio, video, image, metadata",
    version="0.1.0",
)

# Allow all origins for local development — tighten in production
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/health")
def health_check() -> dict[str, str]:
    """Simple liveness check."""
    return {"status": "ok"}


@app.post("/detect")
async def detect(files: list[UploadFile] = File(...)) -> JSONResponse:
    """
    Receive one or more files from the frontend, run the full detection
    pipeline (ingest → route → detect → persist), and return the results.
    """
    logger.info("Received %d file(s) for detection.", len(files))

    file_list: list[dict[str, Any]] = []

    with TemporaryDirectory() as tmp_dir:
        for upload in files:
            # Save each uploaded file to a temp path
            safe_name = upload.filename or f"unknown_{uuid.uuid4()}"
            tmp_path = Path(tmp_dir) / f"{uuid.uuid4()}_{safe_name}"

            try:
                with open(tmp_path, "wb") as f:
                    shutil.copyfileobj(upload.file, f)
            except OSError as exc:
                logger.error("Failed to write temp file '%s': %s", safe_name, exc)
                continue

            file_list.append({
                "source_path": str(tmp_path),
                "original_filename": safe_name,
            })

        if not file_list:
            return JSONResponse(
                status_code=400,
                content={"error": "No valid files received."},
            )

        # Run the full detection pipeline
        results = process_upload(file_list)

    # Strip frame_results from video responses (can be very large)
    for r in results:
        if "frame_results" in r:
            frame_summary = []
            for fr in r.get("frame_results", []):
                frame_summary.append({
                    "frame": fr.get("frame"),
                    "is_deepfake": fr.get("is_deepfake"),
                    "confidence": fr.get("confidence"),
                    "label": fr.get("label"),
                })
            r["frame_results"] = frame_summary

    return JSONResponse(content={"results": results})


# ---------------------------------------------------------------------------
# Static file serving — mount AFTER API routes so /detect still works
# ---------------------------------------------------------------------------
FRONTEND_DIST = Path(__file__).resolve().parent.parent / "frontend_dist"
FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

STATIC_TARGET = FRONTEND_DIST if FRONTEND_DIST.exists() else FRONTEND_DIR

if STATIC_TARGET.exists():
    app.mount("/", StaticFiles(directory=str(STATIC_TARGET), html=True), name="frontend")
    logger.info("Frontend served from: %s", STATIC_TARGET)
else:
    logger.warning(
        "Neither frontend_dist nor frontend directory found. "
        "The API will still work, but the UI will not be served."
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "api.app:app",
        host="127.0.0.1",
        port=8080,
        reload=True,
    )
