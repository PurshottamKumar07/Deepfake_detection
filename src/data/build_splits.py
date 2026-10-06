"""
src/data/build_splits.py
========================
Leakage-safe train/validation/test splitting.

CRITICAL LEAKAGE RULE:
  All frames from the same source video MUST go to the same split.
  Never randomly split individual frames across splits.

Split strategy:
  1. PRIMARY (preferred): Split at source_id level.
     All rows sharing a source_id go to the same split.
  2. FALLBACK (limited): If source_id is unavailable for many rows,
     this script will NOT silently fall back to per-file random splitting.
     It will report the limitation and stop unless --allow-file-split
     is explicitly passed.

Splits are reproducible via a fixed random seed.

Output:
  data/splits/train.csv
  data/splits/val.csv
  data/splits/test.csv

Also writes a split column back into data/manifest.csv.

Usage (local):
    python -m src.data.build_splits

    # With explicit ratios:
    python -m src.data.build_splits --train 0.70 --val 0.15 --test 0.15

    # Allow file-level split as a last resort (REPORTS WARNING):
    python -m src.data.build_splits --allow-file-split

Usage (Colab):
    from src.data.build_splits import build_splits
    train_df, val_df, test_df = build_splits()
"""

from __future__ import annotations

import argparse
import logging
import random
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Minimum fraction of rows that must have source_id to use source-level splitting.
# If fewer rows have source_id, the script reports a limitation.
_MIN_SOURCE_ID_COVERAGE = 0.80


class SplitError(RuntimeError):
    """Raised when splitting cannot proceed safely."""


# ---------------------------------------------------------------------------
# Core split function
# ---------------------------------------------------------------------------


def build_splits(
    manifest_path: str | Path | None = None,
    train_ratio: float = 0.70,
    val_ratio: float = 0.15,
    test_ratio: float = 0.15,
    random_seed: int = 42,
    splits_dir: str | Path | None = None,
    allow_file_split: bool = False,
    config_path: str | Path | None = None,
) -> tuple[Any, Any, Any]:  # returns (train_df, val_df, test_df)
    """
    Assign train/val/test splits to the manifest and save split CSVs.

    Parameters
    ----------
    manifest_path:
        Path to manifest.csv. Defaults to data/manifest.csv.
    train_ratio, val_ratio, test_ratio:
        Desired split ratios (must sum to 1.0).
    random_seed:
        Fixed seed for reproducible shuffling.
    splits_dir:
        Directory to save train/val/test CSV files.
    allow_file_split:
        If True and source_id coverage is insufficient, falls back to
        file-level splitting WITH a loud warning. Default: False (safer).
    config_path:
        Optional path to config.yaml override.

    Returns
    -------
    (train_df, val_df, test_df)

    Raises
    ------
    SplitError
        If source_id coverage is insufficient and allow_file_split is False,
        or if ratios do not sum to 1.0.
    """
    try:
        import pandas as pd
    except ImportError as exc:
        raise SplitError("pandas is required: pip install pandas") from exc

    from src.common.config_loader import get_project_root, load_config

    cfg = load_config(config_path)
    splits_cfg = cfg.get("splits", {})

    # --- Resolve paths ---
    project_root = get_project_root()

    if manifest_path is None:
        manifest_path = project_root / cfg.get("data", {}).get(
            "manifest_path", "data/manifest.csv"
        )
    manifest_path = Path(manifest_path)

    if not manifest_path.exists():
        raise SplitError(
            f"Manifest not found: {manifest_path}\n"
            "Run build_manifest.py first."
        )

    if splits_dir is None:
        splits_dir = project_root / cfg.get("data", {}).get(
            "splits_dir", "data/splits"
        )
    splits_dir = Path(splits_dir)
    splits_dir.mkdir(parents=True, exist_ok=True)

    # Ratios from config if not passed
    if train_ratio == 0.70 and val_ratio == 0.15:
        train_ratio = float(splits_cfg.get("train_ratio", train_ratio))
        val_ratio = float(splits_cfg.get("val_ratio", val_ratio))
        test_ratio = float(splits_cfg.get("test_ratio", test_ratio))

    random_seed = int(splits_cfg.get("random_seed", random_seed))

    # Validate ratios
    ratio_sum = train_ratio + val_ratio + test_ratio
    if abs(ratio_sum - 1.0) > 1e-6:
        raise SplitError(
            f"Split ratios must sum to 1.0, got {ratio_sum:.4f} "
            f"(train={train_ratio}, val={val_ratio}, test={test_ratio})"
        )

    logger.info("Loading manifest: %s", manifest_path)
    df = pd.read_csv(manifest_path, dtype=str).fillna("")

    n_total = len(df)
    logger.info("Manifest rows: %d", n_total)

    # --- Decide split strategy ---
    n_with_source_id = (df["source_id"] != "").sum()
    source_id_coverage = n_with_source_id / max(n_total, 1)

    logger.info(
        "Source ID coverage: %d/%d (%.1f%%)",
        n_with_source_id, n_total, 100 * source_id_coverage,
    )

    use_source_split = source_id_coverage >= _MIN_SOURCE_ID_COVERAGE

    if use_source_split:
        logger.info("Using SOURCE-LEVEL splitting (leakage-safe).")
        train_df, val_df, test_df = _split_by_source(
            df, train_ratio, val_ratio, test_ratio, random_seed
        )
        split_method = "source_id"
    else:
        msg = (
            f"Source ID coverage is only {source_id_coverage:.1%} "
            f"(< required {_MIN_SOURCE_ID_COVERAGE:.0%}).\n"
            "Source-level splitting CANNOT be performed reliably.\n\n"
            "Options:\n"
            "  1. Re-run inspect_dataset.py to check if source IDs are\n"
            "     recoverable from directory names or filename patterns.\n"
            "  2. Provide a supplementary source ID mapping file.\n"
            "  3. If you understand the leakage risk and want to proceed anyway,\n"
            "     use --allow-file-split (WARNING: may introduce data leakage\n"
            "     if multiple frames come from the same source video)."
        )
        if not allow_file_split:
            raise SplitError(msg)

        logger.warning(msg)
        logger.warning(
            "PROCEEDING WITH FILE-LEVEL SPLIT — this may introduce leakage "
            "if multiple rows share the same source video."
        )
        train_df, val_df, test_df = _split_by_file(
            df, train_ratio, val_ratio, test_ratio, random_seed
        )
        split_method = "file_LEAKAGE_RISK"

    # --- Write split column back to manifest ---
    df.loc[train_df.index, "split"] = "train"
    df.loc[val_df.index, "split"] = "val"
    df.loc[test_df.index, "split"] = "test"

    # Save updated manifest
    df.to_csv(manifest_path, index=False)
    logger.info("Updated manifest saved: %s", manifest_path)

    # Save split CSVs
    train_path = splits_dir / "train.csv"
    val_path = splits_dir / "val.csv"
    test_path = splits_dir / "test.csv"

    train_df.to_csv(train_path, index=False)
    val_df.to_csv(val_path, index=False)
    test_df.to_csv(test_path, index=False)

    # --- Leakage validation ---
    _validate_no_leakage(train_df, val_df, test_df, split_method)

    # --- Print summary ---
    _print_split_summary(train_df, val_df, test_df, split_method, splits_dir)

    return train_df, val_df, test_df


