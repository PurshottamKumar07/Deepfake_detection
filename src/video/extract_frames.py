"""
src/video/extract_frames.py
============================
Video preprocessing foundation: uniform frame extraction.

Pipeline:
  Video → open/validate → determine FPS/duration/frame count →
  uniformly sample N frames → save or return selected frames

Handles:
  - Corrupted/undecodable videos
  - Very short videos
  - Variable FPS and duration
  - Missing video files

Does NOT implement the temporal model (LSTM/GRU) — that comes later.

Usage:
    # Extract frames and save to disk
    from src.video.extract_frames import extract_frames
    meta = extract_frames("video.mp4", output_dir="frames/", n_frames=20)

    # Extract frames in-memory (for inference)
    from src.video.extract_frames import extract_frames_to_memory
    frames, meta = extract_frames_to_memory("video.mp4", n_frames=20)

    # CLI
    python -m src.video.extract_frames --input video.mp4 --output frames/
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)


class VideoExtractionError(RuntimeError):
    """Raised when frame extraction fails."""


def _open_video(video_path: str | Path):
    """
    Open a video file with OpenCV and return the capture object.

    Returns
    -------
    cv2.VideoCapture or None
        The opened capture, or None if it cannot be opened.
    """
    import cv2

    path = str(video_path)
    cap = cv2.VideoCapture(path)

    if not cap.isOpened():
        logger.warning("Cannot open video: %s", path)
        return None

    return cap


def get_video_metadata(video_path: str | Path) -> dict[str, Any]:
    """
    Read video metadata without extracting frames.

    Returns
    -------
    dict with keys:
        video_path, fps, total_frames, duration_sec, width, height,
        codec, status ('ok' or error description).
    """
    import cv2

    video_path = Path(video_path)
    meta: dict[str, Any] = {
        "video_path": str(video_path),
        "fps": 0.0,
        "total_frames": 0,
        "duration_sec": 0.0,
        "width": 0,
        "height": 0,
        "codec": "",
        "status": "ok",
    }

    if not video_path.exists():
        meta["status"] = "file_not_found"
        return meta

    cap = _open_video(video_path)
    if cap is None:
        meta["status"] = "cannot_open"
        return meta

    try:
        meta["fps"] = float(cap.get(cv2.CAP_PROP_FPS) or 0)
        meta["total_frames"] = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        meta["width"] = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        meta["height"] = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)

        # Codec fourcc
        fourcc = int(cap.get(cv2.CAP_PROP_FOURCC))
        meta["codec"] = "".join(chr((fourcc >> 8 * i) & 0xFF) for i in range(4))

        # Duration
        if meta["fps"] > 0 and meta["total_frames"] > 0:
            meta["duration_sec"] = meta["total_frames"] / meta["fps"]
    except Exception as exc:
        meta["status"] = f"metadata_error: {exc}"
    finally:
        cap.release()

    return meta


def _compute_uniform_indices(
    total_frames: int,
    n_frames: int,
) -> list[int]:
    """
    Compute uniformly-spaced frame indices.

    If n_frames >= total_frames, returns all frame indices.
    """
    if total_frames <= 0:
        return []

    if n_frames >= total_frames:
        return list(range(total_frames))

    # Uniform spacing
    indices = np.linspace(0, total_frames - 1, n_frames, dtype=int).tolist()

    # Remove duplicates (can happen with very short videos)
    indices = sorted(set(indices))

    return indices


def extract_frames(
    video_path: str | Path,
    output_dir: str | Path,
    n_frames: int = 20,
    image_format: str = "jpg",
) -> dict[str, Any]:
    """
    Extract uniformly-sampled frames from a video and save to disk.

    Parameters
    ----------
    video_path:
        Path to the video file.
    output_dir:
        Directory to save extracted frames.
    n_frames:
        Number of frames to uniformly sample.
    image_format:
        Output image format ('jpg', 'png').

    Returns
    -------
    dict with metadata:
        video_path, fps, total_frames, duration_sec, n_requested,
        n_extracted, sampled_indices, saved_paths, status.
    """
    import cv2

    video_path = Path(video_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Get metadata
    meta = get_video_metadata(video_path)
    meta["n_requested"] = n_frames
    meta["n_extracted"] = 0
    meta["sampled_indices"] = []
    meta["saved_paths"] = []

    if meta["status"] != "ok":
        return meta

    total_frames = meta["total_frames"]

    if total_frames <= 0:
        meta["status"] = "no_frames"
        logger.warning("Video has 0 frames: %s", video_path)
        return meta

    # Compute sample indices
    indices = _compute_uniform_indices(total_frames, n_frames)
    meta["sampled_indices"] = indices

    # Extract frames
    cap = _open_video(video_path)
    if cap is None:
        meta["status"] = "cannot_open_for_extraction"
        return meta

    try:
        saved_paths = []
        for frame_idx in indices:
            cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
            ret, frame = cap.read()

            if not ret or frame is None:
                logger.debug(
                    "Could not read frame %d from %s", frame_idx, video_path
                )
                continue

            # Save frame
            stem = video_path.stem
            frame_filename = f"{stem}_frame_{frame_idx:06d}.{image_format}"
            frame_path = output_dir / frame_filename

            cv2.imwrite(str(frame_path), frame)
            saved_paths.append(str(frame_path))

        meta["n_extracted"] = len(saved_paths)
        meta["saved_paths"] = saved_paths

        if len(saved_paths) == 0:
            meta["status"] = "no_frames_decoded"
        elif len(saved_paths) < len(indices):
            meta["status"] = "partial_extraction"
        else:
            meta["status"] = "ok"

    except Exception as exc:
        meta["status"] = f"extraction_error: {exc}"
        logger.error("Frame extraction failed for %s: %s", video_path, exc)
    finally:
        cap.release()

    logger.info(
        "Extracted %d/%d frames from %s",
        meta["n_extracted"], n_frames, video_path.name,
    )

    return meta


def extract_frames_to_memory(
    video_path: str | Path,
    n_frames: int = 20,
) -> tuple[list[np.ndarray], dict[str, Any]]:
    """
    Extract uniformly-sampled frames into memory (as numpy arrays).

    Useful for inference without saving to disk.

    Parameters
    ----------
    video_path:
        Path to the video file.
    n_frames:
        Number of frames to uniformly sample.

    Returns
    -------
    (frames, metadata)
        frames: list of numpy arrays (H, W, 3) in RGB order.
        metadata: extraction metadata dict.
    """
    import cv2

    video_path = Path(video_path)

    meta = get_video_metadata(video_path)
    meta["n_requested"] = n_frames
    meta["n_extracted"] = 0
    meta["sampled_indices"] = []

    if meta["status"] != "ok":
        return [], meta

    total_frames = meta["total_frames"]
    if total_frames <= 0:
        meta["status"] = "no_frames"
        return [], meta

    indices = _compute_uniform_indices(total_frames, n_frames)
    meta["sampled_indices"] = indices

    cap = _open_video(video_path)
    if cap is None:
        meta["status"] = "cannot_open_for_extraction"
        return [], meta

    frames = []
    try:
        for frame_idx in indices:
            cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
            ret, frame = cap.read()

            if not ret or frame is None:
                continue

            # Convert BGR (OpenCV) → RGB
            frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            frames.append(frame_rgb)

        meta["n_extracted"] = len(frames)
        meta["status"] = "ok" if frames else "no_frames_decoded"

    except Exception as exc:
        meta["status"] = f"extraction_error: {exc}"
    finally:
        cap.release()

    return frames, meta


def extract_frames_batch(
    video_paths: list[str | Path],
    output_dir: str | Path,
    n_frames: int = 20,
    config: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """
    Extract frames from multiple videos.

    Parameters
    ----------
    video_paths:
        List of video file paths.
    output_dir:
        Root output directory. Each video gets a subdirectory.
    n_frames:
        Frames per video.
    config:
        Optional config dict (for n_frames override from video.frames_per_video).

    Returns
    -------
    list of metadata dicts (one per video).
    """
    output_dir = Path(output_dir)

    if config:
        n_frames = int(config.get("video", {}).get("frames_per_video", n_frames))

    all_metadata = []
    for i, vpath in enumerate(video_paths):
        vpath = Path(vpath)
        video_output = output_dir / vpath.stem
        logger.info(
            "Extracting frames %d/%d: %s", i + 1, len(video_paths), vpath.name
        )
        meta = extract_frames(vpath, video_output, n_frames=n_frames)
        all_metadata.append(meta)

    # Save batch metadata
    meta_path = output_dir / "extraction_metadata.json"
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(all_metadata, f, indent=2, default=str)
    logger.info("Batch metadata saved: %s", meta_path)

    # Summary
    ok = sum(1 for m in all_metadata if m["status"] == "ok")
    failed = len(all_metadata) - ok
    total_frames = sum(m.get("n_extracted", 0) for m in all_metadata)
    logger.info(
        "Batch extraction: %d/%d videos ok, %d failed, %d total frames",
        ok, len(all_metadata), failed, total_frames,
    )

    return all_metadata


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract uniformly-sampled frames from video files."
    )
    parser.add_argument(
        "--input", type=str, required=True,
        help="Path to a video file or directory of videos.",
    )
    parser.add_argument(
        "--output", type=str, required=True,
        help="Output directory for extracted frames.",
    )
    parser.add_argument(
        "--n-frames", type=int, default=20,
        help="Number of frames to extract per video (default: 20).",
    )
    parser.add_argument("--config", type=str, default=None)
    parser.add_argument(
        "--format", type=str, default="jpg", choices=["jpg", "png"],
        help="Output image format.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    )

    args = _parse_args()
    input_path = Path(args.input)

    if input_path.is_file():
        # Single video
        meta = extract_frames(
            input_path, args.output, n_frames=args.n_frames,
            image_format=args.format,
        )
        print(json.dumps(meta, indent=2, default=str))
    elif input_path.is_dir():
        # Directory of videos
        VIDEO_EXT = {".mp4", ".avi", ".mov", ".mkv", ".webm", ".flv", ".wmv"}
        videos = sorted(
            f for f in input_path.rglob("*")
            if f.is_file() and f.suffix.lower() in VIDEO_EXT
        )
        if not videos:
            print(f"No video files found in {input_path}")
        else:
            print(f"Found {len(videos)} videos. Extracting frames...")
            config = None
            if args.config:
                from src.common.config_loader import load_config
                config = load_config(args.config)

            extract_frames_batch(
                videos, args.output, n_frames=args.n_frames, config=config,
            )
    else:
        print(f"Input not found: {input_path}")
