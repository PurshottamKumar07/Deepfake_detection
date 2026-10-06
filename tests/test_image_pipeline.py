"""
tests/test_image_pipeline.py
==============================
Tests for Steps 6-13: image preprocessing, dataset loading, model building,
training, evaluation, Grad-CAM, and video frame extraction.

These tests use synthetic data (generated in-memory) and do NOT require
the actual dataset or DATA_ROOT to be set.

Usage:
    uv run pytest tests/test_image_pipeline.py -v
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import numpy as np
import pytest
import torch
from PIL import Image


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sample_config():
    """Return a minimal config dict for testing."""
    return {
        "data": {
            "data_root": None,
            "dataset_name": "test_dataset",
            "real_classes": ["Original"],
            "fake_classes": ["Deepfakes"],
            "splits_dir": "data/splits",
            "manifest_path": "data/manifest.csv",
        },
        "image": {
            "input_size": [299, 299],
            "normalize_mean": [0.485, 0.456, 0.406],
            "normalize_std": [0.229, 0.224, 0.225],
            "use_face_crop": False,  # Disabled for tests (no MTCNN needed)
            "face_crop_padding": 0.3,
        },
        "video": {
            "frames_per_video": 5,
        },
        "training": {
            "batch_size": 4,
            "learning_rate": 0.001,
            "optimizer": "adam",
            "dropout_rate": 0.5,
            "freeze_backbone": True,
            "num_workers": 0,  # Main process only for tests
            "use_amp": False,
            "grad_clip": 1.0,
            "epochs": 2,
            "classifier_dim": 0,
        },
        "augmentation": {
            "enabled": False,
        },
        "splits": {
            "random_seed": 42,
        },
        "checkpointing": {
            "checkpoint_dir": "models/checkpoints",
            "monitor_metric": "val_auc",
            "monitor_mode": "max",
            "save_latest": True,
        },
        "outputs": {
            "metrics_dir": "results/metrics",
            "gradcam_dir": "results/gradcam",
        },
    }


@pytest.fixture
def sample_config_with_augmentation(sample_config):
    """Config with augmentation enabled."""
    cfg = sample_config.copy()
    cfg["augmentation"] = {
        "enabled": True,
        "horizontal_flip": True,
        "rotation_degrees": 10,
        "color_jitter": {
            "brightness": 0.2,
            "contrast": 0.2,
            "saturation": 0.2,
            "hue": 0.05,
        },
        "random_erasing_prob": 0.1,
    }
    return cfg


@pytest.fixture
def temp_dataset_dir():
    """
    Create a temporary dataset with fake images for testing.
    Structure:
        tmpdir/
        ├── Original/
        │   ├── 001_002_frame_0001.jpg
        │   ├── 001_002_frame_0002.jpg
        │   └── ...
        └── Deepfakes/
            ├── 003_004_frame_0001.jpg
            └── ...
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)

        for cls, n_images in [("Original", 20), ("Deepfakes", 20)]:
            cls_dir = root / cls
            cls_dir.mkdir()

            for i in range(n_images):
                # Create a random 100x100 RGB image
                arr = np.random.randint(0, 255, (100, 100, 3), dtype=np.uint8)
                img = Image.fromarray(arr, "RGB")
                prefix = f"001_{i:03d}" if cls == "Original" else f"003_{i:03d}"
                img.save(cls_dir / f"{prefix}_frame_{i:04d}.jpg")

        yield root


@pytest.fixture
def temp_split_csvs(temp_dataset_dir):
    """
    Create manifest and split CSV files from the temp dataset.
    Returns (data_root, splits_dir).
    """
    import pandas as pd

    root = temp_dataset_dir
    splits_dir = root / "splits"
    splits_dir.mkdir()

    rows = []
    for cls in ["Original", "Deepfakes"]:
        label = "real" if cls == "Original" else "fake"
        cls_dir = root / cls
        for f in sorted(cls_dir.iterdir()):
            rows.append({
                "file_path": f"{cls}/{f.name}",
                "label": label,
                "modality": "image",
                "dataset": "test",
                "source_id": f.name.split("_frame_")[0],
                "manipulation_type": "real" if label == "real" else cls,
                "split": "",
            })

    df = pd.DataFrame(rows)

    # Split: 14 train, 3 val, 3 test per class
    for label in ["real", "fake"]:
        mask = df["label"] == label
        idxs = df[mask].index.tolist()
        df.loc[idxs[:14], "split"] = "train"
        df.loc[idxs[14:17], "split"] = "val"
        df.loc[idxs[17:], "split"] = "test"

    # Save splits
    for split in ["train", "val", "test"]:
        split_df = df[df["split"] == split]
        split_df.to_csv(splits_dir / f"{split}.csv", index=False)

    # Save manifest
    df.to_csv(root / "manifest.csv", index=False)

    return root, splits_dir