# ---------------------------------------------------------------------------
# Split strategies
# ---------------------------------------------------------------------------


def _split_by_source(
    df: Any,
    train_ratio: float,
    val_ratio: float,
    test_ratio: float,
    seed: int,
) -> tuple[Any, Any, Any]:
    """
    Split at source_id level: all rows sharing a source_id go to the same split.

    Strategy:
    - Group source_ids by their label class (to maintain class balance).
    - Shuffle source_ids within each class.
    - Assign proportional fractions of source_ids to train/val/test.
    - Collect all rows belonging to those source_ids.
    """
    import pandas as pd

    rng = random.Random(seed)

    # Get unique source_ids per label class
    rows_with_source = df[df["source_id"] != ""].copy()
    rows_without_source = df[df["source_id"] == ""].copy()

    if len(rows_without_source) > 0:
        logger.warning(
            "%d rows have no source_id and will be excluded from leakage-safe splitting. "
            "They will be assigned to 'train' as a safe default.",
            len(rows_without_source),
        )

    # Build {label -> [source_ids]}
    label_to_sources: dict[str, list[str]] = {}
    for label, group in rows_with_source.groupby("label"):
        sources = sorted(group["source_id"].unique().tolist())
        rng.shuffle(sources)
        label_to_sources[str(label)] = sources

    train_sources: set[str] = set()
    val_sources: set[str] = set()
    test_sources: set[str] = set()

    for label, sources in label_to_sources.items():
        n = len(sources)
        n_train = max(1, round(n * train_ratio))
        n_val = max(1, round(n * val_ratio))
        # Test gets the remainder (ensures all sources are assigned)
        n_test = n - n_train - n_val
        if n_test < 0:
            # Very small dataset: adjust
            n_test = 0
            n_val = n - n_train

        train_sources.update(sources[:n_train])
        val_sources.update(sources[n_train: n_train + n_val])
        test_sources.update(sources[n_train + n_val:])

    train_mask = rows_with_source["source_id"].isin(train_sources)
    val_mask = rows_with_source["source_id"].isin(val_sources)
    test_mask = rows_with_source["source_id"].isin(test_sources)

    # Rows without source_id go to train as a safe default
    train_df = pd.concat([rows_with_source[train_mask], rows_without_source])
    val_df = rows_with_source[val_mask]
    test_df = rows_with_source[test_mask]

    return train_df, val_df, test_df


