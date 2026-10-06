"""
src/evaluation/metrics.py
==========================
Evaluation metrics and threshold calibration for deepfake detection.

Computes: Accuracy, Precision, Recall, F1, ROC-AUC, confusion matrix,
ROC curve. Saves predictions, scores, and misclassified samples.

Reusable across image, video, and audio modalities.

Usage:
    from src.evaluation.metrics import (
        compute_metrics,
        calibrate_threshold,
        save_evaluation_results,
    )
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)


def compute_metrics(
    y_true: list[int] | np.ndarray,
    y_scores: list[float] | np.ndarray,
    threshold: float = 0.5,
) -> dict[str, Any]:
    """
    Compute classification metrics at a given threshold.

    Parameters
    ----------
    y_true:
        Ground-truth binary labels (0=real, 1=fake).
    y_scores:
        Predicted fake probabilities in [0, 1].
    threshold:
        Decision threshold: score >= threshold → fake.

    Returns
    -------
    dict with keys: accuracy, precision, recall, f1, roc_auc,
        confusion_matrix, threshold, n_samples, n_real, n_fake.
    """
    from sklearn.metrics import (
        accuracy_score,
        confusion_matrix,
        f1_score,
        precision_score,
        recall_score,
        roc_auc_score,
    )

    y_true = np.asarray(y_true, dtype=int)
    y_scores = np.asarray(y_scores, dtype=float)
    y_pred = (y_scores >= threshold).astype(int)

    metrics: dict[str, Any] = {
        "threshold": float(threshold),
        "n_samples": int(len(y_true)),
        "n_real": int((y_true == 0).sum()),
        "n_fake": int((y_true == 1).sum()),
        "accuracy": float(accuracy_score(y_true, y_pred)),
        "precision": float(precision_score(y_true, y_pred, zero_division=0)),
        "recall": float(recall_score(y_true, y_pred, zero_division=0)),
        "f1": float(f1_score(y_true, y_pred, zero_division=0)),
    }

    # AUC requires both classes
    try:
        metrics["roc_auc"] = float(roc_auc_score(y_true, y_scores))
    except ValueError:
        metrics["roc_auc"] = 0.0
        logger.warning("Cannot compute AUC: only one class in labels.")

    # Confusion matrix: [[TN, FP], [FN, TP]]
    cm = confusion_matrix(y_true, y_pred, labels=[0, 1])
    metrics["confusion_matrix"] = cm.tolist()
    metrics["tn"] = int(cm[0, 0])
    metrics["fp"] = int(cm[0, 1])
    metrics["fn"] = int(cm[1, 0])
    metrics["tp"] = int(cm[1, 1])

    return metrics


# ---------------------------------------------------------------------------
# Threshold calibration (Step 11)
# ---------------------------------------------------------------------------


def calibrate_threshold(
    y_true: list[int] | np.ndarray,
    y_scores: list[float] | np.ndarray,
    metric: str = "f1",
    n_candidates: int = 201,
) -> dict[str, Any]:
    """
    Find the optimal decision threshold on validation predictions.

    Searches over candidate thresholds from 0 to 1 and selects the one
    that maximizes the specified metric.

    IMPORTANT: This must be called on the VALIDATION set only.
    The selected threshold is then frozen and applied to the TEST set.

    Parameters
    ----------
    y_true:
        Ground-truth binary labels (validation set).
    y_scores:
        Predicted fake probabilities (validation set).
    metric:
        Metric to optimize: 'f1', 'accuracy', 'recall', 'precision',
        or 'youden' (Youden's J = sensitivity + specificity - 1).
    n_candidates:
        Number of candidate thresholds to evaluate.

    Returns
    -------
    dict with keys: best_threshold, best_score, metric_name,
        all_thresholds, all_scores.
    """
    from sklearn.metrics import (
        accuracy_score,
        f1_score,
        precision_score,
        recall_score,
    )

    y_true = np.asarray(y_true, dtype=int)
    y_scores = np.asarray(y_scores, dtype=float)

    candidates = np.linspace(0.0, 1.0, n_candidates)
    scores_at_threshold = []

    for t in candidates:
        y_pred = (y_scores >= t).astype(int)

        if metric == "f1":
            score = f1_score(y_true, y_pred, zero_division=0)
        elif metric == "accuracy":
            score = accuracy_score(y_true, y_pred)
        elif metric == "recall":
            score = recall_score(y_true, y_pred, zero_division=0)
        elif metric == "precision":
            score = precision_score(y_true, y_pred, zero_division=0)
        elif metric == "youden":
            # Youden's J statistic
            tp = ((y_pred == 1) & (y_true == 1)).sum()
            tn = ((y_pred == 0) & (y_true == 0)).sum()
            fn = ((y_pred == 0) & (y_true == 1)).sum()
            fp = ((y_pred == 1) & (y_true == 0)).sum()
            sens = tp / max(tp + fn, 1)
            spec = tn / max(tn + fp, 1)
            score = sens + spec - 1
        else:
            raise ValueError(f"Unknown metric: {metric}")

        scores_at_threshold.append(float(score))

    best_idx = int(np.argmax(scores_at_threshold))
    best_threshold = float(candidates[best_idx])
    best_score = scores_at_threshold[best_idx]

    logger.info(
        "Threshold calibration (%s): best=%.3f  score=%.4f",
        metric, best_threshold, best_score,
    )

    return {
        "best_threshold": best_threshold,
        "best_score": best_score,
        "metric_name": metric,
        "all_thresholds": candidates.tolist(),
        "all_scores": scores_at_threshold,
    }


# ---------------------------------------------------------------------------
# ROC curve data
# ---------------------------------------------------------------------------


def compute_roc_curve(
    y_true: list[int] | np.ndarray,
    y_scores: list[float] | np.ndarray,
) -> dict[str, list[float]]:
    """
    Compute ROC curve data (FPR, TPR, thresholds).

    Returns
    -------
    dict with keys: fpr, tpr, thresholds (all lists of floats).
    """
    from sklearn.metrics import roc_curve

    y_true = np.asarray(y_true, dtype=int)
    y_scores = np.asarray(y_scores, dtype=float)

    fpr, tpr, thresholds = roc_curve(y_true, y_scores)

    return {
        "fpr": fpr.tolist(),
        "tpr": tpr.tolist(),
        "thresholds": thresholds.tolist(),
    }


# ---------------------------------------------------------------------------
# Save evaluation results
# ---------------------------------------------------------------------------


def save_evaluation_results(
    output_dir: str | Path,
    metrics: dict[str, Any],
    y_true: list[int] | np.ndarray,
    y_scores: list[float] | np.ndarray,
    file_paths: list[str] | None = None,
    manipulation_types: list[str] | None = None,
    threshold_info: dict[str, Any] | None = None,
    roc_data: dict | None = None,
    experiment_name: str = "default",
) -> Path:
    """
    Save all evaluation artifacts to disk.

    Saves:
      - metrics.json: computed metrics
      - predictions.csv: per-sample predictions
      - threshold.json: calibration results
      - roc_data.json: ROC curve data
      - misclassified.csv: incorrectly classified samples

    Parameters
    ----------
    output_dir:
        Directory to save results under.
    metrics:
        Output of compute_metrics().
    y_true, y_scores:
        Labels and predicted scores.
    file_paths:
        Optional list of file paths for each sample.
    manipulation_types:
        Optional list of manipulation types for each sample.
    threshold_info:
        Optional output of calibrate_threshold().
    roc_data:
        Optional output of compute_roc_curve().
    experiment_name:
        Subdirectory name for this experiment.

    Returns
    -------
    Path to the experiment results directory.
    """
    import pandas as pd

    y_true = np.asarray(y_true, dtype=int)
    y_scores = np.asarray(y_scores, dtype=float)

    exp_dir = Path(output_dir) / experiment_name
    exp_dir.mkdir(parents=True, exist_ok=True)

    # 1. Save metrics
    metrics_path = exp_dir / "metrics.json"
    with open(metrics_path, "w", encoding="utf-8") as f:
        json.dump(metrics, f, indent=2)
    logger.info("Metrics saved: %s", metrics_path)

    # 2. Save predictions
    threshold = metrics.get("threshold", 0.5)
    y_pred = (y_scores >= threshold).astype(int)

    pred_data: dict[str, Any] = {
        "y_true": y_true.tolist(),
        "y_score": y_scores.tolist(),
        "y_pred": y_pred.tolist(),
    }
    if file_paths:
        pred_data["file_path"] = file_paths
    if manipulation_types:
        pred_data["manipulation_type"] = manipulation_types

    pred_df = pd.DataFrame(pred_data)
    pred_path = exp_dir / "predictions.csv"
    pred_df.to_csv(pred_path, index=False)
    logger.info("Predictions saved: %s", pred_path)

    # 3. Save misclassified samples
    misclassified_mask = y_pred != y_true
    if misclassified_mask.any():
        mis_df = pred_df[misclassified_mask]
        mis_path = exp_dir / "misclassified.csv"
        mis_df.to_csv(mis_path, index=False)
        logger.info(
            "Misclassified samples saved: %s (%d samples)",
            mis_path, len(mis_df),
        )

    # 4. Save threshold calibration
    if threshold_info:
        thresh_path = exp_dir / "threshold.json"
        with open(thresh_path, "w", encoding="utf-8") as f:
            json.dump(threshold_info, f, indent=2)
        logger.info("Threshold calibration saved: %s", thresh_path)

    # 5. Save ROC data
    if roc_data:
        roc_path = exp_dir / "roc_data.json"
        with open(roc_path, "w", encoding="utf-8") as f:
            json.dump(roc_data, f, indent=2)
        logger.info("ROC data saved: %s", roc_path)

    return exp_dir


# ---------------------------------------------------------------------------
# Plot utilities
# ---------------------------------------------------------------------------


def plot_confusion_matrix(
    metrics: dict[str, Any],
    save_path: str | Path | None = None,
) -> None:
    """Plot and optionally save the confusion matrix."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import seaborn as sns

    cm = np.array(metrics["confusion_matrix"])
    labels = ["Real", "Fake"]

    fig, ax = plt.subplots(figsize=(6, 5))
    sns.heatmap(
        cm, annot=True, fmt="d", cmap="Blues",
        xticklabels=labels, yticklabels=labels, ax=ax,
    )
    ax.set_xlabel("Predicted")
    ax.set_ylabel("Actual")
    ax.set_title(
        f"Confusion Matrix (threshold={metrics.get('threshold', 0.5):.3f})"
    )
    plt.tight_layout()

    if save_path:
        fig.savefig(save_path, dpi=150, bbox_inches="tight")
        logger.info("Confusion matrix saved: %s", save_path)
        plt.close(fig)
    else:
        plt.show()


def plot_roc_curve(
    roc_data: dict[str, list[float]],
    auc_score: float,
    save_path: str | Path | None = None,
) -> None:
    """Plot and optionally save the ROC curve."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fpr = roc_data["fpr"]
    tpr = roc_data["tpr"]

    fig, ax = plt.subplots(figsize=(6, 5))
    ax.plot(fpr, tpr, "b-", lw=2, label=f"ROC (AUC = {auc_score:.4f})")
    ax.plot([0, 1], [0, 1], "r--", lw=1, label="Random")
    ax.set_xlabel("False Positive Rate")
    ax.set_ylabel("True Positive Rate")
    ax.set_title("ROC Curve")
    ax.legend(loc="lower right")
    ax.set_xlim([0, 1])
    ax.set_ylim([0, 1.05])
    ax.grid(alpha=0.3)
    plt.tight_layout()

    if save_path:
        fig.savefig(save_path, dpi=150, bbox_inches="tight")
        logger.info("ROC curve saved: %s", save_path)
        plt.close(fig)
    else:
        plt.show()