# ---------------------------------------------------------------------------
# STEP 6 — Image Preprocessing Tests
# ---------------------------------------------------------------------------


class TestImagePreprocessing:
    """Test the image preprocessing pipeline."""

    def test_load_and_validate_valid_image(self, temp_dataset_dir):
        from src.image.preprocessing import load_and_validate_image

        img_path = list((temp_dataset_dir / "Original").iterdir())[0]
        img = load_and_validate_image(img_path)
        assert img is not None
        assert img.mode == "RGB"

    def test_load_and_validate_missing_image(self):
        from src.image.preprocessing import load_and_validate_image

        result = load_and_validate_image("/nonexistent/path/image.jpg")
        assert result is None

    def test_load_and_validate_grayscale(self):
        from src.image.preprocessing import load_and_validate_image

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "gray.png"
            arr = np.random.randint(0, 255, (50, 50), dtype=np.uint8)
            img = Image.fromarray(arr, "L")
            img.save(path)
            result = load_and_validate_image(path)
            assert result is not None
            assert result.mode == "RGB"

    def test_load_and_validate_rgba(self):
        from src.image.preprocessing import load_and_validate_image

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "rgba.png"
            arr = np.random.randint(0, 255, (50, 50, 4), dtype=np.uint8)
            img = Image.fromarray(arr, "RGBA")
            img.save(path)
            result = load_and_validate_image(path)
            assert result is not None
            assert result.mode == "RGB"

    def test_load_and_validate_too_small(self):
        from src.image.preprocessing import load_and_validate_image

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "tiny.png"
            arr = np.random.randint(0, 255, (5, 5, 3), dtype=np.uint8)
            img = Image.fromarray(arr, "RGB")
            img.save(path)
            result = load_and_validate_image(path, min_size=10)
            assert result is None

    def test_train_transforms(self, sample_config):
        from src.image.preprocessing import get_train_transforms

        transform = get_train_transforms(sample_config)
        assert transform is not None

        # Apply to a sample image
        img = Image.fromarray(
            np.random.randint(0, 255, (100, 100, 3), dtype=np.uint8)
        )
        tensor = transform(img)
        assert tensor.shape == (3, 299, 299)

    def test_train_transforms_with_augmentation(self, sample_config_with_augmentation):
        from src.image.preprocessing import get_train_transforms

        transform = get_train_transforms(sample_config_with_augmentation)
        img = Image.fromarray(
            np.random.randint(0, 255, (100, 100, 3), dtype=np.uint8)
        )
        tensor = transform(img)
        assert tensor.shape == (3, 299, 299)

    def test_eval_transforms(self, sample_config):
        from src.image.preprocessing import get_eval_transforms

        transform = get_eval_transforms(sample_config)
        img = Image.fromarray(
            np.random.randint(0, 255, (100, 100, 3), dtype=np.uint8)
        )
        tensor = transform(img)
        assert tensor.shape == (3, 299, 299)

    def test_preprocess_image(self, temp_dataset_dir, sample_config):
        from src.image.preprocessing import get_eval_transforms, preprocess_image

        transform = get_eval_transforms(sample_config)
        img_path = list((temp_dataset_dir / "Original").iterdir())[0]

        tensor, success = preprocess_image(
            img_path, transform, use_face_crop=False,
        )
        assert success is True
        assert tensor.shape == (3, 299, 299)


# ---------------------------------------------------------------------------
# STEP 7 — Dataset Loader Tests
# ---------------------------------------------------------------------------


