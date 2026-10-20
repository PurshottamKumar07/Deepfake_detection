"""
src/data/extract_frames.py
===========================
Extract frames from FaceForensics++ videos and prepare the full data pipeline:
  1. Extract N uniformly-spaced frames per video (default 10)
  2. Build a manifest CSV cataloguing every extracted frame
  3. Split into train/val/test by source video ID (leakage-safe)
  4. Compute class weights for the real/fake imbalance
  5. Save everything ready for training

Frame selection strategy:
  UNIFORM TEMPORAL SAMPLING — not random.
  For a video with F frames, we select frames at indices:
    [F/(2N), 3F/(2N), 5F/(2N), ..., (2N-1)F/(2N)]
  This is the center of each of N equal-length temporal bins.
  Advantages over random sampling:
    - Deterministic / reproducible across runs
    - Guarantees temporal coverage of the full video
    - Avoids clustering near video start/end
    - Standard approach in deepfake detection literature (FF++, FaceForensics)

Directory layout produced:
  <output_root>/
    real/
      000_frame00.jpg  ...  000_frame09.jpg
      001_frame00.jpg  ...  etc.
    fake/
      000_003_frame00.jpg  ...  (Deepfakes)
      etc.

Usage (local):
    python -m src.data.extract_frames \
        --video-root "D:/Datasets/FF++/FaceForensics++_C23" \
        --output-root "D:/Datasets/FF++/frames" \
        --frames-per-video 10

Usage (Python / Colab):
    from src.data.extract_frames import run_extraction_pipeline
    run_extraction_pipeline(
        video_root="D:/Datasets/FF++/FaceForensics++_C23",
        output_root="D:/Datasets/FF++/frames",
        frames_per_video=10,
    )
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import os
import random
import sys
import time
from pathlib import Path
from typing import Any

import cv2

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# FF++ dataset class definitions
# ---------------------------------------------------------------------------

# All class directories treated as REAL
REAL_CLASSES = ["original"]

# All class directories treated as FAKE
FAKE_CLASSES = [
    "Deepfakes",
    "Face2Face",
    "FaceShifter",
    "FaceSwap",
    "NeuralTextures",
    "DeepFakeDetection",
]


# ---------------------------------------------------------------------------
# Frame extraction
# ---------------------------------------------------------------------------


def extract_uniform_frames(
    video_path: str | Path,
    n_frames: int = 10,
    quality: int = 95,
) -> list[tuple[int, Any]]:
    """
    Extract N uniformly-spaced frames from a video file.

    Uses the center-of-bin strategy: divides the video into N equal
    temporal bins and takes the center frame of each bin.

    Parameters
    ----------
    video_path:
        Path to the .mp4 video file.
    n_frames:
        Number of frames to extract.
    quality:
        JPEG quality (1-100) for saved frames.

    Returns
    -------
    list of (frame_index, numpy_array) tuples.
    Empty list if the video cannot be read.
    """
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        logger.warning("Cannot open video: %s", video_path)
        return []

    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    if total_frames <= 0:
        logger.warning("Zero frames in video: %s", video_path)
        cap.release()
        return []

    # Compute center-of-bin indices
    actual_n = min(n_frames, total_frames)
    indices = []
    for i in range(actual_n):
        # Center of the i-th bin: (2i + 1) * total_frames / (2 * actual_n)
        idx = int((2 * i + 1) * total_frames / (2 * actual_n))
        idx = min(idx, total_frames - 1)  # Clamp to valid range
        indices.append(idx)

    # Extract frames at computed indices
    frames = []
    for idx in indices:
        cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
        ret, frame = cap.read()
        if ret and frame is not None:
            frames.append((idx, frame))
        else:
            logger.debug("Failed to read frame %d from %s", idx, video_path)

    cap.release()
    return frames


def save_frames(
    frames: list[tuple[int, Any]],
    output_dir: Path,
    video_stem: str,
    quality: int = 95,
) -> list[str]:
    """
    Save extracted frames as JPEG files.

    Returns list of saved filenames (just the basename).
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    saved = []

    for i, (frame_idx, frame_data) in enumerate(frames):
        filename = f"{video_stem}_frame{i:02d}.jpg"
        filepath = output_dir / filename
        cv2.imwrite(
            str(filepath),
            frame_data,
            [cv2.IMWRITE_JPEG_QUALITY, quality],
        )
        saved.append(filename)

    return saved


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


