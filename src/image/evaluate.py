"""
src/image/evaluate.py
======================
Full evaluation pipeline for the Xception deepfake image classifier.

Pipeline:
  1. Load best checkpoint
  2. Run inference on validation set → calibrate threshold
  3. Run inference on test set → compute metrics with calibrated threshold
  4. Save all results (metrics, predictions, ROC, confusion matrix, plots)

Usage:
    python -m src.image.evaluate
    python -m src.image.evaluate --checkpoint models/checkpoints/best_model.pth

    # From Python / Colab:
    from src.image.evaluate import evaluate_model
    results = evaluate_model(config)
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import Any

import torch
from torch.utils.data import DataLoader
from tqdm import tqdm

logger = logging.getLogger(__name__)


@torch.no_grad()
def run_inference(
    model: torch.nn.Module,
    loader: DataLoader,
    device: torch.device,
) -> dict[str, list]:
    """
    Run inference on a DataLoader and collect predictions.

    Returns
    -------
    dict with keys:
        y_true: list[int]
        y_scores: list[float]
        file_paths: list[str]
        manipulation_types: list[str]
    """
    model.eval()
    y_true, y_scores = [], []
    file_paths, manipulation_types = [], []

    use_amp = device.type == "cuda"

    for batch in tqdm(loader, desc="  Inference", leave=False):
        images = batch["image"].to(device, non_blocking=True)
        labels = batch["label"]

        with torch.amp.autocast(device_type=device.type, enabled=use_amp):
            logits = model(images).squeeze(-1)
            scores = torch.sigmoid(logits)

        y_true.extend(labels.tolist())
        y_scores.extend(scores.cpu().tolist())
        file_paths.extend(batch["file_path"])
        manipulation_types.extend(batch["manipulation_type"])

    return {
        "y_true": y_true,
        "y_scores": y_scores,
        "file_paths": file_paths,
        "manipulation_types": manipulation_types,
    }


def evaluate_model(
    config: dict[str, Any] | None = None,
    config_path: str | Path | None = None,
    checkpoint_path: str | Path | None = None,
    max_val_samples: int | None = None,
    max_test_samples: int | None = None,
    experiment_name: str = "default",
) -> dict[str, Any]:
    """
    Full evaluation pipeline.

    Parameters
    ----------
    config:
        Loaded config dict.
    checkpoint_path:
        Path to model checkpoint. Defaults to best_model.pth.
    max_val_samples, max_test_samples:
        Limit dataset sizes for testing.
    experiment_name:
        Subdirectory name for results.

    Returns
    -------
    dict with test_metrics, threshold_info, and output_dir.
    """
    from src.common.config_loader import get_project_root, load_config
    from src.evaluation.metrics import (
        calibrate_threshold,
        compute_metrics,
        compute_roc_curve,
        plot_confusion_matrix,
        plot_roc_curve,
        save_evaluation_results,
    )
    from src.image.dataset import create_dataloaders
    from src.image.model import build_xception_model
    from src.image.train import get_device

    if config is None:
        config = load_config(config_path)

    project_root = get_project_root()
    ckpt_cfg = config.get("checkpointing", {})
    device = get_device()

    # Resolve checkpoint path
    if checkpoint_path is None:
        ckpt_dir = project_root / ckpt_cfg.get("checkpoint_dir", "models/checkpoints")
        checkpoint_path = ckpt_dir / "best_model.pth"
    checkpoint_path = Path(checkpoint_path)

    if not checkpoint_path.exists():
        raise FileNotFoundError(
            f"Checkpoint not found: {checkpoint_path}\n"
            "Train the model first with: python -m src.image.train"
        )

    # Build model and load weights
    model = build_xception_model(config, freeze_backbone=False)
    state = torch.load(checkpoint_path, map_location="cpu", weights_only=False)
    model.load_state_dict(state["model_state_dict"])
    model = model.to(device)
    model.eval()
    logger.info("Loaded checkpoint: %s (epoch %d)", checkpoint_path, state.get("epoch", -1))

    # Create dataloaders
    _, val_loader, test_loader = create_dataloaders(
        config,
        max_val_samples=max_val_samples,
        max_test_samples=max_test_samples,
    )

    # --- Step 1: Validation inference + threshold calibration ---
    logger.info("Running inference on validation set...")
    val_results = run_inference(model, val_loader, device)

    threshold_info = calibrate_threshold(
        val_results["y_true"],
        val_results["y_scores"],
        metric="youden",
    )
    best_threshold = threshold_info["best_threshold"]
    logger.info("Calibrated threshold: %.3f (Youden's J=%.4f)", best_threshold, threshold_info["best_score"])

    # --- Step 2: Test inference with calibrated threshold ---
    logger.info("Running inference on test set...")
    test_results = run_inference(model, test_loader, device)

    test_metrics = compute_metrics(
        test_results["y_true"],
        test_results["y_scores"],
        threshold=best_threshold,
    )

    # ROC data
    roc_data = compute_roc_curve(
        test_results["y_true"],
        test_results["y_scores"],
    )

    # --- Step 3: Save everything ---
    output_dir = project_root / config.get("outputs", {}).get("metrics_dir", "results/metrics")
    exp_dir = save_evaluation_results(
        output_dir=output_dir,
        metrics=test_metrics,
        y_true=test_results["y_true"],
        y_scores=test_results["y_scores"],
        file_paths=test_results["file_paths"],
        manipulation_types=test_results["manipulation_types"],
        threshold_info=threshold_info,
        roc_data=roc_data,
        experiment_name=experiment_name,
    )

    # Save plots
    plot_confusion_matrix(test_metrics, save_path=exp_dir / "confusion_matrix.png")
    plot_roc_curve(roc_data, test_metrics["roc_auc"], save_path=exp_dir / "roc_curve.png")

    # Print summary
    print("\n" + "=" * 70)
    print("EVALUATION RESULTS")
    print("=" * 70)
    print(f"Checkpoint   : {checkpoint_path}")
    print(f"Threshold    : {best_threshold:.3f} (calibrated on validation set)")
    print(f"Test samples : {test_metrics['n_samples']}")
    print(f"  Accuracy   : {test_metrics['accuracy']:.4f}")
    print(f"  Precision  : {test_metrics['precision']:.4f}")
    print(f"  Recall     : {test_metrics['recall']:.4f}")
    print(f"  F1         : {test_metrics['f1']:.4f}")
    print(f"  ROC-AUC    : {test_metrics['roc_auc']:.4f}")
    print(f"  TP={test_metrics['tp']}  FP={test_metrics['fp']}  "
          f"TN={test_metrics['tn']}  FN={test_metrics['fn']}")
    print(f"\nResults saved to: {exp_dir}")
    print("=" * 70 + "\n")

    return {
        "test_metrics": test_metrics,
        "threshold_info": threshold_info,
        "roc_data": roc_data,
        "output_dir": str(exp_dir),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Evaluate the trained Xception deepfake classifier."
    )
    parser.add_argument("--config", type=str, default=None)
    parser.add_argument("--checkpoint", type=str, default=None)
    parser.add_argument("--max-val", type=int, default=None)
    parser.add_argument("--max-test", type=int, default=None)
    parser.add_argument("--experiment-name", type=str, default="default")
    return parser.parse_args()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    )

    args = _parse_args()
    evaluate_model(
        config_path=args.config,
        checkpoint_path=args.checkpoint,
        max_val_samples=args.max_val,
        max_test_samples=args.max_test,
        experiment_name=args.experiment_name,
    )