class TestDatasetLoader:
    """Test the PyTorch dataset and dataloader."""

    def test_dataset_creation(self, temp_split_csvs, sample_config):
        from src.image.dataset import DeepfakeImageDataset
        from src.image.preprocessing import get_eval_transforms

        data_root, splits_dir = temp_split_csvs
        transform = get_eval_transforms(sample_config)

        ds = DeepfakeImageDataset(
            split_csv=splits_dir / "train.csv",
            data_root=data_root,
            transform=transform,
            use_face_crop=False,
        )

        assert len(ds) == 28  # 14 real + 14 fake
        item = ds[0]
        assert "image" in item
        assert "label" in item
        assert "manipulation_type" in item
        assert "valid" in item

    def test_dataset_labels_correct(self, temp_split_csvs, sample_config):
        from src.image.dataset import DeepfakeImageDataset
        from src.image.preprocessing import get_eval_transforms

        data_root, splits_dir = temp_split_csvs
        transform = get_eval_transforms(sample_config)

        ds = DeepfakeImageDataset(
            split_csv=splits_dir / "train.csv",
            data_root=data_root,
            transform=transform,
            use_face_crop=False,
        )

        # Check label distribution
        labels = [ds[i]["label"] for i in range(len(ds))]
        assert labels.count(0) == 14  # real
        assert labels.count(1) == 14  # fake

    def test_dataloader_batching(self, temp_split_csvs, sample_config):
        from src.image.dataset import DeepfakeImageDataset, collate_fn
        from src.image.preprocessing import get_eval_transforms
        from torch.utils.data import DataLoader

        data_root, splits_dir = temp_split_csvs
        transform = get_eval_transforms(sample_config)

        ds = DeepfakeImageDataset(
            split_csv=splits_dir / "train.csv",
            data_root=data_root,
            transform=transform,
            use_face_crop=False,
        )

        loader = DataLoader(ds, batch_size=4, collate_fn=collate_fn)
        batch = next(iter(loader))

        assert batch["image"].shape[0] == 4
        assert batch["image"].shape[1] == 3
        assert batch["label"].shape[0] == 4

    def test_max_samples_limit(self, temp_split_csvs, sample_config):
        from src.image.dataset import DeepfakeImageDataset
        from src.image.preprocessing import get_eval_transforms

        data_root, splits_dir = temp_split_csvs
        transform = get_eval_transforms(sample_config)

        ds = DeepfakeImageDataset(
            split_csv=splits_dir / "train.csv",
            data_root=data_root,
            transform=transform,
            use_face_crop=False,
            max_samples=5,
        )

        assert len(ds) == 5


# ---------------------------------------------------------------------------
# STEP 8 — Xception Model Tests
# ---------------------------------------------------------------------------


class TestXceptionModel:
    """Test the Xception binary classifier."""

    def test_model_builds(self, sample_config):
        from src.image.model import build_xception_model

        model = build_xception_model(sample_config, pretrained=False)
        assert model is not None

    def test_forward_pass(self, sample_config):
        from src.image.model import build_xception_model

        model = build_xception_model(sample_config, pretrained=False)
        x = torch.randn(2, 3, 299, 299)
        logits = model(x)
        assert logits.shape == (2, 1)

    def test_fake_score_output(self, sample_config):
        from src.image.model import build_xception_model

        model = build_xception_model(sample_config, pretrained=False)
        x = torch.randn(2, 3, 299, 299)
        scores = model.get_fake_score(x)
        assert scores.shape == (2,)
        assert (scores >= 0).all() and (scores <= 1).all()

    def test_frozen_backbone(self, sample_config):
        from src.image.model import build_xception_model

        model = build_xception_model(
            sample_config, pretrained=False, freeze_backbone=True
        )

        # Backbone params should not require grad
        for p in model.backbone.parameters():
            assert not p.requires_grad

        # Classifier params should require grad
        for p in model.classifier.parameters():
            assert p.requires_grad

    def test_unfreeze_backbone(self, sample_config):
        from src.image.model import build_xception_model, unfreeze_backbone

        model = build_xception_model(
            sample_config, pretrained=False, freeze_backbone=True
        )
        unfreeze_backbone(model)

        for p in model.backbone.parameters():
            assert p.requires_grad

    def test_loss_computation(self, sample_config):
        from src.image.model import build_xception_model

        model = build_xception_model(sample_config, pretrained=False)
        criterion = torch.nn.BCEWithLogitsLoss()

        x = torch.randn(4, 3, 299, 299)
        labels = torch.tensor([0.0, 1.0, 1.0, 0.0])

        logits = model(x).squeeze(-1)
        loss = criterion(logits, labels)

        assert loss.item() > 0
        loss.backward()  # Ensure backward works


