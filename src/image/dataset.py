"""
src/image/dataset.py
=====================
PyTorch Dataset and DataLoader factories for deepfake image classification.

Reads from the manifest/split CSV files produced by build_manifest.py and
build_splits.py. Does NOT independently scan directories.

Usage:
    from src.image.dataset import create_dataloaders

    train_loader, val_loader, test_loader = create_dataloaders(cfg)

    # Or individually:
    from src.image.dataset import DeepfakeImageDataset
    ds = DeepfakeImageDataset(split_csv="data/splits/train.csv", ...)
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pandas as pd
import torch
from torch.utils.data import DataLoader, Dataset

from src.image.preprocessing import (
    get_eval_transforms,
    get_train_transforms,
    preprocess_image,
)

logger = logging.getLogger(__name__)

# Label mapping: manifest uses strings, model needs integers
LABEL_MAP = {"real": 0, "fake": 1}


class DeepfakeImageDataset(Dataset):
    """
    PyTorch Dataset for deepfake detection images.

    Reads a split CSV (train.csv, val.csv, or test.csv) produced by
    build_splits.py. Each row must have at least:
      - file_path: relative path to image (relative to data_root)
      - label: 'real' or 'fake'

    Optional columns (preserved as metadata):
      - manipulation_type, source_id, dataset, compression, etc.

    Parameters
    ----------
    split_csv:
        Path to the split CSV file.
    data_root:
        Root directory for resolving relative file paths.
    transform:
        Torchvision transform pipeline to apply.
    use_face_crop:
        Whether to attempt face cropping.
    face_crop_padding:
        Padding fraction for face bounding box.
    max_samples:
        If set, limit the dataset to this many samples (for debugging).
    """

    def __init__(
        self,
        split_csv: str | Path,
        data_root: str | Path,
        transform=None,
        use_face_crop: bool = True,
        face_crop_padding: float = 0.3,
        max_samples: int | None = None,
    ):
        self.data_root = Path(data_root)
        self.transform = transform
        self.use_face_crop = use_face_crop
        self.face_crop_padding = face_crop_padding

        # Load the CSV
        df = pd.read_csv(split_csv, dtype=str).fillna("")
        if max_samples is not None and max_samples < len(df):
            df = df.head(max_samples)
            logger.info("Dataset limited to %d samples (max_samples).", max_samples)

        self.file_paths: list[str] = df["file_path"].tolist()
        self.labels: list[int] = [
            LABEL_MAP.get(lbl, 1) for lbl in df["label"].tolist()
        ]
        self.manipulation_types: list[str] = (
            df["manipulation_type"].tolist() if "manipulation_type" in df.columns else [""] * len(df)
        )

        # Store full dataframe for metadata access
        self._df = df

        n_real = self.labels.count(0)
        n_fake = self.labels.count(1)
        logger.info(
            "Loaded %s: %d samples (real=%d, fake=%d)",
            Path(split_csv).name, len(self), n_real, n_fake,
        )

    def __len__(self) -> int:
        return len(self.file_paths)

    def __getitem__(self, idx: int) -> dict[str, Any]:
        """
        Returns
        -------
        dict with keys:
            image: torch.Tensor (C, H, W) or None
            label: int (0=real, 1=fake)
            manipulation_type: str
            file_path: str
            valid: bool (False if image could not be loaded)
        """
        rel_path = self.file_paths[idx]
        full_path = self.data_root / rel_path
        label = self.labels[idx]
        manip_type = self.manipulation_types[idx]

        tensor, success = preprocess_image(
            path=full_path,
            transform=self.transform,
            use_face_crop=self.use_face_crop,
            face_crop_padding=self.face_crop_padding,
        )

        if not success or tensor is None:
            # Return a zero tensor placeholder — the collate_fn will handle it
            img_cfg_size = 299  # Default; will be overridden by actual transform
            tensor = torch.zeros(3, img_cfg_size, img_cfg_size)
            success = False

        return {
            "image": tensor,
            "label": label,
            "manipulation_type": manip_type,
            "file_path": rel_path,
            "valid": success,
        }


def collate_fn(batch: list[dict]) -> dict[str, Any]:
    """
    Custom collate function that filters out invalid samples.

    If all samples in a batch are invalid, returns the batch as-is
    (with zero tensors) to avoid crashing the training loop.
    """
    valid_batch = [item for item in batch if item["valid"]]
    if not valid_batch:
        # All invalid — return placeholder batch
        valid_batch = batch
        logger.warning("Entire batch had no valid images.")

    return {
        "image": torch.stack([item["image"] for item in valid_batch]),
        "label": torch.tensor(
            [item["label"] for item in valid_batch], dtype=torch.long
        ),
        "manipulation_type": [item["manipulation_type"] for item in valid_batch],
        "file_path": [item["file_path"] for item in valid_batch],
        "valid": [item["valid"] for item in valid_batch],
    }


# ---------------------------------------------------------------------------
# DataLoader factory
# ---------------------------------------------------------------------------


def create_dataloaders(
    config: dict[str, Any],
    data_root: str | Path | None = None,
    splits_dir: str | Path | None = None,
    max_train_samples: int | None = None,
    max_val_samples: int | None = None,
    max_test_samples: int | None = None,
) -> tuple[DataLoader, DataLoader, DataLoader]:
    """
    Create train/val/test DataLoaders from the split CSV files.

    Parameters
    ----------
    config:
        Loaded config dict (from load_config()).
    data_root:
        Dataset root directory. If None, resolved from config/env.
    splits_dir:
        Directory containing train.csv, val.csv, test.csv.
        If None, resolved from config.
    max_train_samples, max_val_samples, max_test_samples:
        Limit samples per split (useful for debugging/testing).

    Returns
    -------
    (train_loader, val_loader, test_loader)
    """
    from src.common.config_loader import get_data_root, get_project_root

    # Resolve paths
    if data_root is None:
        data_root = get_data_root(config)
    data_root = Path(data_root)

    project_root = get_project_root()
    if splits_dir is None:
        splits_dir = project_root / config.get("data", {}).get(
            "splits_dir", "data/splits"
        )
    splits_dir = Path(splits_dir)

    # Config values
    image_cfg = config.get("image", {})
    training_cfg = config.get("training", {})
    use_face_crop = image_cfg.get("use_face_crop", True)
    face_crop_padding = image_cfg.get("face_crop_padding", 0.3)
    batch_size = int(training_cfg.get("batch_size", 32))
    num_workers = int(training_cfg.get("num_workers", 4))
    seed = int(config.get("splits", {}).get("random_seed", 42))

    # Build transforms
    train_transform = get_train_transforms(config)
    eval_transform = get_eval_transforms(config)

    # Create datasets
    train_ds = DeepfakeImageDataset(
        split_csv=splits_dir / "train.csv",
        data_root=data_root,
        transform=train_transform,
        use_face_crop=use_face_crop,
        face_crop_padding=face_crop_padding,
        max_samples=max_train_samples,
    )

    val_ds = DeepfakeImageDataset(
        split_csv=splits_dir / "val.csv",
        data_root=data_root,
        transform=eval_transform,
        use_face_crop=use_face_crop,
        face_crop_padding=face_crop_padding,
        max_samples=max_val_samples,
    )

    test_ds = DeepfakeImageDataset(
        split_csv=splits_dir / "test.csv",
        data_root=data_root,
        transform=eval_transform,
        use_face_crop=use_face_crop,
        face_crop_padding=face_crop_padding,
        max_samples=max_test_samples,
    )

    # Create DataLoaders
    generator = torch.Generator()
    generator.manual_seed(seed)

    train_loader = DataLoader(
        train_ds,
        batch_size=batch_size,
        shuffle=True,  # Only shuffle training
        num_workers=num_workers,
        collate_fn=collate_fn,
        pin_memory=True,
        drop_last=False,
        generator=generator,
    )

    val_loader = DataLoader(
        val_ds,
        batch_size=batch_size,
        shuffle=False,
        num_workers=num_workers,
        collate_fn=collate_fn,
        pin_memory=True,
    )

    test_loader = DataLoader(
        test_ds,
        batch_size=batch_size,
        shuffle=False,
        num_workers=num_workers,
        collate_fn=collate_fn,
        pin_memory=True,
    )

    logger.info(
        "DataLoaders created: train=%d, val=%d, test=%d (batch_size=%d)",
        len(train_ds), len(val_ds), len(test_ds), batch_size,
    )

    return train_loader, val_loader, test_loader
