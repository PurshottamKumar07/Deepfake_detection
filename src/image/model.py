"""
src/image/model.py
===================
Xception-based binary classifier for deepfake detection.

Architecture:
    Input → pretrained Xception backbone → GlobalAveragePooling2D →
    Dropout → Dense → Sigmoid

Uses timm (PyTorch Image Models) for pretrained Xception weights.

Usage:
    from src.image.model import build_xception_model

    model = build_xception_model(cfg)
    # or with explicit parameters:
    model = build_xception_model(
        cfg, dropout=0.5, freeze_backbone=True, pretrained=True
    )
"""

from __future__ import annotations

import logging
from typing import Any

import torch
import torch.nn as nn

logger = logging.getLogger(__name__)


class XceptionBinaryClassifier(nn.Module):
    """
    Xception backbone + binary classification head.

    Parameters
    ----------
    backbone:
        The Xception feature extractor (without the original classifier).
    num_features:
        Number of features from the backbone's output.
    dropout_rate:
        Dropout probability before the final linear layer.
    classifier_dim:
        If > 0, adds an intermediate Dense layer of this size.
    """

    def __init__(
        self,
        backbone: nn.Module,
        num_features: int,
        dropout_rate: float = 0.5,
        classifier_dim: int = 0,
    ):
        super().__init__()
        self.backbone = backbone
        self.global_pool = nn.AdaptiveAvgPool2d(1)
        self.flatten = nn.Flatten()

        layers: list[nn.Module] = []
        in_features = num_features

        # Optional intermediate dense layer
        if classifier_dim > 0:
            layers.extend([
                nn.Linear(in_features, classifier_dim),
                nn.ReLU(inplace=True),
            ])
            in_features = classifier_dim

        layers.extend([
            nn.Dropout(p=dropout_rate),
            nn.Linear(in_features, 1),  # Binary output (single logit)
        ])

        self.classifier = nn.Sequential(*layers)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        """
        Forward pass.

        Parameters
        ----------
        x: torch.Tensor
            Input images of shape (B, 3, H, W).

        Returns
        -------
        torch.Tensor
            Raw logits of shape (B, 1). Apply sigmoid for probabilities.
        """
        features = self.backbone(x)

        # Handle different output shapes from timm models
        if features.dim() == 4:
            # (B, C, H, W) → pool → (B, C, 1, 1) → flatten
            features = self.global_pool(features)
            features = self.flatten(features)
        elif features.dim() == 2:
            # Already (B, C) — some timm models pool internally
            pass
        else:
            raise ValueError(
                f"Unexpected backbone output shape: {features.shape}"
            )

        return self.classifier(features)

    def get_fake_score(self, x: torch.Tensor) -> torch.Tensor:
        """
        Convenience method: returns sigmoid probability of being FAKE.

        Returns
        -------
        torch.Tensor
            Probabilities of shape (B,) in [0, 1].
        """
        logits = self.forward(x)
        return torch.sigmoid(logits).squeeze(-1)


def build_xception_model(
    config: dict[str, Any],
    dropout: float | None = None,
    freeze_backbone: bool | None = None,
    pretrained: bool = True,
    classifier_dim: int | None = None,
) -> XceptionBinaryClassifier:
    """
    Build the Xception binary classifier from config.

    Parameters
    ----------
    config:
        Loaded config dict.
    dropout:
        Dropout rate. Defaults to training.dropout_rate from config.
    freeze_backbone:
        Whether to freeze backbone weights. Defaults to
        training.freeze_backbone from config.
    pretrained:
        Whether to use ImageNet pretrained weights.
    classifier_dim:
        Optional intermediate classifier dimension. 0 = no intermediate layer.

    Returns
    -------
    XceptionBinaryClassifier
    """
    import timm

    training_cfg = config.get("training", {})

    if dropout is None:
        dropout = float(training_cfg.get("dropout_rate", 0.5))
    if freeze_backbone is None:
        freeze_backbone = bool(training_cfg.get("freeze_backbone", True))
    if classifier_dim is None:
        classifier_dim = int(training_cfg.get("classifier_dim", 0))

    # Load Xception backbone from timm (without classifier head)
    logger.info(
        "Loading Xception backbone (pretrained=%s, freeze=%s, dropout=%.2f)",
        pretrained, freeze_backbone, dropout,
    )

    backbone = timm.create_model(
        "xception",
        pretrained=pretrained,
        num_classes=0,        # Remove original classifier → feature extractor
        global_pool="",       # We handle pooling ourselves
    )

    # Get the number of output features
    num_features = backbone.num_features
    logger.info("Xception num_features: %d", num_features)

    # Freeze backbone if requested
    if freeze_backbone:
        for param in backbone.parameters():
            param.requires_grad = False
        logger.info("Backbone frozen (only classifier head is trainable).")

    model = XceptionBinaryClassifier(
        backbone=backbone,
        num_features=num_features,
        dropout_rate=dropout,
        classifier_dim=classifier_dim,
    )

    # Count parameters
    total_params = sum(p.numel() for p in model.parameters())
    trainable_params = sum(p.numel() for p in model.parameters() if p.requires_grad)
    logger.info(
        "Model built: %d total params, %d trainable params (%.1f%%)",
        total_params, trainable_params,
        100 * trainable_params / max(total_params, 1),
    )

    return model


def unfreeze_backbone(model: XceptionBinaryClassifier) -> None:
    """
    Unfreeze all backbone parameters for fine-tuning.

    Call this after initial head-only training to fine-tune the full model.
    """
    for param in model.backbone.parameters():
        param.requires_grad = True

    trainable = sum(p.numel() for p in model.parameters() if p.requires_grad)
    logger.info("Backbone unfrozen. Trainable parameters: %d", trainable)