# ---------------------------------------------------------------------------
# STEPS 10-11 — Evaluation & Threshold Calibration Tests
# ---------------------------------------------------------------------------


class TestEvaluationMetrics:
    """Test metrics computation and threshold calibration."""

    def test_compute_metrics(self):
        from src.evaluation.metrics import compute_metrics

        y_true = [0, 0, 0, 1, 1, 1, 1, 1]
        y_scores = [0.1, 0.3, 0.4, 0.6, 0.7, 0.8, 0.9, 0.95]

        metrics = compute_metrics(y_true, y_scores, threshold=0.5)

        assert "accuracy" in metrics
        assert "precision" in metrics
        assert "recall" in metrics
        assert "f1" in metrics
        assert "roc_auc" in metrics
        assert "confusion_matrix" in metrics
        assert metrics["accuracy"] > 0.5
        assert metrics["n_samples"] == 8

    def test_calibrate_threshold(self):
        from src.evaluation.metrics import calibrate_threshold

        y_true = [0, 0, 0, 0, 0, 1, 1, 1, 1, 1]
        y_scores = [0.1, 0.2, 0.3, 0.35, 0.4, 0.6, 0.7, 0.8, 0.85, 0.9]

        result = calibrate_threshold(y_true, y_scores, metric="f1")

        assert "best_threshold" in result
        assert "best_score" in result
        assert 0.0 <= result["best_threshold"] <= 1.0
        assert result["best_score"] > 0

    def test_calibrate_threshold_youden(self):
        from src.evaluation.metrics import calibrate_threshold

        y_true = [0, 0, 0, 1, 1, 1]
        y_scores = [0.1, 0.2, 0.3, 0.7, 0.8, 0.9]

        result = calibrate_threshold(y_true, y_scores, metric="youden")
        assert result["best_threshold"] > 0.3
        assert result["best_threshold"] < 0.8

    def test_compute_roc_curve(self):
        from src.evaluation.metrics import compute_roc_curve

        y_true = [0, 0, 1, 1]
        y_scores = [0.1, 0.4, 0.6, 0.9]

        roc = compute_roc_curve(y_true, y_scores)
        assert "fpr" in roc
        assert "tpr" in roc
        assert len(roc["fpr"]) > 2

    def test_save_evaluation_results(self):
        from src.evaluation.metrics import (
            compute_metrics,
            save_evaluation_results,
        )

        y_true = [0, 0, 1, 1]
        y_scores = [0.1, 0.4, 0.6, 0.9]
        metrics = compute_metrics(y_true, y_scores)

        with tempfile.TemporaryDirectory() as tmpdir:
            exp_dir = save_evaluation_results(
                output_dir=tmpdir,
                metrics=metrics,
                y_true=y_true,
                y_scores=y_scores,
                experiment_name="test_exp",
            )

            assert (exp_dir / "metrics.json").exists()
            assert (exp_dir / "predictions.csv").exists()

    def test_plot_confusion_matrix(self):
        from src.evaluation.metrics import compute_metrics, plot_confusion_matrix

        metrics = compute_metrics([0, 0, 1, 1], [0.1, 0.4, 0.6, 0.9])

        with tempfile.TemporaryDirectory() as tmpdir:
            save_path = Path(tmpdir) / "cm.png"
            plot_confusion_matrix(metrics, save_path=save_path)
            assert save_path.exists()


# ---------------------------------------------------------------------------
# STEP 13 — Video Frame Extraction Tests
# ---------------------------------------------------------------------------


class TestVideoFrameExtraction:
    """Test video frame extraction utilities."""

    def test_compute_uniform_indices(self):
        from src.video.extract_frames import _compute_uniform_indices

        # Normal case
        indices = _compute_uniform_indices(100, 10)
        assert len(indices) == 10
        assert indices[0] == 0
        assert indices[-1] == 99

    def test_compute_uniform_indices_more_frames_than_video(self):
        from src.video.extract_frames import _compute_uniform_indices

        indices = _compute_uniform_indices(5, 20)
        assert len(indices) == 5
        assert indices == [0, 1, 2, 3, 4]

    def test_compute_uniform_indices_zero_frames(self):
        from src.video.extract_frames import _compute_uniform_indices

        indices = _compute_uniform_indices(0, 10)
        assert indices == []

    def test_metadata_missing_file(self):
        from src.video.extract_frames import get_video_metadata

        meta = get_video_metadata("/nonexistent/video.mp4")
        assert meta["status"] == "file_not_found"

    def test_extract_frames_missing_file(self):
        from src.video.extract_frames import extract_frames

        with tempfile.TemporaryDirectory() as tmpdir:
            meta = extract_frames("/nonexistent/video.mp4", tmpdir)
            assert meta["status"] == "file_not_found"
            assert meta["n_extracted"] == 0


