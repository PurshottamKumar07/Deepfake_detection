"""
src/data/build_manifest.py
===========================
Generates the central dataset manifest (data/manifest.csv).

The manifest is the single source of truth for training and evaluation.
It records per-file metadata without modifying the original dataset.

Manifest fields:
  file_path        - path to the media file (absolute or relative to DATA_ROOT)
  label            - binary label: 'real' or 'fake'
  modality         - 'image', 'video', or 'audio'
  dataset          - dataset name tag
  source_id        - inferred source video ID (empty if cannot be determined)
  person_id        - person identity (empty if unknown)
  speaker_id       - speaker identity for audio (empty if unknown)
  manipulation_type - e.g. 'DeepFakes', 'FaceSwap', 'real', etc.
  compression      - compression level (empty if unknown)
  split            - 'train', 'val', 'test', or '' (filled by split step)
  generator        - generator/manipulation method (empty if unknown)

IMPORTANT:
  - This script does NOT invent metadata.
  - Fields that cannot be determined are left empty/null.
  - The script reports which fields it could and could not fill.
  - real_classes and fake_classes MUST be set in config.yaml
    (or passed explicitly) after running inspect_dataset.py.

Usage (local):
    export DATA_ROOT=/path/to/your/dataset
    python -m src.data.build_manifest

    # With explicit class names (after running inspect_dataset.py):
    python -m src.data.build_manifest \\
        --real-classes original \\
        --fake-classes DeepFakes FaceSwap Face2Face FaceShifter NeuralTextures

Usage (Colab):
    import os
    os.environ["DATA_ROOT"] = "/content/drive/MyDrive/deepfake_dataset"

    from src.data.build_manifest import build_manifest
    df = build_manifest(
        real_classes=["original"],
        fake_classes=["DeepFakes", "FaceSwap", "Face2Face"],
    )
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Extension sets (must stay in sync with inspect_dataset.py)
# ---------------------------------------------------------------------------
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tiff", ".tif"}
VIDEO_EXTENSIONS = {".mp4", ".avi", ".mov", ".mkv", ".webm", ".flv", ".wmv"}
AUDIO_EXTENSIONS = {".wav", ".flac", ".mp3", ".ogg", ".m4a", ".aac"}

ALL_SUPPORTED_EXTENSIONS = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS | AUDIO_EXTENSIONS

# ---------------------------------------------------------------------------
# Source-ID extraction patterns
# FF++ frames typically: 000_003_frame_0001.png  ->  source_id = "000_003"
# ---------------------------------------------------------------------------
_SOURCE_ID_PATTERNS: list[tuple[str, str]] = [
    (r"^(\d{3}_\d{3})", "ff++_pair_prefix"),       # FF++ style
    (r"^(\d+)_", "leading_number_underscore"),       # generic numeric prefix
]


def _infer_source_id(filename: str) -> str:
    """
    Attempt to extract a source video ID from a filename.
    Returns empty string if no reliable pattern matches.
    """
    for pattern, _ in _SOURCE_ID_PATTERNS:
        m = re.match(pattern, filename)
        if m:
            return m.group(1)
    return ""


def _infer_modality(suffix: str) -> str:
    """Return 'image', 'video', 'audio', or 'unknown' based on extension."""
    lower = suffix.lower()
    if lower in IMAGE_EXTENSIONS:
        return "image"
    if lower in VIDEO_EXTENSIONS:
        return "video"
    if lower in AUDIO_EXTENSIONS:
        return "audio"
    return "unknown"


def _parse_compression_from_path(path: Path, root: Path) -> str:
    """
    Try to infer compression level from directory path components.
    FF++ often has paths like: .../c0/DeepFakes/...  or .../c23/...
    Returns compression string or empty string.
    """
    try:
        rel = path.relative_to(root)
        parts = [p.lower() for p in rel.parts]
    except ValueError:
        return ""

    # Check for FF++ compression directories: c0, c23, c40, raw, hq, lq
    for part in parts:
        if re.match(r"^c\d+$", part) or part in {"raw", "hq", "lq"}:
            return part
    return ""


class ManifestBuildError(RuntimeError):
    """Raised when manifest generation cannot proceed safely."""


def build_manifest(
    data_root: str | Path | None = None,
    real_classes: list[str] | None = None,
    fake_classes: list[str] | None = None,
    output_path: str | Path | None = None,
    dataset_name: str | None = None,
    config_path: str | Path | None = None,
    relative_paths: bool = True,
) -> Any:  # returns pd.DataFrame
    """
    Scan the dataset and build the manifest CSV.

    Parameters
    ----------
    data_root:
        Dataset root directory. If None, resolved from env/config.
    real_classes:
        List of class directory names that are 'real'.
        If None, reads from config (data.real_classes).
        MUST be non-empty or an error is raised.
    fake_classes:
        List of class directory names that are 'fake'.
        If None, reads from config (data.fake_classes).
        MUST be non-empty or an error is raised.
    output_path:
        Where to save the manifest CSV. Defaults to data/manifest.csv.
    dataset_name:
        Dataset name tag. Defaults to config value or 'unknown'.
    config_path:
        Optional path to config.yaml override.
    relative_paths:
        If True, file_path is relative to DATA_ROOT. If False, absolute.

    Returns
    -------
    pd.DataFrame
        The manifest as a DataFrame.

    Raises
    ------
    ManifestBuildError
        If real_classes or fake_classes cannot be determined, or if the
        class directories don't exist.
    """
    try:
        import pandas as pd
    except ImportError as exc:
        raise ManifestBuildError(
            "pandas is required. Install with: pip install pandas"
        ) from exc

    from src.common.config_loader import (
        get_data_root,
        get_project_root,
        load_config,
    )

    cfg = load_config(config_path)
    data_cfg = cfg.get("data", {})

    # Resolve data root
    if data_root is not None:
        root = Path(data_root)
    else:
        root = get_data_root(cfg)

    # Resolve class lists
    if real_classes is None:
        real_classes = data_cfg.get("real_classes") or []
    if fake_classes is None:
        fake_classes = data_cfg.get("fake_classes") or []

    if not real_classes and not fake_classes:
        raise ManifestBuildError(
            "Neither real_classes nor fake_classes are specified.\n"
            "You must run inspect_dataset.py first to determine the actual class names,\n"
            "then either:\n"
            "  (a) Set data.real_classes and data.fake_classes in configs/config.yaml\n"
            "  (b) Pass them explicitly to build_manifest()\n"
            "  (c) Use the --real-classes / --fake-classes CLI flags\n\n"
            "Do NOT guess class names."
        )

    if not real_classes:
        raise ManifestBuildError(
            f"real_classes is empty. All detected fake classes: {fake_classes}.\n"
            "At least one real class directory must be specified."
        )

    if not fake_classes:
        raise ManifestBuildError(
            f"fake_classes is empty. All detected real classes: {real_classes}.\n"
            "At least one fake class directory must be specified."
        )

    # Validate class directories exist
    all_classes = real_classes + fake_classes
    missing_dirs = [cls for cls in all_classes if not (root / cls).exists()]
    if missing_dirs:
        raise ManifestBuildError(
            f"The following class directories were not found under {root}:\n"
            + "\n".join(f"  {root / cls}" for cls in missing_dirs)
            + "\n\nCheck that DATA_ROOT and class names are correct."
        )

    # Dataset name
    if dataset_name is None:
        dataset_name = data_cfg.get("dataset_name") or "unknown"

    # Output path
    project_root = get_project_root()
    if output_path is None:
        output_path = project_root / data_cfg.get("manifest_path", "data/manifest.csv")
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    logger.info("=" * 60)
    logger.info("Building dataset manifest")
    logger.info("DATA_ROOT      : %s", root)
    logger.info("Real classes   : %s", real_classes)
    logger.info("Fake classes   : %s", fake_classes)
    logger.info("Output path    : %s", output_path)
    logger.info("=" * 60)

    rows: list[dict[str, Any]] = []
    skipped_unsupported = 0
    field_fill_stats: dict[str, int] = {
        "source_id": 0,
        "compression": 0,
    }

    # --- Build rows ---
    for cls in all_classes:
        label = "real" if cls in real_classes else "fake"
        manipulation_type = cls if cls in fake_classes else "real"
        cls_dir = root / cls

        for file_path in sorted(cls_dir.rglob("*")):
            if not file_path.is_file():
                continue

            suffix = file_path.suffix.lower()
            if suffix not in ALL_SUPPORTED_EXTENSIONS:
                skipped_unsupported += 1
                continue

            modality = _infer_modality(suffix)

            # Relative or absolute path
            if relative_paths:
                try:
                    stored_path = str(file_path.relative_to(root))
                except ValueError:
                    stored_path = str(file_path)
            else:
                stored_path = str(file_path)

            source_id = _infer_source_id(file_path.name)
            if source_id:
                field_fill_stats["source_id"] += 1

            compression = _parse_compression_from_path(file_path, root)
            if compression:
                field_fill_stats["compression"] += 1

            rows.append({
                "file_path": stored_path,
                "label": label,
                "modality": modality,
                "dataset": dataset_name,
                "source_id": source_id,
                "person_id": "",       # Cannot be determined without additional metadata
                "speaker_id": "",      # Cannot be determined without additional metadata
                "manipulation_type": manipulation_type,
                "compression": compression,
                "split": "",           # Filled by the split step
                "generator": manipulation_type if label == "fake" else "",
            })

    if not rows:
        raise ManifestBuildError(
            f"No supported files found under {root} in classes {all_classes}.\n"
            "Check that file extensions match supported types."
        )

    df = pd.DataFrame(rows)

    # --- Report field fill rates ---
    n = len(df)
    fill_report: dict[str, str] = {}
    for field in ["source_id", "person_id", "speaker_id", "compression", "generator"]:
        n_filled = (df[field] != "").sum()
        pct = 100 * n_filled / max(n, 1)
        fill_report[field] = f"{n_filled}/{n} ({pct:.1f}%)"

    # --- Save manifest ---
    df.to_csv(output_path, index=False)

    # --- Print summary ---
    _print_manifest_summary(df, fill_report, skipped_unsupported, output_path)

    return df


def _print_manifest_summary(
    df: Any,
    fill_report: dict[str, str],
    skipped: int,
    output_path: Path,
) -> None:
    """Print a human-readable manifest build summary."""
    print("\n" + "=" * 70)
    print("MANIFEST BUILD SUMMARY")
    print("=" * 70)
    print(f"Total rows     : {len(df)}")
    print(f"Skipped files  : {skipped} (unsupported extension)")
    print()

    print("Label distribution:")
    for label, count in df["label"].value_counts().items():
        pct = 100 * count / max(len(df), 1)
        print(f"  {label:<8}: {count:>7} ({pct:.1f}%)")

    print("\nModality distribution:")
    for mod, count in df["modality"].value_counts().items():
        print(f"  {mod:<8}: {count:>7}")

    print("\nManipulation type distribution:")
    for mt, count in df["manipulation_type"].value_counts().items():
        pct = 100 * count / max(len(df), 1)
        print(f"  {mt:<30}: {count:>7} ({pct:.1f}%)")

    print("\nMetadata field fill rates:")
    for field, summary in fill_report.items():
        print(f"  {field:<20}: {summary}")

    print(f"\nManifest saved: {output_path}")

    print("\nNOTES:")
    print("  - person_id and speaker_id are EMPTY: cannot be determined from filenames alone.")
    print("    Provide a supplementary metadata file to fill these fields.")
    print("  - source_id was inferred from filename patterns (may not be 100% accurate).")
    print("  - 'split' column is empty — run build_splits.py to assign train/val/test.")
    print("=" * 70 + "\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build the dataset manifest CSV from DATA_ROOT."
    )
    parser.add_argument("--data-root", type=str, default=None)
    parser.add_argument(
        "--real-classes",
        nargs="+",
        default=None,
        metavar="CLASS",
        help="Class directory names to label as 'real'. E.g.: --real-classes original",
    )
    parser.add_argument(
        "--fake-classes",
        nargs="+",
        default=None,
        metavar="CLASS",
        help="Class directory names to label as 'fake'. "
             "E.g.: --fake-classes DeepFakes FaceSwap Face2Face",
    )
    parser.add_argument("--output-path", type=str, default=None)
    parser.add_argument("--dataset-name", type=str, default=None)
    parser.add_argument("--config", type=str, default=None)
    parser.add_argument(
        "--absolute-paths",
        action="store_true",
        help="Store absolute file paths instead of paths relative to DATA_ROOT.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    )

    args = _parse_args()
    build_manifest(
        data_root=args.data_root,
        real_classes=args.real_classes,
        fake_classes=args.fake_classes,
        output_path=args.output_path,
        dataset_name=args.dataset_name,
        config_path=args.config,
        relative_paths=not args.absolute_paths,
    )