def _process_class_videos(
    video_dir: Path,
    output_dir: Path,
    label: str,
    manipulation_type: str,
    n_frames: int,
    quality: int,
) -> list[dict[str, str]]:
    """
    Process all videos in a single class directory.

    Returns a list of manifest row dicts.
    """
    videos = sorted(video_dir.glob("*.mp4"))
    if not videos:
        logger.warning("No .mp4 files found in: %s", video_dir)
        return []

    rows = []
    for video_path in videos:
        video_stem = video_path.stem  # e.g. "000" or "000_003"
        source_id = video_stem.split("_")[0]
        
        # Prevent collisions for fake classes since multiple methods manipulate the same videos
        out_stem = f"{manipulation_type}_{video_stem}" if label == "fake" else video_stem

        # Check if frames already exist on disk (fast resume)
        expected_files = [f"{out_stem}_frame{i:02d}.jpg" for i in range(n_frames)]
        all_exist = all((output_dir / fname).exists() for fname in expected_files)

        if all_exist:
            saved = expected_files
        else:
            frames = extract_uniform_frames(video_path, n_frames=n_frames, quality=quality)
            if not frames:
                logger.warning("Skipping video (no frames extracted): %s", video_path)
                continue
            saved = save_frames(frames, output_dir, out_stem, quality=quality)

        for filename in saved:
            rows.append({
                "file_path": f"{label}/{filename}",
                "label": label,
                "modality": "image",
                "dataset": "faceforensics_plus_plus",
                "source_id": source_id,
                "person_id": "",
                "speaker_id": "",
                "manipulation_type": manipulation_type,
                "compression": "c23",
                "split": "",
                "generator": manipulation_type if label == "fake" else "",
                "video_file": video_path.name,
            })

    return rows


def _split_by_source_id(
    rows: list[dict],
    train_ratio: float = 0.70,
    val_ratio: float = 0.15,
    test_ratio: float = 0.15,
    seed: int = 42,
) -> tuple[list[dict], list[dict], list[dict]]:
    """
    Split rows into train/val/test by source_id to prevent data leakage.

    ALL frames from the same source video go to the same split.
    """
    # Gather unique source IDs
    source_ids = sorted(set(row["source_id"] for row in rows))
    rng = random.Random(seed)
    rng.shuffle(source_ids)

    n = len(source_ids)
    n_train = int(n * train_ratio)
    n_val = int(n * val_ratio)
    # test gets the remainder

    train_ids = set(source_ids[:n_train])
    val_ids = set(source_ids[n_train:n_train + n_val])
    test_ids = set(source_ids[n_train + n_val:])

    train_rows, val_rows, test_rows = [], [], []
    for row in rows:
        sid = row["source_id"]
        if sid in train_ids:
            row["split"] = "train"
            train_rows.append(row)
        elif sid in val_ids:
            row["split"] = "val"
            val_rows.append(row)
        else:
            row["split"] = "test"
            test_rows.append(row)

    return train_rows, val_rows, test_rows


def _compute_class_weights(rows: list[dict]) -> dict[str, float]:
    """
    Compute class weights using the 'balanced' strategy:
      weight_c = n_total / (n_classes * n_c)

    For 1000 real and 6000 fake videos (× 10 frames each):
      real: 70000 / (2 * 10000) = 3.5
      fake: 70000 / (2 * 60000) ≈ 0.583

    This is exactly what sklearn's class_weight='balanced' computes.
    """
    label_counts: dict[str, int] = {}
    for row in rows:
        label = row["label"]
        label_counts[label] = label_counts.get(label, 0) + 1

    n_total = len(rows)
    n_classes = len(label_counts)

    weights = {}
    for label, count in label_counts.items():
        weights[label] = n_total / (n_classes * count)

    return weights


