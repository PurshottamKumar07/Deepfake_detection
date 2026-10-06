"""
src/data/inspect_dataset.py
============================
Dataset inspection tool for the deepfake-forensics project.

Inspects the dataset at DATA_ROOT and reports:
  - Directory structure (top 3 levels)
  - Detected class directories (subdirectory names)
  - File counts per class
  - File extensions
  - Sample file names per class
  - Corrupted/unreadable files
  - Image dimensions (sampled subset)
  - Whether source IDs can be inferred from filenames
  - Obvious inconsistencies

IMPORTANT:
  This script NEVER modifies the original dataset.
  It reads files in a read-only manner.

Usage (local):
    # Set DATA_ROOT first
    export DATA_ROOT=/path/to/your/dataset

    python -m src.data.inspect_dataset

    # Or with explicit arguments:
    python -m src.data.inspect_dataset \\
        --data-root /path/to/dataset \\
        --output-dir results/dataset_inspection \\
        --sample-size 10 \\
        --check-images

Usage (Colab):
    import os
    os.environ["DATA_ROOT"] = "/content/drive/MyDrive/deepfake_dataset"

    from src.data.inspect_dataset import run_inspection
    report = run_inspection(check_images=True, sample_size=10)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Extensions this tool treats as image files
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tiff", ".tif"}

# Extensions treated as video files
VIDEO_EXTENSIONS = {".mp4", ".avi", ".mov", ".mkv", ".webm", ".flv", ".wmv"}

# Extensions treated as audio files
AUDIO_EXTENSIONS = {".wav", ".flac", ".mp3", ".ogg", ".m4a", ".aac"}

# Pattern heuristics to try to infer a source video / sequence ID from filenames.
# FF++ frames often look like: 000_003_frame_0001.png  or  000_003_0001.png
# We try several patterns and report which ones match.
_SOURCE_ID_PATTERNS: list[tuple[str, str]] = [
    # FF++ style: leading numeric groups separated by underscores
    (r"^(\d{3}_\d{3})", "ff++_pair_prefix"),
    # Generic: first numeric run in filename
    (r"^(\d+)", "leading_number"),
    # Frame index: contains _frame_ keyword
    (r"_frame_(\d+)", "frame_keyword"),
    # Sequence: "seq" or "vid" followed by number
    (r"(?:seq|vid|video|clip)[\-_]?(\d+)", "seq_vid_prefix"),
]


# ---------------------------------------------------------------------------
# Core inspection functions
# ---------------------------------------------------------------------------


def _safe_read_image(path: Path) -> tuple[bool, tuple[int, int] | None, str]:
    """
    Attempt to open an image and return (success, (h, w), error_message).
    Returns without raising.
    """
    try:
        from PIL import Image  # type: ignore[import]

        with Image.open(path) as img:
            w, h = img.size
            img.verify()  # checks for file truncation
        # Re-open after verify (verify closes the file)
        with Image.open(path) as img:
            w, h = img.size
        return True, (h, w), ""
    except Exception as exc:
        return False, None, str(exc)


def _scan_directory_tree(root: Path, max_depth: int = 3) -> list[dict[str, Any]]:
    """
    Walk the directory tree up to ``max_depth`` and return a list of
    {path, depth, is_dir, n_files} records.  Does NOT recurse into files.
    """
    entries: list[dict[str, Any]] = []

    def _walk(current: Path, depth: int) -> None:
        if depth > max_depth:
            return
        try:
            children = sorted(current.iterdir())
        except PermissionError:
            return

        for child in children:
            if child.is_dir():
                try:
                    n_files = sum(1 for _ in child.rglob("*") if _.is_file())
                except Exception:
                    n_files = -1
                entries.append(
                    {
                        "path": str(child.relative_to(root)),
                        "depth": depth,
                        "is_dir": True,
                        "n_files": n_files,
                    }
                )
                if depth < max_depth:
                    _walk(child, depth + 1)

    _walk(root, 1)
    return entries


def _detect_classes(root: Path) -> list[str]:
    """
    Return immediate child directory names of root.
    These are the candidate class/category directories.
    """
    if not root.is_dir():
        return []
    return sorted(
        d.name
        for d in root.iterdir()
        if d.is_dir() and not d.name.startswith(".")
    )


def _count_files_per_class(
    root: Path, classes: list[str]
) -> dict[str, dict[str, int]]:
    """
    For each class directory, count files grouped by extension.

    Returns:
        {class_name: {".ext": count, ...}}
    """
    result: dict[str, dict[str, int]] = {}
    for cls in classes:
        cls_dir = root / cls
        ext_counts: dict[str, int] = defaultdict(int)
        for f in cls_dir.rglob("*"):
            if f.is_file():
                ext_counts[f.suffix.lower()] += 1
        result[cls] = dict(ext_counts)
    return result


def _sample_files(root: Path, cls: str, n: int = 10) -> list[str]:
    """Return up to n sample file names from a class directory."""
    cls_dir = root / cls
    files = [f for f in cls_dir.rglob("*") if f.is_file()]
    files = sorted(files)[:n]
    return [f.name for f in files]


def _check_source_id_patterns(filenames: list[str]) -> dict[str, Any]:
    """
    Try each source-ID heuristic pattern against a list of filenames.
    Return a report: which patterns matched and what unique groups were found.
    """
    report: dict[str, Any] = {}
    for pattern, label in _SOURCE_ID_PATTERNS:
        matches = []
        unique_ids: set[str] = set()
        for fn in filenames:
            m = re.search(pattern, fn, re.IGNORECASE)
            if m:
                matches.append(fn)
                unique_ids.add(m.group(1))
        report[label] = {
            "pattern": pattern,
            "n_matched": len(matches),
            "n_unique_ids": len(unique_ids),
            "coverage_pct": round(100 * len(matches) / max(len(filenames), 1), 1),
            "sample_ids": sorted(unique_ids)[:10],
        }
    return report


def _check_image_corruption(
    paths: list[Path], sample_size: int = 200
) -> dict[str, Any]:
    """
    Check up to sample_size images for readability.
    Returns stats: n_checked, n_ok, n_corrupted, corrupted_list, sample_dims.
    """
    sampled = paths[:sample_size]
    n_ok = 0
    corrupted: list[str] = []
    dims: list[tuple[int, int]] = []

    for p in sampled:
        ok, dim, err = _safe_read_image(p)
        if ok and dim is not None:
            n_ok += 1
            dims.append(dim)
        else:
            corrupted.append(f"{p.name}: {err}")

    unique_dims = list({d for d in dims})
    return {
        "n_checked": len(sampled),
        "n_ok": n_ok,
        "n_corrupted": len(corrupted),
        "corrupted_files": corrupted[:20],  # cap list length
        "unique_dimensions": sorted(unique_dims),
    }


def _compute_duplicate_check(paths: list[Path], max_files: int = 5000) -> dict[str, Any]:
    """
    Hash a sample of files to detect obvious duplicates.
    Uses fast partial hash (first 64KB) to avoid reading entire large files.
    """
    sampled = paths[:max_files]
    seen: dict[str, list[str]] = defaultdict(list)

    for p in sampled:
        try:
            with open(p, "rb") as fh:
                chunk = fh.read(65536)
            h = hashlib.md5(chunk).hexdigest()
            seen[h].append(p.name)
        except Exception:
            pass

    duplicates = {h: names for h, names in seen.items() if len(names) > 1}
    n_duplicate_files = sum(len(v) - 1 for v in duplicates.values())

    return {
        "n_sampled": len(sampled),
        "n_duplicate_groups": len(duplicates),
        "n_estimated_duplicate_files": n_duplicate_files,
        "duplicate_examples": [
            {"hash": h, "files": v[:5]} for h, v in list(duplicates.items())[:5]
        ],
    }


# ---------------------------------------------------------------------------
# Main inspection entry point
# ---------------------------------------------------------------------------


def run_inspection(
    data_root: str | Path | None = None,
    output_dir: str | Path | None = None,
    sample_size: int = 10,
    check_images: bool = True,
    check_duplicates: bool = True,
    config_path: str | Path | None = None,
) -> dict[str, Any]:
    """
    Run the full dataset inspection.

    Parameters
    ----------
    data_root:
        Path to the dataset root. If None, resolved from env/config.
    output_dir:
        Where to save JSON/text reports. If None, uses results/dataset_inspection.
    sample_size:
        Number of sample filenames to include per class in the report.
    check_images:
        Whether to open image files to verify readability and dimensions.
    check_duplicates:
        Whether to run a partial-hash duplicate check.
    config_path:
        Optional path to config.yaml override.

    Returns
    -------
    dict
        Full inspection report as a Python dict (also saved to disk).
    """
    from src.common.config_loader import (
        get_data_root,
        get_output_dir,
        get_project_root,
        load_config,
    )

    cfg = load_config(config_path)

    # Resolve data root
    if data_root is not None:
        root = Path(data_root)
    else:
        root = get_data_root(cfg)

    # Resolve output directory
    if output_dir is not None:
        out_dir = Path(output_dir)
    else:
        out_dir = get_project_root() / cfg.get("outputs", {}).get(
            "dataset_inspection_dir", "results/dataset_inspection"
        )
    out_dir.mkdir(parents=True, exist_ok=True)

    logger.info("=" * 60)
    logger.info("Dataset Inspection")
    logger.info("DATA_ROOT: %s", root)
    logger.info("=" * 60)

    report: dict[str, Any] = {
        "data_root": str(root),
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "warnings": [],
        "errors": [],
    }

    # 1. Directory tree (top 3 levels)
    logger.info("Scanning directory tree...")
    tree = _scan_directory_tree(root, max_depth=3)
    report["directory_tree"] = tree

    # 2. Detect candidate class directories
    classes = _detect_classes(root)
    report["detected_classes"] = classes
    report["n_classes"] = len(classes)

    if not classes:
        msg = (
            f"No subdirectories found directly under DATA_ROOT: {root}\n"
            "The dataset may use a different structure (e.g., nested subdirs, "
            "flat layout). Inspect manually."
        )
        report["errors"].append(msg)
        logger.error(msg)
        _save_report(report, out_dir)
        return report

    logger.info("Detected %d candidate class directories: %s", len(classes), classes)

    # 3. File counts per class
    logger.info("Counting files per class...")
    counts = _count_files_per_class(root, classes)
    report["files_per_class"] = counts

    total_files = sum(
        sum(ext_counts.values()) for ext_counts in counts.values()
    )
    report["total_files"] = total_files
    logger.info("Total files: %d", total_files)

    for cls, ext_counts in counts.items():
        n = sum(ext_counts.values())
        logger.info("  %-30s  %6d files  extensions: %s", cls, n, dict(ext_counts))

    # 4. Class imbalance check
    class_totals = {cls: sum(v.values()) for cls, v in counts.items()}
    if class_totals:
        max_cls = max(class_totals, key=class_totals.get)  # type: ignore[arg-type]
        min_cls = min(class_totals, key=class_totals.get)  # type: ignore[arg-type]
        ratio = class_totals[max_cls] / max(class_totals[min_cls], 1)
        report["class_imbalance"] = {
            "max_class": max_cls,
            "max_count": class_totals[max_cls],
            "min_class": min_cls,
            "min_count": class_totals[min_cls],
            "imbalance_ratio": round(ratio, 2),
        }
        if ratio > 5:
            msg = (
                f"Significant class imbalance detected: '{max_cls}' has "
                f"{class_totals[max_cls]} files vs '{min_cls}' with "
                f"{class_totals[min_cls]} files (ratio {ratio:.1f}x)."
            )
            report["warnings"].append(msg)
            logger.warning(msg)

    # 5. Sample filenames per class
    logger.info("Collecting sample filenames...")
    samples: dict[str, list[str]] = {}
    for cls in classes:
        samples[cls] = _sample_files(root, cls, n=sample_size)
    report["sample_files_per_class"] = samples

    # 6. Source ID pattern analysis
    logger.info("Analysing filename patterns for source ID inference...")
    all_filenames: list[str] = []
    for cls in classes:
        cls_dir = root / cls
        all_filenames.extend(
            f.name for f in sorted(cls_dir.rglob("*"))[:2000] if f.is_file()
        )

    source_id_report = _check_source_id_patterns(all_filenames)
    report["source_id_pattern_analysis"] = source_id_report

    # Summarise source ID findings
    best_pattern = max(
        source_id_report.items(),
        key=lambda kv: kv[1]["n_unique_ids"],
        default=(None, {"n_unique_ids": 0}),
    )
    if best_pattern[1]["n_unique_ids"] > 1:
        report["source_id_recommendation"] = (
            f"Best pattern: '{best_pattern[0]}' "
            f"({best_pattern[1]['n_unique_ids']} unique IDs, "
            f"{best_pattern[1]['coverage_pct']}% coverage). "
            "Recommend using this pattern to extract source IDs for leakage-safe splitting."
        )
        logger.info("Source ID pattern recommendation: %s", report["source_id_recommendation"])
    else:
        msg = (
            "No reliable source ID pattern found in filenames. "
            "Leakage-safe splitting by source video will NOT be possible "
            "without additional metadata. Consider providing a source ID "
            "mapping file, or re-examining the directory structure."
        )
        report["source_id_recommendation"] = msg
        report["warnings"].append(msg)
        logger.warning(msg)

    # 7. Collect all image file paths for further checks
    all_image_paths: list[Path] = []
    for cls in classes:
        cls_dir = root / cls
        for f in cls_dir.rglob("*"):
            if f.is_file() and f.suffix.lower() in IMAGE_EXTENSIONS:
                all_image_paths.append(f)

    report["total_image_files"] = len(all_image_paths)

    # 8. Image readability + dimension check
    if check_images and all_image_paths:
        logger.info(
            "Checking image readability (sampling up to 200 per class)..."
        )
        per_class_img: dict[str, Any] = {}
        for cls in classes:
            cls_paths = [
                p for p in all_image_paths
                if p.parts[-2] == cls or str(p).startswith(str(root / cls))
            ]
            img_report = _check_image_corruption(cls_paths, sample_size=200)
            per_class_img[cls] = img_report
            if img_report["n_corrupted"] > 0:
                msg = (
                    f"Class '{cls}': {img_report['n_corrupted']} corrupted/"
                    f"unreadable images out of {img_report['n_checked']} checked."
                )
                report["warnings"].append(msg)
                logger.warning(msg)
        report["image_readability"] = per_class_img
    elif check_images:
        report["image_readability"] = {}
        report["warnings"].append(
            "No image files found at DATA_ROOT. "
            "Check that extensions match IMAGE_EXTENSIONS."
        )

    # 9. Duplicate file check
    if check_duplicates and all_image_paths:
        logger.info("Running partial-hash duplicate check (up to 5000 files)...")
        dup_report = _compute_duplicate_check(all_image_paths, max_files=5000)
        report["duplicate_check"] = dup_report
        if dup_report["n_duplicate_groups"] > 0:
            msg = (
                f"Detected ~{dup_report['n_estimated_duplicate_files']} "
                f"possible duplicate files across {dup_report['n_duplicate_groups']} groups "
                "(partial hash match — verify manually)."
            )
            report["warnings"].append(msg)
            logger.warning(msg)

    # 10. Manipulation type inference
    _add_manipulation_inference(classes, report)

    # 11. Summary
    _print_summary(report)

    # Save reports
    _save_report(report, out_dir)

    return report


def _add_manipulation_inference(classes: list[str], report: dict[str, Any]) -> None:
    """
    Based on detected class names, try to infer which are real vs fake
    and what manipulation types are present.

    IMPORTANT: This is heuristic. Results must be confirmed by the user.
    """
    # Known FF++ manipulation type names (case-insensitive matching)
    known_fake_types = {
        "deepfakes", "faceswap", "face2face", "faceshifter", "neuraltextures",
        "faceswapkowalski", "df", "fs", "f2f", "fsgan", "deepfakedetection",
    }
    known_real_types = {"original", "real", "youtube", "actors"}

    inferred_real: list[str] = []
    inferred_fake: list[str] = []
    uncertain: list[str] = []

    for cls in classes:
        lower = cls.lower()
        if any(kw in lower for kw in known_real_types):
            inferred_real.append(cls)
        elif any(kw in lower for kw in known_fake_types):
            inferred_fake.append(cls)
        else:
            uncertain.append(cls)

    report["class_inference"] = {
        "inferred_real_classes": inferred_real,
        "inferred_fake_classes": inferred_fake,
        "uncertain_classes": uncertain,
        "note": (
            "IMPORTANT: These are heuristic guesses based on class directory names. "
            "You MUST confirm these before generating the manifest. "
            "Set data.real_classes and data.fake_classes in configs/config.yaml "
            "after confirming."
        ),
    }

    if uncertain:
        logger.warning(
            "Could not infer real/fake for classes: %s — "
            "inspect manually and update configs/config.yaml.",
            uncertain,
        )


def _print_summary(report: dict[str, Any]) -> None:
    """Print a human-readable inspection summary to stdout."""
    print("\n" + "=" * 70)
    print("DATASET INSPECTION SUMMARY")
    print("=" * 70)
    print(f"DATA_ROOT         : {report['data_root']}")
    print(f"Total files       : {report.get('total_files', 'N/A')}")
    print(f"Image files       : {report.get('total_image_files', 'N/A')}")
    print(f"Detected classes  : {report.get('n_classes', 0)}")
    print()

    classes = report.get("detected_classes", [])
    fpc = report.get("files_per_class", {})
    print(f"{'CLASS':<30}  {'FILES':>8}  {'EXTENSIONS'}")
    print("-" * 70)
    for cls in classes:
        n = sum(fpc.get(cls, {}).values())
        exts = fpc.get(cls, {})
        print(f"  {cls:<28}  {n:>8}  {exts}")

    print()
    inf = report.get("class_inference", {})
    print(f"Inferred REAL classes : {inf.get('inferred_real_classes', [])}")
    print(f"Inferred FAKE classes : {inf.get('inferred_fake_classes', [])}")
    print(f"Uncertain classes     : {inf.get('uncertain_classes', [])}")

    src = report.get("source_id_recommendation", "")
    print(f"\nSource ID recommendation:\n  {src}")

    imb = report.get("class_imbalance", {})
    if imb:
        print(
            f"\nClass imbalance: {imb.get('max_class')} ({imb.get('max_count')} files) "
            f"vs {imb.get('min_class')} ({imb.get('min_count')} files) "
            f"ratio={imb.get('imbalance_ratio')}x"
        )

    warnings = report.get("warnings", [])
    if warnings:
        print(f"\n{'!':=<70}")
        print(f"  {len(warnings)} WARNING(S):")
        for i, w in enumerate(warnings, 1):
            print(f"  [{i}] {w}")

    errors = report.get("errors", [])
    if errors:
        print(f"\n{'ERROR':=<70}")
        for e in errors:
            print(f"  {e}")

    print("=" * 70 + "\n")


def _save_report(report: dict[str, Any], out_dir: Path) -> None:
    """Save the inspection report as JSON to output_dir."""
    ts = report.get("timestamp", time.strftime("%Y%m%d_%H%M%S")).replace(":", "").replace("-", "")
    json_path = out_dir / f"inspection_{ts}.json"
    latest_path = out_dir / "inspection_latest.json"

    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, ensure_ascii=False)
    with open(latest_path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, ensure_ascii=False)

    logger.info("Inspection report saved to: %s", json_path)
    print(f"Report saved: {json_path}")
    print(f"Latest copy : {latest_path}")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Inspect a deepfake dataset at DATA_ROOT."
    )
    parser.add_argument(
        "--data-root",
        type=str,
        default=None,
        help="Path to the dataset root. Overrides DATA_ROOT env var and config.",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default=None,
        help="Directory to save inspection reports. Defaults to results/dataset_inspection/.",
    )
    parser.add_argument(
        "--sample-size",
        type=int,
        default=10,
        help="Number of sample filenames to include per class (default: 10).",
    )
    parser.add_argument(
        "--no-image-check",
        action="store_true",
        help="Skip opening image files (faster, but won't detect corruption).",
    )
    parser.add_argument(
        "--no-duplicate-check",
        action="store_true",
        help="Skip duplicate file detection.",
    )
    parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="Path to config.yaml override.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    )

    args = _parse_args()
    run_inspection(
        data_root=args.data_root,
        output_dir=args.output_dir,
        sample_size=args.sample_size,
        check_images=not args.no_image_check,
        check_duplicates=not args.no_duplicate_check,
        config_path=args.config,
    )