# ---------------------------------------------------------------------------
# Integration: Training smoke test (tiny subset)
# ---------------------------------------------------------------------------


class TestTrainingSmoke:
    """Smoke test: verify end-to-end training works on tiny data."""

    def test_training_loop_runs(self, temp_split_csvs, sample_config):
        """Verify 1 epoch of training completes without error."""
        from src.image.model import build_xception_model
        from src.image.dataset import DeepfakeImageDataset, collate_fn
        from src.image.preprocessing import get_train_transforms, get_eval_transforms

        data_root, splits_dir = temp_split_csvs
        device = torch.device("cpu")

        # Build model
        model = build_xception_model(sample_config, pretrained=False)
        model = model.to(device)

        # Build loader (tiny)
        transform = get_train_transforms(sample_config)
        from torch.utils.data import DataLoader
        train_ds = DeepfakeImageDataset(
            splits_dir / "train.csv", data_root, transform,
            use_face_crop=False, max_samples=8,
        )
        loader = DataLoader(train_ds, batch_size=4, collate_fn=collate_fn)

        # Train 1 step
        criterion = torch.nn.BCEWithLogitsLoss()
        optimizer = torch.optim.Adam(
            [p for p in model.parameters() if p.requires_grad], lr=0.001
        )

        model.train()
        batch = next(iter(loader))
        images = batch["image"].to(device)
        labels = batch["label"].float().to(device)

        logits = model(images).squeeze(-1)
        loss = criterion(logits, labels)
        loss.backward()
        optimizer.step()

        assert loss.item() > 0

    def test_validation_runs(self, temp_split_csvs, sample_config):
        """Verify validation inference works."""
        from src.image.model import build_xception_model
        from src.image.dataset import DeepfakeImageDataset, collate_fn
        from src.image.preprocessing import get_eval_transforms
        from torch.utils.data import DataLoader

        data_root, splits_dir = temp_split_csvs
        device = torch.device("cpu")

        model = build_xception_model(sample_config, pretrained=False)
        model = model.to(device)
        model.eval()

        transform = get_eval_transforms(sample_config)
        val_ds = DeepfakeImageDataset(
            splits_dir / "val.csv", data_root, transform,
            use_face_crop=False,
        )
        loader = DataLoader(val_ds, batch_size=4, collate_fn=collate_fn)

        all_scores = []
        all_labels = []

        with torch.no_grad():
            for batch in loader:
                images = batch["image"].to(device)
                logits = model(images).squeeze(-1)
                scores = torch.sigmoid(logits).cpu().tolist()
                all_scores.extend(scores)
                all_labels.extend(batch["label"].tolist())

        assert len(all_scores) > 0
        assert all(0 <= s <= 1 for s in all_scores)

    def test_checkpoint_save_and_load(self, sample_config):
        """Verify checkpoint save/load round-trips correctly."""
        from src.image.model import build_xception_model
        from src.image.train import save_checkpoint, load_checkpoint

        model = build_xception_model(sample_config, pretrained=False)
        optimizer = torch.optim.Adam(
            [p for p in model.parameters() if p.requires_grad], lr=0.001
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            ckpt_path = Path(tmpdir) / "test_ckpt.pth"

            save_checkpoint(
                ckpt_path, model, optimizer,
                scheduler=None, scaler=None,
                epoch=5, best_metric=0.85, history=[{"epoch": 5}],
            )

            assert ckpt_path.exists()

            # Load into a new model
            model2 = build_xception_model(sample_config, pretrained=False)
            epoch, best_metric, history = load_checkpoint(
                ckpt_path, model2,
            )

            assert epoch == 5
            assert best_metric == 0.85
            assert len(history) == 1
