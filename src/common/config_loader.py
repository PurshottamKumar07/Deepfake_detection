"""
src/common/config_loader.py
===========================
Loads and resolves the project configuration.

Priority order for DATA_ROOT:
  1. Environment variable DATA_ROOT  (highest priority)
  2. data.data_root in configs/config.yaml
  3. Raises ConfigurationError if neither is set

Usage:
    from src.common.config_loader import load_config, get_data_root

    cfg = load_config()                          # uses default config path
    cfg = load_config("path/to/custom.yaml")     # custom config
    data_root = get_data_root(cfg)               # resolves DATA_ROOT
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Locate the project root (two levels up from this file: src/common/ -> src/ -> project root)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_DEFAULT_CONFIG_PATH = _PROJECT_ROOT / "configs" / "config.yaml"


class ConfigurationError(RuntimeError):
    """Raised when the configuration is invalid or incomplete."""


def load_config(config_path: str | Path | None = None) -> dict[str, Any]:
    """
    Load the YAML configuration file.

    Parameters
    ----------
    config_path:
        Path to the YAML config file. Defaults to ``configs/config.yaml``
        relative to the project root.

    Returns
    -------
    dict
        Parsed configuration dictionary.

    Raises
    ------
    ConfigurationError
        If the config file cannot be found or parsed.
    """
    try:
        import yaml  # type: ignore[import]
    except ImportError as exc:
        raise ConfigurationError(
            "PyYAML is required to load config. Install it with: pip install pyyaml"
        ) from exc

    resolved_path = Path(config_path) if config_path else _DEFAULT_CONFIG_PATH

    if not resolved_path.exists():
        raise ConfigurationError(
            f"Configuration file not found: {resolved_path}\n"
            f"Expected location: {_DEFAULT_CONFIG_PATH}"
        )

    try:
        with open(resolved_path, "r", encoding="utf-8") as fh:
            config: dict[str, Any] = yaml.safe_load(fh) or {}
    except Exception as exc:
        raise ConfigurationError(
            f"Failed to parse config file '{resolved_path}': {exc}"
        ) from exc

    logger.debug("Loaded config from: %s", resolved_path)
    return config


def get_data_root(config: dict[str, Any] | None = None) -> Path:
    """
    Resolve the DATA_ROOT directory.

    Priority:
      1. ``DATA_ROOT`` environment variable
      2. ``data.data_root`` in the loaded config
      3. Raises ``ConfigurationError``

    Parameters
    ----------
    config:
        The loaded config dict (output of ``load_config()``).
        If None, ``load_config()`` is called automatically.

    Returns
    -------
    Path
        Resolved, existing data root directory.

    Raises
    ------
    ConfigurationError
        If DATA_ROOT cannot be determined from env or config.
    """
    if config is None:
        config = load_config()

    # 1. Environment variable wins
    env_root = os.environ.get("DATA_ROOT")
    if env_root:
        root = Path(env_root)
        logger.info("DATA_ROOT from environment: %s", root)
    else:
        # 2. Config file value
        cfg_root = (config.get("data") or {}).get("data_root")
        if not cfg_root:
            raise ConfigurationError(
                "DATA_ROOT is not set.\n"
                "Options:\n"
                "  (a) Set the DATA_ROOT environment variable:\n"
                "        export DATA_ROOT=/path/to/your/dataset      # Linux/macOS\n"
                "        $env:DATA_ROOT = 'C:\\path\\to\\dataset'     # PowerShell\n"
                "  (b) Set data.data_root in configs/config.yaml\n"
                "  (c) At the top of your Colab notebook:\n"
                "        import os; os.environ['DATA_ROOT'] = '/content/drive/...'"
            )
        root = Path(cfg_root)
        logger.info("DATA_ROOT from config: %s", root)

    if not root.exists():
        raise ConfigurationError(
            f"DATA_ROOT directory does not exist: {root}\n"
            "Please ensure the dataset is mounted/accessible at this path."
        )

    return root


def get_project_root() -> Path:
    """Return the absolute path to the project root directory."""
    return _PROJECT_ROOT

def get_output_dir(config: dict[str, Any], key: str) -> Path:
    """
    Resolve an output directory from config and create it if necessary.

    Parameters
    ----------
    config:
        Loaded config dict.
    key:
        Key within ``outputs`` section (e.g., ``"results_dir"``).

    Returns
    -------
    Path
        The resolved and created output directory.
    """
    outputs = config.get("outputs") or {}
    relative = outputs.get(key, f"results/{key}")
    resolved = _PROJECT_ROOT / relative
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved

def get_random_seed(config: dict[str, Any]) -> int:
    """Return the configured random seed (default: 42)."""
    return int((config.get("splits") or {}).get("random_seed", 42))