def _save_split_csv(rows: list[dict], path: Path, fieldnames: list[str]) -> None:
    """Save rows to a CSV file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def run_extraction_pipeline(
    video_root: str | Path,
    output_root: str | Path,
    frames_per_video: int = 10,
    jpeg_quality: int = 95,
    train_ratio: float = 0.70,
    val_ratio: float = 0.15,
    test_ratio: float = 0.15,
    seed: int = 42,
    project_root: str | Path | None = None,
) -> dict[str, Any]:
    """
    Complete extraction pipeline.

    Steps:
      1. Extract frames from all videos → save as JPEG
      2. Build manifest.csv
      3. Split into train/val/test by source video ID
      4. Compute class weights
      5. Save everything

    Parameters
    ----------
    video_root:
        Path to the FF++ dataset root containing class directories
        (original/, Deepfakes/, Face2Face/, etc.).
    output_root:
        Where to save extracted frames and metadata.
    frames_per_video:
        Number of frames to extract per video (default 10).
    jpeg_quality:
        JPEG compression quality (default 95).
    train_ratio, val_ratio, test_ratio:
        Split ratios (must sum to 1.0).
    seed:
        Random seed for reproducible splits.
    project_root:
        Path to the project root (for saving splits). If None, auto-detected.

    Returns
    -------
    dict with summary statistics and file paths.
    """
    video_root = Path(video_root)
    output_root = Path(output_root)

    if project_root is None:
        # Auto-detect: this file is at src/data/extract_frames.py
        project_root = Path(__file__).resolve().parent.parent.parent
    project_root = Path(project_root)

    print("=" * 70)
    print("FF++ FRAME EXTRACTION PIPELINE")
    print("=" * 70)
    print(f"Video root      : {video_root}")
    print(f"Output root     : {output_root}")
    print(f"Frames/video    : {frames_per_video}")
    print(f"JPEG quality    : {jpeg_quality}")
    print(f"Split ratios    : {train_ratio}/{val_ratio}/{test_ratio}")
    print(f"Random seed     : {seed}")
    print("=" * 70)

    all_rows: list[dict] = []
    stats: dict[str, Any] = {"classes": {}}

    # Process REAL videos
    for cls in REAL_CLASSES:
        cls_dir = video_root / cls
        if not cls_dir.exists():
            logger.warning("Class directory not found: %s", cls_dir)
            continue

        print(f"\nProcessing REAL class: {cls}")
        t0 = time.time()
        rows = _process_class_videos(
            video_dir=cls_dir,
            output_dir=output_root / "real",
            label="real",
            manipulation_type="real",
            n_frames=frames_per_video,
            quality=jpeg_quality,
        )
        elapsed = time.time() - t0
        n_videos = len(set(r["video_file"] for r in rows))
        print(f"  -> {n_videos} videos, {len(rows)} frames extracted ({elapsed:.1f}s)")
        stats["classes"][cls] = {"videos": n_videos, "frames": len(rows)}
        all_rows.extend(rows)

    # Process FAKE videos
    for cls in FAKE_CLASSES:
        cls_dir = video_root / cls
        if not cls_dir.exists():
            logger.warning("Class directory not found: %s", cls_dir)
            continue

        print(f"\nProcessing FAKE class: {cls}")
        t0 = time.time()
        rows = _process_class_videos(
            video_dir=cls_dir,
            output_dir=output_root / "fake",
            label="fake",
            manipulation_type=cls,
            n_frames=frames_per_video,
            quality=jpeg_quality,
        )
        elapsed = time.time() - t0
        n_videos = len(set(r["video_file"] for r in rows))
        print(f"  -> {n_videos} videos, {len(rows)} frames extracted ({elapsed:.1f}s)")
        stats["classes"][cls] = {"videos": n_videos, "frames": len(rows)}
        all_rows.extend(rows)

    if not all_rows:
        print("\nERROR: No frames were extracted! Check video_root path.")
        return {"error": "No frames extracted"}

    # --- Manifest ---
    fieldnames = [
        "file_path", "label", "modality", "dataset", "source_id",
        "person_id", "speaker_id", "manipulation_type", "compression",
        "split", "generator", "video_file",
    ]

    manifest_path = output_root / "manifest.csv"
    _save_split_csv(all_rows, manifest_path, fieldnames)
    print(f"\nManifest saved: {manifest_path} ({len(all_rows)} rows)")

    # --- Splits ---
    train_rows, val_rows, test_rows = _split_by_source_id(
        all_rows,
        train_ratio=train_ratio,
        val_ratio=val_ratio,
        test_ratio=test_ratio,
        seed=seed,
    )

    # Also save manifest with split column filled
    _save_split_csv(all_rows, manifest_path, fieldnames)

    # Save split CSVs to project data/splits/ (for training pipeline)
    splits_dir = project_root / "data" / "splits"
    _save_split_csv(train_rows, splits_dir / "train.csv", fieldnames)
    _save_split_csv(val_rows, splits_dir / "val.csv", fieldnames)
    _save_split_csv(test_rows, splits_dir / "test.csv", fieldnames)

    # Also copy to output_root for portability
    out_splits_dir = output_root / "splits"
    _save_split_csv(train_rows, out_splits_dir / "train.csv", fieldnames)
    _save_split_csv(val_rows, out_splits_dir / "val.csv", fieldnames)
    _save_split_csv(test_rows, out_splits_dir / "test.csv", fieldnames)

    # --- Class weights ---
    class_weights = _compute_class_weights(all_rows)
    train_class_weights = _compute_class_weights(train_rows)

    weights_info = {
        "overall": class_weights,
        "train_split": train_class_weights,
        "label_map": {"real": 0, "fake": 1},
        "pos_weight_for_bce": class_weights.get("real", 1.0) / max(class_weights.get("fake", 1.0), 1e-6),
        "description": (
            "pos_weight_for_bce is used with BCEWithLogitsLoss(pos_weight=...) "
            "to upweight the minority class (real). Since label=1 means fake, "
            "and fake is the majority, we actually want to upweight real. "
            "For BCEWithLogitsLoss, pos_weight > 1 upweights the positive class (fake=1). "
            "Since real is the minority, we use the INVERSE: weight for label=1 (fake) "
            "should be < 1, equivalent to giving real higher weight. "
            "USE the class_weights_tensor in CrossEntropyLoss or the pos_weight in BCE."
        ),
    }

    # Save class weights
    weights_path = output_root / "class_weights.json"
    with open(weights_path, "w", encoding="utf-8") as f:
        json.dump(weights_info, f, indent=2)

    # Also save to project
    project_weights_path = project_root / "data" / "class_weights.json"
    project_weights_path.parent.mkdir(parents=True, exist_ok=True)
    with open(project_weights_path, "w", encoding="utf-8") as f:
        json.dump(weights_info, f, indent=2)

    # --- Summary ---
    n_real = sum(1 for r in all_rows if r["label"] == "real")
    n_fake = sum(1 for r in all_rows if r["label"] == "fake")

    print("\n" + "=" * 70)
    print("EXTRACTION COMPLETE")
    print("=" * 70)
    print(f"Total frames    : {len(all_rows)}")
    print(f"  Real          : {n_real}")
    print(f"  Fake          : {n_fake}")
    print(f"  Ratio (fake/real): {n_fake / max(n_real, 1):.1f}x")
    print(f"\nSplit sizes:")
    print(f"  Train         : {len(train_rows)} frames")
    print(f"  Val           : {len(val_rows)} frames")
    print(f"  Test          : {len(test_rows)} frames")
    print(f"\nClass weights (balanced):")
    for label, weight in sorted(class_weights.items()):
        print(f"  {label:<8}: {weight:.4f}")
    print(f"\nBCEWithLogitsLoss pos_weight: {weights_info['pos_weight_for_bce']:.4f}")
    print(f"\nFiles saved:")
    print(f"  Frames        : {output_root}/real/  and  {output_root}/fake/")
    print(f"  Manifest      : {manifest_path}")
    print(f"  Splits (proj) : {splits_dir}/")
    print(f"  Splits (copy) : {out_splits_dir}/")
    print(f"  Class weights : {weights_path}")
    print(f"  Class weights : {project_weights_path}")
    print("=" * 70)

    stats.update({
        "total_frames": len(all_rows),
        "n_real": n_real,
        "n_fake": n_fake,
        "train_size": len(train_rows),
        "val_size": len(val_rows),
        "test_size": len(test_rows),
        "class_weights": class_weights,
        "output_root": str(output_root),
        "manifest_path": str(manifest_path),
        "splits_dir": str(splits_dir),
    })

    return stats


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract frames from FF++ videos and prepare training data."
    )
    parser.add_argument(
        "--video-root", type=str, required=True,
        help="Path to FF++ dataset root (containing original/, Deepfakes/, etc.)",
    )
    parser.add_argument(
        "--output-root", type=str, required=True,
        help="Where to save extracted frames and metadata.",
    )
    parser.add_argument(
        "--frames-per-video", type=int, default=10,
        help="Number of frames to extract per video (default: 10).",
    )
    parser.add_argument(
        "--jpeg-quality", type=int, default=95,
        help="JPEG compression quality 1-100 (default: 95).",
    )
    parser.add_argument(
        "--train-ratio", type=float, default=0.70,
    )
    parser.add_argument(
        "--val-ratio", type=float, default=0.15,
    )
    parser.add_argument(
        "--test-ratio", type=float, default=0.15,
    )
    parser.add_argument(
        "--seed", type=int, default=42,
    )
    return parser.parse_args()


if __name__ == "__main__":
    import sys
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    )

    args = _parse_args()
    run_extraction_pipeline(
        video_root=args.video_root,
        output_root=args.output_root,
        frames_per_video=args.frames_per_video,
        jpeg_quality=args.jpeg_quality,
        train_ratio=args.train_ratio,
        val_ratio=args.val_ratio,
        test_ratio=args.test_ratio,
        seed=args.seed,
    )
