"""
src/image/train.py
===================
Reusable training script for the Xception deepfake image classifier.

Features:
  - Configurable epochs, learning rate, optimizer
  - Mixed precision training (AMP) — auto-disabled on CPU
  - Checkpointing (best model + latest checkpoint)
  - Early stopping on validation metric plateau
  - Learning rate reduction on plateau
  - TensorBoard logging
  - Training history saved as JSON
  - Resume-from-checkpoint support
  - Works on CPU (local), GPU (Colab), and MPS (Mac)

Usage:
    # CLI
    python -m src.image.train
    python -m src.image.train --epochs 10 --batch-size 16 --max-train 500

    # From Python / Colab
    from src.image.train import train_model
    history = train_model(config, max_train_samples=500)
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from pathlib import Path
from typing import Any

import torch
import torch.nn as nn
from torch.utils.data import DataLoader
from tqdm import tqdm

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Device detection
# ---------------------------------------------------------------------------

def get_device() -> torch.device:
    """Select the best available device: CUDA > MPS > CPU."""
    if torch.cuda.is_available():
        device = torch.device("cuda")
        logger.info("Using CUDA: %s", torch.cuda.get_device_name(0))
    elif hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
        device = torch.device("mps")
        logger.info("Using MPS (Apple Silicon)")
    else:
        device = torch.device("cpu")
        logger.info("Using CPU")
    return device


# ---------------------------------------------------------------------------
# Training loop
# ---------------------------------------------------------------------------

def train_one_epoch(
    model: nn.Module,
    loader: DataLoader,
    criterion: nn.Module,
    optimizer: torch.optim.Optimizer,
    device: torch.device,
    scaler: torch.amp.GradScaler | None,
    use_amp: bool,
    grad_clip: float | None,
) -> dict[str, float]:
    """Run one training epoch. Returns dict with train_loss and train_acc."""
    model.train()
    total_loss = 0.0
    correct = 0
    total = 0

    for batch in tqdm(loader, desc="  Train", leave=False):
        images = batch["image"].to(device, non_blocking=True)
        labels = batch["label"].to(device, non_blocking=True).float()

        optimizer.zero_grad()

        with torch.amp.autocast(device_type=device.type, enabled=use_amp):
            logits = model(images).squeeze(-1)
            loss = criterion(logits, labels)

        if scaler is not None:
            scaler.scale(loss).backward()
            if grad_clip:
                scaler.unscale_(optimizer)
                nn.utils.clip_grad_norm_(model.parameters(), grad_clip)
            scaler.step(optimizer)
            scaler.update()
        else:
            loss.backward()
            if grad_clip:
                nn.utils.clip_grad_norm_(model.parameters(), grad_clip)
            optimizer.step()

        total_loss += loss.item() * images.size(0)
        preds = (torch.sigmoid(logits) >= 0.5).long()
        correct += (preds == labels.long()).sum().item()
        total += images.size(0)

    return {
        "train_loss": total_loss / max(total, 1),
        "train_acc": correct / max(total, 1),
    }


@torch.no_grad()
def validate(
    model: nn.Module,
    loader: DataLoader,
    criterion: nn.Module,
    device: torch.device,
    use_amp: bool,
) -> dict[str, float]:
    """Run validation. Returns dict with val_loss, val_acc, val_auc."""
    from sklearn.metrics import roc_auc_score

    model.eval()
    total_loss = 0.0
    correct = 0
    total = 0
    all_scores: list[float] = []
    all_labels: list[int] = []

    for batch in tqdm(loader, desc="  Val  ", leave=False):
        images = batch["image"].to(device, non_blocking=True)
        labels = batch["label"].to(device, non_blocking=True).float()

        with torch.amp.autocast(device_type=device.type, enabled=use_amp):
            logits = model(images).squeeze(-1)
            loss = criterion(logits, labels)

        total_loss += loss.item() * images.size(0)
        scores = torch.sigmoid(logits)
        preds = (scores >= 0.5).long()
        correct += (preds == labels.long()).sum().item()
        total += images.size(0)

        all_scores.extend(scores.cpu().tolist())
        all_labels.extend(labels.long().cpu().tolist())

    val_loss = total_loss / max(total, 1)
    val_acc = correct / max(total, 1)

    # AUC (requires both classes present)
    try:
        val_auc = roc_auc_score(all_labels, all_scores)
    except ValueError:
        val_auc = 0.0
        logger.warning("Cannot compute AUC (only one class in labels).")

    return {
        "val_loss": val_loss,
        "val_acc": val_acc,
        "val_auc": val_auc,
    }


# ---------------------------------------------------------------------------
# Checkpoint management
# ---------------------------------------------------------------------------

def save_checkpoint(
    path: Path,
    model: nn.Module,
    optimizer: torch.optim.Optimizer,
    scheduler: Any,
    scaler: torch.amp.GradScaler | None,
    epoch: int,
    best_metric: float,
    history: list[dict],
) -> None:
    """Save a full training checkpoint."""
    path.parent.mkdir(parents=True, exist_ok=True)
    state = {
        "epoch": epoch,
        "model_state_dict": model.state_dict(),
        "optimizer_state_dict": optimizer.state_dict(),
        "scheduler_state_dict": scheduler.state_dict() if scheduler else None,
        "scaler_state_dict": scaler.state_dict() if scaler else None,
        "best_metric": best_metric,
        "history": history,
    }
    torch.save(state, path)
    logger.debug("Checkpoint saved: %s", path)


def load_checkpoint(
    path: Path,
    model: nn.Module,
    optimizer: torch.optim.Optimizer | None = None,
    scheduler: Any = None,
    scaler: torch.amp.GradScaler | None = None,
) -> tuple[int, float, list[dict]]:
    """
    Load a training checkpoint.

    Returns (start_epoch, best_metric, history).
    """
    state = torch.load(path, map_location="cpu", weights_only=False)
    model.load_state_dict(state["model_state_dict"])

    if optimizer and "optimizer_state_dict" in state:
        optimizer.load_state_dict(state["optimizer_state_dict"])
    if scheduler and state.get("scheduler_state_dict"):
        scheduler.load_state_dict(state["scheduler_state_dict"])
    if scaler and state.get("scaler_state_dict"):
        scaler.load_state_dict(state["scaler_state_dict"])

    epoch = state.get("epoch", 0)
    best_metric = state.get("best_metric", 0.0)
    history = state.get("history", [])

    logger.info("Resumed from checkpoint: epoch %d, best_metric=%.4f", epoch, best_metric)
    return epoch, best_metric, history


# ---------------------------------------------------------------------------
# Main training function
# ---------------------------------------------------------------------------

def train_model(
    config: dict[str, Any] | None = None,
    config_path: str | Path | None = None,
    max_train_samples: int | None = None,
    max_val_samples: int | None = None,
    resume: bool = True,
    epochs_override: int | None = None,
    batch_size_override: int | None = None,
    lr_override: float | None = None,
) -> list[dict]:
    """
    Train the Xception deepfake classifier.

    Parameters
    ----------
    config:
        Loaded config dict. If None, loaded from config_path.
    config_path:
        Path to config.yaml. Used only if config is None.
    max_train_samples, max_val_samples:
        Limit dataset sizes (for debugging/smoke tests).
    resume:
        Whether to resume from the latest checkpoint if available.
    epochs_override, batch_size_override, lr_override:
        Override config values (useful from CLI).

    Returns
    -------
    list[dict]
        Training history (one dict per epoch).
    """
    from src.common.config_loader import get_project_root, load_config
    from src.image.dataset import create_dataloaders
    from src.image.model import build_xception_model, unfreeze_backbone

    if config is None:
        config = load_config(config_path)

    training_cfg = config.get("training", {})
    ckpt_cfg = config.get("checkpointing", {})
    project_root = get_project_root()

    # Training hyperparameters
    epochs = epochs_override or int(training_cfg.get("epochs", 30))
    lr = lr_override or float(training_cfg.get("learning_rate", 1e-4))
    optimizer_name = training_cfg.get("optimizer", "adam")
    use_amp = bool(training_cfg.get("use_amp", True))
    grad_clip = training_cfg.get("grad_clip")
    if grad_clip is not None:
        grad_clip = float(grad_clip)
    unfreeze_epoch = training_cfg.get("unfreeze_epoch")
    if unfreeze_epoch is not None:
        unfreeze_epoch = int(unfreeze_epoch)

    # Override batch size if provided
    if batch_size_override is not None:
        config = {**config, "training": {**training_cfg, "batch_size": batch_size_override}}

    # Checkpoint paths
    ckpt_dir = project_root / ckpt_cfg.get("checkpoint_dir", "models/checkpoints")
    ckpt_dir.mkdir(parents=True, exist_ok=True)
    best_path = ckpt_dir / "best_model.pth"
    latest_path = ckpt_dir / "latest_checkpoint.pth"
    history_path = ckpt_dir / "training_history.json"

    # Monitoring
    monitor_metric = ckpt_cfg.get("monitor_metric", "val_auc")
    monitor_mode = ckpt_cfg.get("monitor_mode", "max")
    save_latest = ckpt_cfg.get("save_latest", True)

    # Device
    device = get_device()

    # Disable AMP on CPU (not supported)
    if device.type == "cpu":
        use_amp = False

    # Create data loaders
    train_loader, val_loader, _ = create_dataloaders(
        config,
        max_train_samples=max_train_samples,
        max_val_samples=max_val_samples,
    )

    # Build model
    model = build_xception_model(config)
    model = model.to(device)

    # Optimizer
    trainable_params = [p for p in model.parameters() if p.requires_grad]
    if optimizer_name == "adam":
        optimizer = torch.optim.Adam(trainable_params, lr=lr)
    elif optimizer_name == "adamw":
        optimizer = torch.optim.AdamW(trainable_params, lr=lr)
    elif optimizer_name == "sgd":
        optimizer = torch.optim.SGD(trainable_params, lr=lr, momentum=0.9)
    else:
        raise ValueError(f"Unknown optimizer: {optimizer_name}")

    # Loss
    criterion = nn.BCEWithLogitsLoss()

    # LR scheduler (reduce on plateau)
    scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(
        optimizer, mode=monitor_mode, factor=0.5, patience=3, verbose=False,
    )

    # AMP scaler
    scaler = torch.amp.GradScaler() if use_amp else None

    # Resume
    start_epoch = 0
    best_metric = 0.0 if monitor_mode == "max" else float("inf")
    history: list[dict] = []
    patience_counter = 0
    early_stop_patience = 7  # Stop after 7 epochs without improvement

    if resume and latest_path.exists():
        start_epoch, best_metric, history = load_checkpoint(
            latest_path, model, optimizer, scheduler, scaler
        )
        start_epoch += 1  # Resume from next epoch

    # TensorBoard
    try:
        from torch.utils.tensorboard import SummaryWriter
        tb_dir = project_root / "results" / "logs" / "tensorboard"
        tb_dir.mkdir(parents=True, exist_ok=True)
        writer = SummaryWriter(log_dir=str(tb_dir))
        logger.info("TensorBoard logging to: %s", tb_dir)
    except ImportError:
        writer = None
        logger.info("TensorBoard not available. Logging to console only.")

    # --- Training loop ---
    logger.info("=" * 60)
    logger.info("Starting training: epochs=%d, lr=%.6f, device=%s", epochs, lr, device)
    logger.info("=" * 60)

    for epoch in range(start_epoch, epochs):
        epoch_start = time.time()

        # Optional backbone unfreezing
        if unfreeze_epoch is not None and epoch == unfreeze_epoch:
            unfreeze_backbone(model)
            # Re-create optimizer with all parameters
            trainable_params = [p for p in model.parameters() if p.requires_grad]
            if optimizer_name == "adam":
                optimizer = torch.optim.Adam(trainable_params, lr=lr * 0.1)
            elif optimizer_name == "adamw":
                optimizer = torch.optim.AdamW(trainable_params, lr=lr * 0.1)
            elif optimizer_name == "sgd":
                optimizer = torch.optim.SGD(trainable_params, lr=lr * 0.1, momentum=0.9)
            logger.info("Backbone unfrozen at epoch %d with reduced LR.", epoch)

        # Train
        train_metrics = train_one_epoch(
            model, train_loader, criterion, optimizer, device, scaler, use_amp, grad_clip
        )

        # Validate
        val_metrics = validate(model, val_loader, criterion, device, use_amp)

        # Combine metrics
        current_lr = optimizer.param_groups[0]["lr"]
        epoch_metrics = {
            "epoch": epoch,
            **train_metrics,
            **val_metrics,
            "lr": current_lr,
            "time_sec": time.time() - epoch_start,
        }
        history.append(epoch_metrics)

        # LR scheduler step
        scheduler_metric = val_metrics.get(monitor_metric, val_metrics.get("val_loss", 0))
        scheduler.step(scheduler_metric)

        # Logging
        logger.info(
            "Epoch %d/%d — train_loss=%.4f  train_acc=%.4f  "
            "val_loss=%.4f  val_acc=%.4f  val_auc=%.4f  lr=%.2e  (%.1fs)",
            epoch + 1, epochs,
            train_metrics["train_loss"], train_metrics["train_acc"],
            val_metrics["val_loss"], val_metrics["val_acc"],
            val_metrics["val_auc"],
            current_lr,
            epoch_metrics["time_sec"],
        )

        # TensorBoard
        if writer:
            for key, val in epoch_metrics.items():
                if isinstance(val, (int, float)):
                    writer.add_scalar(f"training/{key}", val, epoch)

        # Checkpointing
        current_metric = val_metrics.get(monitor_metric, 0)
        is_best = (
            (monitor_mode == "max" and current_metric > best_metric) or
            (monitor_mode == "min" and current_metric < best_metric)
        )

        if is_best:
            best_metric = current_metric
            patience_counter = 0
            save_checkpoint(
                best_path, model, optimizer, scheduler, scaler,
                epoch, best_metric, history,
            )
            logger.info(
                "  ★ New best %s: %.4f → saved to %s",
                monitor_metric, best_metric, best_path,
            )
        else:
            patience_counter += 1

        if save_latest:
            save_checkpoint(
                latest_path, model, optimizer, scheduler, scaler,
                epoch, best_metric, history,
            )

        # Early stopping
        if patience_counter >= early_stop_patience:
            logger.info(
                "Early stopping: no improvement for %d epochs.", early_stop_patience
            )
            break

    # Save history
    with open(history_path, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=2)
    logger.info("Training history saved: %s", history_path)

    if writer:
        writer.close()

    logger.info("=" * 60)
    logger.info(
        "Training complete. Best %s: %.4f", monitor_metric, best_metric
    )
    logger.info("=" * 60)

    return history


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Train the Xception deepfake image classifier."
    )
    parser.add_argument("--config", type=str, default=None)
    parser.add_argument("--epochs", type=int, default=None)
    parser.add_argument("--batch-size", type=int, default=None)
    parser.add_argument("--lr", type=float, default=None)
    parser.add_argument("--max-train", type=int, default=None,
                        help="Limit training samples (for smoke test).")
    parser.add_argument("--max-val", type=int, default=None,
                        help="Limit validation samples (for smoke test).")
    parser.add_argument("--no-resume", action="store_true",
                        help="Do not resume from checkpoint.")
    return parser.parse_args()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    )

    args = _parse_args()
    train_model(
        config_path=args.config,
        max_train_samples=args.max_train,
        max_val_samples=args.max_val,
        resume=not args.no_resume,
        epochs_override=args.epochs,
        batch_size_override=args.batch_size,
        lr_override=args.lr,
    )