def _split_by_file(
    df: Any,
    train_ratio: float,
    val_ratio: float,
    test_ratio: float,
    seed: int,
) -> tuple[Any, Any, Any]:
    """
    Per-file random split (fallback only — may introduce leakage).
    Stratified by label to maintain class balance.
    """
    from sklearn.model_selection import train_test_split  # type: ignore[import]

    # First split: train vs (val+test)
    train_idx, rest_idx = train_test_split(
        df.index.tolist(),
        test_size=(val_ratio + test_ratio),
        random_state=seed,
        stratify=df["label"].tolist(),
    )

    # Second split: val vs test from rest
    rest_df = df.loc[rest_idx]
    val_ratio_adjusted = val_ratio / (val_ratio + test_ratio)
    val_idx, test_idx = train_test_split(
        rest_idx,
        test_size=(1 - val_ratio_adjusted),
        random_state=seed,
        stratify=rest_df["label"].tolist(),
    )

    return df.loc[train_idx], df.loc[val_idx], df.loc[test_idx]


# ---------------------------------------------------------------------------
# Leakage validation
# ---------------------------------------------------------------------------


def _validate_no_leakage(
    train_df: Any,
    val_df: Any,
    test_df: Any,
    split_method: str,
) -> None:
    """
    Check that no source_id appears in more than one split.
    Raises SplitError if leakage is detected.
    """
    def _sources(df: Any) -> set[str]:
        return set(df.loc[df["source_id"] != "", "source_id"].unique())

    train_srcs = _sources(train_df)
    val_srcs = _sources(val_df)
    test_srcs = _sources(test_df)

    train_val_overlap = train_srcs & val_srcs
    train_test_overlap = train_srcs & test_srcs
    val_test_overlap = val_srcs & test_srcs

    leakage_found = False
    for name, overlap in [
        ("train/val", train_val_overlap),
        ("train/test", train_test_overlap),
        ("val/test", val_test_overlap),
    ]:
        if overlap:
            leakage_found = True
            logger.error(
                "LEAKAGE DETECTED: %d source_id(s) appear in both %s splits: %s",
                len(overlap), name, sorted(overlap)[:10],
            )

    if leakage_found and "file_LEAKAGE_RISK" not in split_method:
        raise SplitError(
            "Source-level leakage detected in split outputs. "
            "This is a critical bug — please report it."
        )
    elif not leakage_found:
        logger.info("Leakage validation: PASSED (no source_id overlap between splits).")


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------


def _print_split_summary(
    train_df: Any,
    val_df: Any,
    test_df: Any,
    split_method: str,
    splits_dir: Path,
) -> None:
    """Print a human-readable split summary."""
    print("\n" + "=" * 70)
    print("SPLIT SUMMARY")
    print("=" * 70)
    print(f"Split method: {split_method}")
    print()

    for name, df in [("train", train_df), ("val", val_df), ("test", test_df)]:
        n = len(df)
        label_counts = df["label"].value_counts().to_dict()
        src_ids = df.loc[df["source_id"] != "", "source_id"].nunique()
        print(
            f"  {name:<8}: {n:>7} rows  |  "
            f"real={label_counts.get('real', 0)}  "
            f"fake={label_counts.get('fake', 0)}  |  "
            f"unique_source_ids={src_ids}"
        )

    print(f"\nSplit files saved to: {splits_dir}")

    if "LEAKAGE_RISK" in split_method:
        print()
        print("  *** WARNING: FILE-LEVEL SPLIT WAS USED ***")
        print("  This split may contain data leakage if frames from the same")
        print("  source video appear in different splits.")
        print("  Provide source IDs to enable leakage-safe splitting.")

    print("=" * 70 + "\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build leakage-safe train/val/test splits from the manifest."
    )
    parser.add_argument("--manifest-path", type=str, default=None)
    parser.add_argument("--splits-dir", type=str, default=None)
    parser.add_argument("--train", type=float, default=0.70, dest="train_ratio")
    parser.add_argument("--val", type=float, default=0.15, dest="val_ratio")
    parser.add_argument("--test", type=float, default=0.15, dest="test_ratio")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--config", type=str, default=None)
    parser.add_argument(
        "--allow-file-split",
        action="store_true",
        help=(
            "Allow file-level splitting if source ID coverage is insufficient. "
            "WARNING: This may introduce data leakage."
        ),
    )
    return parser.parse_args()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    )

    args = _parse_args()
    build_splits(
        manifest_path=args.manifest_path,
        train_ratio=args.train_ratio,
        val_ratio=args.val_ratio,
        test_ratio=args.test_ratio,
        random_seed=args.seed,
        splits_dir=args.splits_dir,
        allow_file_split=args.allow_file_split,
        config_path=args.config,
    )
