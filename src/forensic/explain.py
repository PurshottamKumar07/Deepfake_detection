"""
src/forensic/explain.py
========================
Grad-CAM attribution visualization for the Xception deepfake detector.

Generates:
  1. Original image
  2. Heatmap (Grad-CAM activation)
  3. Overlay (original + heatmap)

IMPORTANT: The visualization shows model-attribution evidence — which
regions influenced the model's decision. It does NOT prove that a
specific region is definitively manipulated.

Usage:
    from src.forensic.explain import generate_gradcam

    generate_gradcam(
        model=model,
        image_path="path/to/image.jpg",
        output_dir="results/gradcam/",
        config=cfg,
    )

    # Or batch:
    from src.forensic.explain import batch_gradcam
    batch_gradcam(model, image_paths, output_dir, config)
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import numpy as np
import torch
import torch.nn.functional as F

logger = logging.getLogger(__name__)


def _find_target_layer(model: torch.nn.Module) -> torch.nn.Module:
    """
    Find the last convolutional layer in the Xception backbone
    for Grad-CAM targeting.
    """
    # Walk the backbone to find the last Conv2d
    target = None
    for name, module in model.backbone.named_modules():
        if isinstance(module, torch.nn.Conv2d):
            target = module
            target_name = name

    if target is None:
        raise RuntimeError(
            "Could not find a Conv2d layer in the backbone for Grad-CAM."
        )

    logger.debug("Grad-CAM target layer: %s", target_name)
    return target


class GradCAM:
    """
    Grad-CAM implementation for any CNN model.

    Parameters
    ----------
    model:
        The full model (backbone + classifier).
    target_layer:
        The convolutional layer to compute gradients for.
    """

    def __init__(self, model: torch.nn.Module, target_layer: torch.nn.Module):
        self.model = model
        self.target_layer = target_layer
        self.gradients = None
        self.activations = None

        # Register hooks
        self._forward_hook = target_layer.register_forward_hook(self._save_activation)
        self._backward_hook = target_layer.register_full_backward_hook(self._save_gradient)

    def _save_activation(self, module, input, output):
        self.activations = output.detach()

    def _save_gradient(self, module, grad_input, grad_output):
        self.gradients = grad_output[0].detach()

    def __call__(self, input_tensor: torch.Tensor) -> tuple[np.ndarray, float]:
        """
        Compute Grad-CAM heatmap.

        Parameters
        ----------
        input_tensor:
            Preprocessed image tensor of shape (1, C, H, W).

        Returns
        -------
        (heatmap, score)
            heatmap: numpy array of shape (H, W) in [0, 1]
            score: model's fake probability
        """
        self.model.eval()
        input_tensor.requires_grad_(True)

        # Forward pass
        logits = self.model(input_tensor)
        score = torch.sigmoid(logits).item()

        # Backward pass (target = fake class activation)
        self.model.zero_grad()
        logits.backward()

        # Compute Grad-CAM
        gradients = self.gradients  # (1, C, h, w)
        activations = self.activations  # (1, C, h, w)

        # Global average pooling of gradients → weights
        weights = gradients.mean(dim=(2, 3), keepdim=True)  # (1, C, 1, 1)

        # Weighted combination
        cam = (weights * activations).sum(dim=1, keepdim=True)  # (1, 1, h, w)

        # ReLU (only positive contributions)
        cam = F.relu(cam)

        # Resize to input dimensions
        cam = F.interpolate(
            cam,
            size=input_tensor.shape[2:],
            mode="bilinear",
            align_corners=False,
        )

        # Normalize to [0, 1]
        cam = cam.squeeze().cpu().numpy()
        if cam.max() > 0:
            cam = (cam - cam.min()) / (cam.max() - cam.min())

        return cam, score

    def remove_hooks(self):
        """Remove registered hooks."""
        self._forward_hook.remove()
        self._backward_hook.remove()


def generate_gradcam(
    model: torch.nn.Module,
    image_path: str | Path,
    output_dir: str | Path,
    config: dict[str, Any],
    device: torch.device | None = None,
    target_layer: torch.nn.Module | None = None,
) -> dict[str, str]:
    """
    Generate Grad-CAM visualization for a single image.

    Saves:
      - original.png: the input image
      - heatmap.png: the Grad-CAM heatmap
      - overlay.png: original + heatmap overlay

    Parameters
    ----------
    model:
        Trained Xception model.
    image_path:
        Path to the input image.
    output_dir:
        Directory to save visualizations.
    config:
        Loaded config dict.
    device:
        Torch device. Auto-detected if None.
    target_layer:
        Optional specific layer for Grad-CAM. Auto-detected if None.

    Returns
    -------
    dict with keys: original, heatmap, overlay (file paths),
        fake_score (float), image_path (str).
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    from src.image.preprocessing import (
        get_eval_transforms,
        load_and_validate_image,
    )

    image_path = Path(image_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Auto-detect device
    if device is None:
        device = next(model.parameters()).device

    # Find target layer
    if target_layer is None:
        target_layer = _find_target_layer(model)

    # Load and preprocess image
    img = load_and_validate_image(image_path)
    if img is None:
        logger.warning("Cannot load image for Grad-CAM: %s", image_path)
        return {}

    transform = get_eval_transforms(config)
    input_tensor = transform(img).unsqueeze(0).to(device)

    # Compute Grad-CAM
    gradcam = GradCAM(model, target_layer)
    try:
        heatmap, fake_score = gradcam(input_tensor)
    finally:
        gradcam.remove_hooks()

    # Prepare filename stem
    stem = image_path.stem

    # Create colormap heatmap
    import matplotlib.cm as cm
    heatmap_colored = cm.jet(heatmap)[:, :, :3]  # Drop alpha channel

    # Resize original to match heatmap
    img_resized = img.resize(
        (heatmap.shape[1], heatmap.shape[0])
    )
    img_np = np.array(img_resized) / 255.0

    # Create overlay
    alpha = 0.4
    overlay = img_np * (1 - alpha) + heatmap_colored * alpha
    overlay = np.clip(overlay, 0, 1)

    # Determine label
    label = "FAKE" if fake_score >= 0.5 else "REAL"
    color = "red" if fake_score >= 0.5 else "green"

    # Save images
    fig, axes = plt.subplots(1, 3, figsize=(15, 5))

    axes[0].imshow(img_np)
    axes[0].set_title(f"Original\n{image_path.name}", fontsize=10)
    axes[0].axis("off")

    axes[1].imshow(heatmap, cmap="jet")
    axes[1].set_title("Grad-CAM Heatmap", fontsize=10)
    axes[1].axis("off")

    axes[2].imshow(overlay)
    axes[2].set_title(
        f"Overlay — {label} ({fake_score:.2%})",
        fontsize=10, color=color, fontweight="bold",
    )
    axes[2].axis("off")

    fig.suptitle(
        "Model Attribution Map (NOT proof of manipulation)",
        fontsize=11, style="italic", color="gray",
    )
    plt.tight_layout()

    composite_path = output_dir / f"{stem}_gradcam.png"
    fig.savefig(composite_path, dpi=150, bbox_inches="tight")
    plt.close(fig)

    logger.info(
        "Grad-CAM saved: %s (score=%.4f, label=%s)", composite_path, fake_score, label
    )

    return {
        "composite": str(composite_path),
        "fake_score": fake_score,
        "label": label,
        "image_path": str(image_path),
    }


def batch_gradcam(
    model: torch.nn.Module,
    image_paths: list[str | Path],
    output_dir: str | Path,
    config: dict[str, Any],
    device: torch.device | None = None,
    max_images: int = 20,
) -> list[dict[str, str]]:
    """
    Generate Grad-CAM visualizations for multiple images.

    Parameters
    ----------
    model:
        Trained model.
    image_paths:
        List of image paths.
    output_dir:
        Directory to save outputs.
    config:
        Loaded config dict.
    max_images:
        Maximum number of images to process.

    Returns
    -------
    list of result dicts from generate_gradcam().
    """
    if device is None:
        device = next(model.parameters()).device

    target_layer = _find_target_layer(model)

    results = []
    for i, path in enumerate(image_paths[:max_images]):
        logger.info("Grad-CAM %d/%d: %s", i + 1, min(len(image_paths), max_images), path)
        result = generate_gradcam(
            model, path, output_dir, config, device, target_layer
        )
        if result:
            results.append(result)

    logger.info("Batch Grad-CAM complete: %d/%d images processed.", len(results), len(image_paths[:max_images]))
    return results
