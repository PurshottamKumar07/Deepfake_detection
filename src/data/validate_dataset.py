"""
src/data/validate_dataset.py
============================
Automated dataset and manifest validation utilities.

Checks performed:
  1. File existence (paths in manifest actually exist)
  2. Duplicate paths in manifest
  3. Invalid / missing labels
  4. Corrupted / unreadable files (sampled)
  5. Class distribution
  6. Split distribution
  7. Source-level train/val/test leakage
  8. Unsupported file formats
  9. Empty split column (splits not yet assigned)

Produces a concise validation report (printed + optionally saved as JSON).

Usage:
    python -m src.data.validate_dataset

    python -m src.data.validate_dataset \\
        --manifest data/manifest.csv \\
        --data-root /path/to/data \\
        --output-dir results/validation
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

VALID_LABELS = {"real", "fake"}
VALID_MODALITIES = {"image", "video", "audio", "unknown"}
VALID_SPLITS = {"train", "val", "test", ""}

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tiff", ".tif"}


# ---------------------------------------------------------------------------
# Check functions — each returns a dict with 'passed', 'issues', 'stats'
# ---------------------------------------------------------------------------


def check_file_existence(
    df: Any, data_root: Path | None = None
) -> dict[str, Any]:
    """Check that every file_path in the manifest actually exists on disk."""
    missing: list[str] = []
    found = 0

    for path_str in df["file_path"]:
        if data_root is not None:
            p = data_root / path_str
        else:
            p = Path(path_str)

        if p.exists():
            found += 1
        else:
            missing.append(str(path_str))

    return {
        "check": "file_existence",
        "passed": len(missing) == 0,
        "stats": {"found": found, "missing": len(missing)},
        "issues": missing[:20],  # cap output
        "issue_count": len(missing),
    }


def check_duplicate_paths(df: Any) -> dict[str, Any]:
    """Check for duplicate file_path entries in the manifest."""
    dupes = df[df["file_path"].duplicated(keep=False)]["file_path"].unique().tolist()
    return {
        "check": "duplicate_paths",
        "passed": len(dupes) == 0,
        "stats": {"n_duplicates": len(dupes)},
        "issues": dupes[:20],
        "issue_count": len(dupes),
    }


def check_labels(df: Any) -> dict[str, Any]:
    """Check that all labels are valid ('real' or 'fake')."""
    invalid = df[~df["label"].isin(VALID_LABELS)]["label"].unique().tolist()
    missing = df[df["label"].isnull() | (df["label"] == "")].shape[0]
    return {
        "check": "labels",
        "passed": len(invalid) == 0 and missing == 0,
        "stats": {"invalid_values": invalid, "missing_count": missing},
        "issues": [f"Invalid labels: {invalid}"] if invalid else [],
        "issue_count": len(invalid) + missing,
    }


def check_class_distribution(df: Any) -> dict[str, Any]:
    """Report class distribution and flag severe imbalance."""
    dist = df["label"].value_counts().to_dict()
    total = len(df)
    issues: list[str] = []

    if total > 0:
        for label, count in dist.items():
            ratio = count / total
            if ratio > 0.95 or ratio < 0.05:
                issues.append(
                    f"Extreme class imbalance: '{label}' = {count}/{total} ({ratio:.1%})"
                )

    return {
        "check": "class_distribution",
        "passed": len(issues) == 0,
        "stats": dist,
        "issues": issues,
        "issue_count": len(issues),
    }


def check_split_distribution(df: Any) -> dict[str, Any]:
    """Report split distribution and check for missing splits."""
    dist = df["split"].value_counts().to_dict()
    issues: list[str] = []

    n_empty = (df["split"] == "").sum() + df["split"].isnull().sum()
    if n_empty > 0:
        issues.append(
            f"{n_empty} rows have no split assigned. "
            "Run build_splits.py to assign train/val/test."
        )

    for required in ["train", "val", "test"]:
        if required not in dist or dist[required] == 0:
            issues.append(f"Split '{required}' is empty or missing.")

    return {
        "check": "split_distribution",
        "passed": len(issues) == 0,
        "stats": dist,
        "issues": issues,
        "issue_count": len(issues),
    }


def check_source_leakage(df: Any) -> dict[str, Any]:
    """
    Check that no source_id appears in more than one split.
    Only checks rows where source_id is non-empty.
    """
    rows = df[df["source_id"].notna() & (df["source_id"] != "")].copy()
    if rows.empty or "split" not in rows.columns:
        return {
            "check": "source_leakage",
            "passed": True,
            "stats": {"n_source_ids_checked": 0},
            "issues": ["No source_id data available — leakage cannot be checked."],
            "issue_count": 0,
        }

    grouped = rows.groupby("source_id")["split"].nunique()
    leaked = grouped[grouped > 1].index.tolist()

    return {
        "check": "source_leakage",
        "passed": len(leaked) == 0,
        "stats": {
            "n_source_ids_checked": len(grouped),
            "n_leaked_source_ids": len(leaked),
        },
        "issues": [f"source_id '{sid}' appears in multiple splits" for sid in leaked[:10]],
        "issue_count": len(leaked),
    }


def check_unsupported_formats(df: Any) -> dict[str, Any]:
    """Flag rows where the file extension is not in a known supported set."""
    all_supported = {
        ".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tiff", ".tif",
        ".mp4", ".avi", ".mov", ".mkv", ".webm", ".flv", ".wmv",
        ".wav", ".flac", ".mp3", ".ogg", ".m4a", ".aac",
    }
    unsupported: list[str] = []
    for path_str in df["file_path"]:
        suffix = Path(str(path_str)).suffix.lower()
        if suffix and suffix not in all_supported:
            unsupported.append(path_str)

    return {
        "check": "unsupported_formats",
        "passed": len(unsupported) == 0,
        "stats": {"n_unsupported": len(unsupported)},
        "issues": unsupported[:10],
        "issue_count": len(unsupported),
    }


def check_readable_images(
    df: Any, data_root: Path | None = None, sample_size: int = 100
) -> dict[str, Any]:
    """
    Try to open a sample of image files to detect unreadable/corrupted files.
    Only checks files with image extensions.
    """
    try:
        from PIL import Image  # type: ignore[import]
    except ImportError:
        return {
            "check": "readable_images",
            "passed": True,
            "stats": {"n_checked": 0},
            "issues": ["Pillow not available — skipping readability check."],
            "issue_count": 0,
        }

    img_rows = df[
        df["file_path"].apply(
            lambda p: Path(str(p)).suffix.lower() in IMAGE_EXTENSIONS
        )
    ].head(sample_size)

    corrupted: list[str] = []
    checked = 0

    for path_str in img_rows["file_path"]:
        if data_root is not None:
            p = data_root / path_str
        else:
            p = Path(str(path_str))

        if not p.exists():
            continue

        try:
            with Image.open(p) as img:
                img.verify()
            checked += 1
        except Exception as exc:
            corrupted.append(f"{path_str}: {exc}")

    return {
        "check": "readable_images",
        "passed": len(corrupted) == 0,
        "stats": {"n_checked": checked, "n_corrupted": len(corrupted)},
        "issues": corrupted[:10],
        "issue_count": len(corrupted),
    }


# ---------------------------------------------------------------------------
# Main validation runner
# ---------------------------------------------------------------------------


def run_validation(
    manifest_path: str | Path | None = None,
    data_root: str | Path | None = None,
    output_dir: str | Path | None = None,
    check_files: bool = True,
    check_image_readability: bool = True,
    image_sample_size: int = 100,
    config_path: str | Path | None = None,
) -> dict[str, Any]:
    """
    Run all validation checks and return a structured report.

    Parameters
    ----------
    manifest_path:
        Path to manifest.csv. Defaults to data/manifest.csv.
    data_root:
        Root directory used to resolve relative file paths.
        If None, resolves from env/config.
    output_dir:
        Where to save the validation report JSON.
    check_files:
        Whether to check file existence on disk (can be slow for large datasets).
    check_image_readability:
        Whether to open a sample of images to check for corruption.
    image_sample_size:
        Number of images to sample for the readability check.
    config_path:
        Optional path to config.yaml override.

    Returns
    -------
    dict
        Full validation report.
    """
    try:
        import pandas as pd
    except ImportError as exc:
        raise RuntimeError("pandas is required: pip install pandas") from exc

    from src.common.config_loader import get_project_root, load_config

    cfg = load_config(config_path)
    project_root = get_project_root()

    # Resolve paths
    if manifest_path is None:
        manifest_path = project_root / cfg.get("data", {}).get(
            "manifest_path", "data/manifest.csv"
        )
    manifest_path = Path(manifest_path)

    if not manifest_path.exists():
        return {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "manifest_path": str(manifest_path),
            "fatal_error": f"Manifest not found: {manifest_path}. Run build_manifest.py first.",
            "checks": [],
        }

    if data_root is not None:
        root = Path(data_root)
    else:
        try:
            from src.common.config_loader import get_data_root
            root = get_data_root(cfg)
        except Exception:
            root = None  # type: ignore[assignment]

    if output_dir is None:
        out_cfg = cfg.get("outputs", {})
        output_dir = project_root / out_cfg.get("results_dir", "results") / "validation"
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    logger.info("Loading manifest: %s", manifest_path)
    df = pd.read_csv(manifest_path, dtype=str).fillna("")

    report: dict[str, Any] = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "manifest_path": str(manifest_path),
        "manifest_rows": len(df),
        "data_root": str(root) if root else "not resolved",
        "checks": [],
        "summary": {},
    }

    checks: list[dict[str, Any]] = []

    # Run checks
    checks.append(check_duplicate_paths(df))
    checks.append(check_labels(df))
    checks.append(check_class_distribution(df))
    checks.append(check_split_distribution(df))
    checks.append(check_source_leakage(df))
    checks.append(check_unsupported_formats(df))

    if check_files and root is not None:
        checks.append(check_file_existence(df, root))
    else:
        checks.append({
            "check": "file_existence",
            "passed": None,
            "stats": {},
            "issues": ["Skipped (check_files=False or data_root not available)."],
            "issue_count": 0,
        })

    if check_image_readability:
        checks.append(check_readable_images(df, root, sample_size=image_sample_size))

    report["checks"] = checks

    # Summary
    n_passed = sum(1 for c in checks if c.get("passed") is True)
    n_failed = sum(1 for c in checks if c.get("passed") is False)
    n_skipped = sum(1 for c in checks if c.get("passed") is None)
    total_issues = sum(c.get("issue_count", 0) for c in checks)

    report["summary"] = {
        "total_checks": len(checks),
        "passed": n_passed,
        "failed": n_failed,
        "skipped": n_skipped,
        "total_issues": total_issues,
        "validation_passed": n_failed == 0,
    }

    _print_validation_report(report)
    _save_validation_report(report, output_dir)

    return report


def _print_validation_report(report: dict[str, Any]) -> None:
    """Print a compact validation report to stdout."""
    print("\n" + "=" * 70)
    print("DATASET VALIDATION REPORT")
    print("=" * 70)
    print(f"Manifest   : {report['manifest_path']}")
    print(f"Rows       : {report['manifest_rows']}")
    print(f"Data root  : {report['data_root']}")
    print()

    for check in report["checks"]:
        status = (
            "PASS  " if check.get("passed") is True
            else "FAIL  " if check.get("passed") is False
            else "SKIP  "
        )
        n_issues = check.get("issue_count", 0)
        print(f"  [{status}] {check['check']:<30} issues={n_issues}")
        if check.get("issues"):
            for issue in check["issues"][:3]:
                print(f"           -> {issue}")

    summary = report.get("summary", {})
    print()
    print(f"  Passed  : {summary.get('passed', 0)}")
    print(f"  Failed  : {summary.get('failed', 0)}")
    print(f"  Skipped : {summary.get('skipped', 0)}")
    overall = "PASSED" if summary.get("validation_passed") else "FAILED"
    print(f"\n  Overall: {overall}")
    print("=" * 70 + "\n")


def _save_validation_report(report: dict[str, Any], output_dir: Path) -> None:
    """Save validation report JSON to output_dir."""
    ts = report["timestamp"].replace(":", "").replace("-", "")
    json_path = output_dir / f"validation_{ts}.json"
    latest_path = output_dir / "validation_latest.json"

    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)
    with open(latest_path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)

    logger.info("Validation report saved: %s", json_path)
    print(f"Report saved : {json_path}")
    print(f"Latest copy  : {latest_path}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate the dataset manifest."
    )
    parser.add_argument("--manifest", type=str, default=None)
    parser.add_argument("--data-root", type=str, default=None)
    parser.add_argument("--output-dir", type=str, default=None)
    parser.add_argument(
        "--no-file-check",
        action="store_true",
        help="Skip checking that every file path exists on disk.",
    )
    parser.add_argument(
        "--no-image-check",
        action="store_true",
        help="Skip opening images for readability.",
    )
    parser.add_argument("--image-sample", type=int, default=100)
    parser.add_argument("--config", type=str, default=None)
    return parser.parse_args()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    )

    args = _parse_args()
    run_validation(
        manifest_path=args.manifest,
        data_root=args.data_root,
        output_dir=args.output_dir,
        check_files=not args.no_file_check,
        check_image_readability=not args.no_image_check,
        image_sample_size=args.image_sample,
        config_path=args.config,
    )
