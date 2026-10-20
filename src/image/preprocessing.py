"""
src/image/preprocessing.py
===========================
Reusable image preprocessing pipeline for deepfake detection.

Pipeline:
  Image → validation → RGB conversion → optional face crop →
  resize → normalization → tensor

Training augmentation is applied ONLY for the training split and is
kept separate from validation/test preprocessing.

All operations are in-memory; raw files are never modified.
All parameters come from configs/config.yaml.

Usage:
    from src.image.preprocessing import get_train_transforms, get_eval_transforms

    train_tf = get_train_transforms(cfg)
    eval_tf  = get_eval_transforms(cfg)
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageFile

logger = logging.getLogger(__name__)

# Allow loading of truncated images (common in web-scraped datasets)
ImageFile.LOAD_TRUNCATED_IMAGES = True


# ---------------------------------------------------------------------------
# Image validation & loading
# ---------------------------------------------------------------------------


def load_and_validate_image(
    path: str | Path,
    min_size: int = 10,
) -> Image.Image | None:
    """
    Load an image file, validate it, and convert to RGB.

    Parameters
    ----------
    path:
        Path to the image file.
    min_size:
        Minimum width/height in pixels to consider valid.

    Returns
    -------
    PIL.Image.Image or None
        The RGB image, or None if the image is invalid/corrupted.
    """
    path = Path(path)
    if not path.exists():
        logger.warning("Image not found: %s", path)
        return None

    try:
        img = Image.open(path)
        img.load()  # Force full decode to catch truncated files
    except Exception as exc:
        logger.warning("Failed to load image %s: %s", path, exc)
        return None

    # Validate dimensions
    w, h = img.size
    if w < min_size or h < min_size:
        logger.warning(
            "Image too small (%dx%d < %d): %s", w, h, min_size, path
        )
        return None

    # Convert to RGB (handles grayscale, RGBA, palette, CMYK, etc.)
    if img.mode != "RGB":
        try:
            img = img.convert("RGB")
        except Exception as exc:
            logger.warning("Failed RGB conversion for %s: %s", path, exc)
            return None

    return img


# ---------------------------------------------------------------------------
# Face detection / cropping (optional)
# ---------------------------------------------------------------------------

_face_detector = None  # Lazy-loaded singleton


def _get_face_detector():
    """Lazy-load MTCNN face detector (facenet-pytorch)."""
    global _face_detector
    if _face_detector is None:
        try:
            from facenet_pytorch import MTCNN
            _face_detector = MTCNN(
                keep_all=False,
                post_process=False,
                device="cpu",  # Detection on CPU is fast enough
            )
            logger.info("MTCNN face detector initialized.")
        except ImportError:
            logger.warning(
                "facenet-pytorch not installed. Face cropping disabled. "
                "Install with: pip install facenet-pytorch"
            )
            _face_detector = False  # Sentinel: tried and failed
    return _face_detector


def crop_face(
    img: Image.Image,
    padding: float = 0.3,
) -> Image.Image:
    """
    Detect and crop the largest face from an image.

    Parameters
    ----------
    img:
        RGB PIL image.
    padding:
        Fraction of face bounding box to add as margin.

    Returns
    -------
    PIL.Image.Image
        Cropped face region, or original image if no face detected.
    """
    detector = _get_face_detector()
    if detector is False or detector is None:
        return img

    try:
        boxes, probs = detector.detect(img)
    except Exception as exc:
        logger.debug("Face detection failed: %s", exc)
        return img

    if boxes is None or len(boxes) == 0:
        return img

    # Pick the face with highest confidence
    best_idx = int(np.argmax(probs))
    x1, y1, x2, y2 = boxes[best_idx]

    # Add padding
    w, h = x2 - x1, y2 - y1
    pad_w, pad_h = w * padding, h * padding
    img_w, img_h = img.size

    x1 = max(0, int(x1 - pad_w))
    y1 = max(0, int(y1 - pad_h))
    x2 = min(img_w, int(x2 + pad_w))
    y2 = min(img_h, int(y2 + pad_h))

    cropped = img.crop((x1, y1, x2, y2))

    # Validate crop size
    if cropped.size[0] < 20 or cropped.size[1] < 20:
        logger.debug("Face crop too small, using full image.")
        return img

    return cropped


# ---------------------------------------------------------------------------
# Transform builders (using torchvision.transforms)
# ---------------------------------------------------------------------------


def get_train_transforms(config: dict[str, Any]):
    """
    Build the training transform pipeline.

    Includes: optional face crop → resize → augmentation → normalize → tensor.
    Augmentation is controlled by configs/config.yaml [augmentation] section.

    Parameters
    ----------
    config:
        Loaded config dict.

    Returns
    -------
    torchvision.transforms.Compose
    """
    from torchvision import transforms

    image_cfg = config.get("image", {})
    aug_cfg = config.get("augmentation", {})

    input_size = tuple(image_cfg.get("input_size", [299, 299]))
    mean = image_cfg.get("normalize_mean", [0.485, 0.456, 0.406])
    std = image_cfg.get("normalize_std", [0.229, 0.224, 0.225])

    transform_list = [
        transforms.Resize(input_size),
    ]

    # Augmentation (training only)
    if aug_cfg.get("enabled", False):
        if aug_cfg.get("horizontal_flip", True):
            transform_list.append(transforms.RandomHorizontalFlip(p=0.5))

        rotation = aug_cfg.get("rotation_degrees", 10)
        if rotation > 0:
            transform_list.append(transforms.RandomRotation(rotation))

        cj = aug_cfg.get("color_jitter", {})
        if cj:
            transform_list.append(transforms.ColorJitter(
                brightness=cj.get("brightness", 0.2),
                contrast=cj.get("contrast", 0.2),
                saturation=cj.get("saturation", 0.2),
                hue=cj.get("hue", 0.05),
            ))

        erasing_prob = aug_cfg.get("random_erasing_prob", 0.1)
        if erasing_prob > 0:
            # RandomErasing works on tensors, so we add it after ToTensor
            pass  # Handled below

    transform_list.extend([
        transforms.ToTensor(),
        transforms.Normalize(mean=mean, std=std),
    ])

    # Add RandomErasing after ToTensor if enabled
    if aug_cfg.get("enabled", False):
        erasing_prob = aug_cfg.get("random_erasing_prob", 0.1)
        if erasing_prob > 0:
            transform_list.append(transforms.RandomErasing(p=erasing_prob))

    return transforms.Compose(transform_list)


def get_eval_transforms(config: dict[str, Any]):
    """
    Build the validation/test transform pipeline.

    Includes: resize → normalize → tensor.
    NO augmentation is applied.

    Parameters
    ----------
    config:
        Loaded config dict.

    Returns
    -------
    torchvision.transforms.Compose
    """
    from torchvision import transforms

    image_cfg = config.get("image", {})
    input_size = tuple(image_cfg.get("input_size", [299, 299]))
    mean = image_cfg.get("normalize_mean", [0.485, 0.456, 0.406])
    std = image_cfg.get("normalize_std", [0.229, 0.224, 0.225])
    
    return transforms.Compose([
        transforms.Resize(input_size),
        transforms.ToTensor(),
        transforms.Normalize(mean=mean, std=std),
    ])


# ---------------------------------------------------------------------------
# Preprocessing function (for single image, used by dataset loader)
# ---------------------------------------------------------------------------


def preprocess_image(
    path: str | Path,
    transform,
    use_face_crop: bool = True,
    face_crop_padding: float = 0.3,
) -> tuple[Any, bool]:
    """
    Load, validate, optionally face-crop, and transform a single image.

    Parameters
    ----------
    path:
        Path to the image file.
    transform:
        A torchvision transform pipeline (from get_train_transforms or
        get_eval_transforms).
    use_face_crop:
        Whether to attempt face detection and cropping.
    face_crop_padding:
        Padding fraction for face bounding box.

    Returns
    -------
    (tensor, success)
        The preprocessed image tensor and a boolean indicating success.
        On failure, returns (None, False).
    """
    img = load_and_validate_image(path)
    if img is None:
        return None, False

    if use_face_crop:
        img = crop_face(img, padding=face_crop_padding)

    try:
        tensor = transform(img)
    except Exception as exc:
        logger.warning("Transform failed for %s: %s", path, exc)
        return None, False

    return tensor, True