"""
tests/test_validate_dataset.py
================================
Unit tests for the dataset validation utilities.

Tests run WITHOUT requiring GPU, a real dataset, or any external files.
They use in-memory pandas DataFrames as fixtures.

Run with:
    python -m pytest tests/test_validate_dataset.py -v
    # or
    python -m pytest tests/ -v
"""

from __future__ import annotations

import pytest

# ---------------------------------------------------------------------------
# We import from src.data.validate_dataset.
# All check functions accept DataFrames — no filesystem access needed for most tests.
# ---------------------------------------------------------------------------

try:
    import pandas as pd
    _HAS_PANDAS = True
except ImportError:
    _HAS_PANDAS = False

pytestmark = pytest.mark.skipif(
    not _HAS_PANDAS, reason="pandas not installed"
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_df(rows: list[dict]) -> "pd.DataFrame":
    import pandas as pd
    return pd.DataFrame(rows).fillna("")


@pytest.fixture
def clean_manifest():
    """A minimal valid manifest with 4 rows and proper source IDs."""
    return _make_df([
        {
            "file_path": "real/000_001_frame_0001.jpg",
            "label": "real",
            "modality": "image",
            "dataset": "faceforensics_plus_plus",
            "source_id": "000_001",
            "person_id": "",
            "speaker_id": "",
            "manipulation_type": "real",
            "compression": "",
            "split": "train",
            "generator": "",
        },
        {
            "file_path": "real/000_001_frame_0002.jpg",
            "label": "real",
            "modality": "image",
            "dataset": "faceforensics_plus_plus",
            "source_id": "000_001",
            "person_id": "",
            "speaker_id": "",
            "manipulation_type": "real",
            "compression": "",
            "split": "train",
            "generator": "",
        },
        {
            "file_path": "DeepFakes/000_002_frame_0001.jpg",
            "label": "fake",
            "modality": "image",
            "dataset": "faceforensics_plus_plus",
            "source_id": "000_002",
            "person_id": "",
            "speaker_id": "",
            "manipulation_type": "DeepFakes",
            "compression": "",
            "split": "val",
            "generator": "DeepFakes",
        },
        {
            "file_path": "DeepFakes/000_003_frame_0001.jpg",
            "label": "fake",
            "modality": "image",
            "dataset": "faceforensics_plus_plus",
            "source_id": "000_003",
            "person_id": "",
            "speaker_id": "",
            "manipulation_type": "DeepFakes",
            "compression": "",
            "split": "test",
            "generator": "DeepFakes",
        },
    ])


# ---------------------------------------------------------------------------
# Test: check_duplicate_paths
# ---------------------------------------------------------------------------


class TestCheckDuplicatePaths:
    def test_no_duplicates(self, clean_manifest):
        from src.data.validate_dataset import check_duplicate_paths

        result = check_duplicate_paths(clean_manifest)
        assert result["passed"] is True
        assert result["issue_count"] == 0

    def test_with_duplicates(self):
        from src.data.validate_dataset import check_duplicate_paths

        df = _make_df([
            {"file_path": "a/b.jpg", "label": "real", "split": "train"},
            {"file_path": "a/b.jpg", "label": "fake", "split": "val"},
            {"file_path": "c/d.jpg", "label": "real", "split": "train"},
        ])
        result = check_duplicate_paths(df)
        assert result["passed"] is False
        assert result["issue_count"] >= 1


# ---------------------------------------------------------------------------
# Test: check_labels
# ---------------------------------------------------------------------------


class TestCheckLabels:
    def test_valid_labels(self, clean_manifest):
        from src.data.validate_dataset import check_labels

        result = check_labels(clean_manifest)
        assert result["passed"] is True

    def test_invalid_label(self):
        from src.data.validate_dataset import check_labels

        df = _make_df([
            {"file_path": "a.jpg", "label": "MAYBE"},
            {"file_path": "b.jpg", "label": "real"},
        ])
        result = check_labels(df)
        assert result["passed"] is False
        assert result["issue_count"] >= 1

    def test_empty_label(self):
        from src.data.validate_dataset import check_labels

        df = _make_df([
            {"file_path": "a.jpg", "label": ""},
            {"file_path": "b.jpg", "label": "real"},
        ])
        result = check_labels(df)
        assert result["passed"] is False


# ---------------------------------------------------------------------------
# Test: check_class_distribution
# ---------------------------------------------------------------------------


class TestCheckClassDistribution:
    def test_balanced(self, clean_manifest):
        from src.data.validate_dataset import check_class_distribution

        result = check_class_distribution(clean_manifest)
        # 2 real, 2 fake — balanced
        assert result["passed"] is True

    def test_extreme_imbalance(self):
        from src.data.validate_dataset import check_class_distribution

        # 99 real, 1 fake
        rows = [{"file_path": f"real/{i}.jpg", "label": "real"} for i in range(99)]
        rows.append({"file_path": "fake/1.jpg", "label": "fake"})
        df = _make_df(rows)
        result = check_class_distribution(df)
        assert result["passed"] is False
        assert result["issue_count"] > 0


# ---------------------------------------------------------------------------
# Test: check_split_distribution
# ---------------------------------------------------------------------------


class TestCheckSplitDistribution:
    def test_all_splits_present(self, clean_manifest):
        from src.data.validate_dataset import check_split_distribution

        result = check_split_distribution(clean_manifest)
        assert result["passed"] is True

    def test_missing_test_split(self):
        from src.data.validate_dataset import check_split_distribution

        df = _make_df([
            {"file_path": "a.jpg", "label": "real", "split": "train"},
            {"file_path": "b.jpg", "label": "fake", "split": "val"},
        ])
        result = check_split_distribution(df)
        assert result["passed"] is False

    def test_empty_split_column(self):
        from src.data.validate_dataset import check_split_distribution

        df = _make_df([
            {"file_path": "a.jpg", "label": "real", "split": ""},
            {"file_path": "b.jpg", "label": "fake", "split": ""},
        ])
        result = check_split_distribution(df)
        assert result["passed"] is False
        assert any("no split" in issue.lower() for issue in result["issues"])


# ---------------------------------------------------------------------------
# Test: check_source_leakage
# ---------------------------------------------------------------------------


class TestCheckSourceLeakage:
    def test_no_leakage(self, clean_manifest):
        from src.data.validate_dataset import check_source_leakage

        result = check_source_leakage(clean_manifest)
        assert result["passed"] is True
        assert result["issue_count"] == 0

    def test_leakage_detected(self):
        from src.data.validate_dataset import check_source_leakage

        # source_id '000_001' appears in BOTH train and val
        df = _make_df([
            {"file_path": "a.jpg", "label": "real", "source_id": "000_001", "split": "train"},
            {"file_path": "b.jpg", "label": "real", "source_id": "000_001", "split": "val"},
            {"file_path": "c.jpg", "label": "fake", "source_id": "000_002", "split": "test"},
        ])
        result = check_source_leakage(df)
        assert result["passed"] is False
        assert result["stats"]["n_leaked_source_ids"] >= 1

    def test_no_source_ids(self):
        """When source_ids are all empty, report as inconclusive (not failed)."""
        from src.data.validate_dataset import check_source_leakage

        df = _make_df([
            {"file_path": "a.jpg", "label": "real", "source_id": "", "split": "train"},
            {"file_path": "b.jpg", "label": "fake", "source_id": "", "split": "val"},
        ])
        result = check_source_leakage(df)
        # Cannot confirm no leakage, but should not hard-fail
        assert result["stats"]["n_source_ids_checked"] == 0


# ---------------------------------------------------------------------------
# Test: check_unsupported_formats
# ---------------------------------------------------------------------------


class TestCheckUnsupportedFormats:
    def test_all_supported(self, clean_manifest):
        from src.data.validate_dataset import check_unsupported_formats

        result = check_unsupported_formats(clean_manifest)
        assert result["passed"] is True

    def test_unsupported_extension(self):
        from src.data.validate_dataset import check_unsupported_formats

        df = _make_df([
            {"file_path": "a.xyz", "label": "real"},
            {"file_path": "b.jpg", "label": "fake"},
        ])
        result = check_unsupported_formats(df)
        assert result["passed"] is False
        assert result["issue_count"] == 1


# ---------------------------------------------------------------------------
# Test: check_file_existence (with temp files)
# ---------------------------------------------------------------------------


class TestCheckFileExistence:
    def test_existing_files(self, tmp_path):
        from src.data.validate_dataset import check_file_existence

        f1 = tmp_path / "real" / "a.jpg"
        f1.parent.mkdir(parents=True)
        f1.write_bytes(b"\xff\xd8\xff")  # minimal JPEG header

        df = _make_df([{"file_path": "real/a.jpg"}])
        result = check_file_existence(df, data_root=tmp_path)
        assert result["passed"] is True
        assert result["stats"]["missing"] == 0

    def test_missing_files(self, tmp_path):
        from src.data.validate_dataset import check_file_existence

        df = _make_df([{"file_path": "nonexistent/x.jpg"}])
        result = check_file_existence(df, data_root=tmp_path)
        assert result["passed"] is False
        assert result["stats"]["missing"] == 1


# ---------------------------------------------------------------------------
# Test: config_loader (no filesystem dependency beyond the config file itself)
# ---------------------------------------------------------------------------


class TestConfigLoader:
    def test_load_default_config(self):
        """The default config.yaml must be loadable."""
        from src.common.config_loader import load_config

        cfg = load_config()
        assert isinstance(cfg, dict)
        assert "data" in cfg
        assert "splits" in cfg
        assert "training" in cfg

    def test_get_random_seed(self):
        from src.common.config_loader import get_random_seed, load_config

        cfg = load_config()
        seed = get_random_seed(cfg)
        assert isinstance(seed, int)
        assert seed == 42  # default

    def test_data_root_from_env(self, tmp_path, monkeypatch):
        """DATA_ROOT env var must take priority over config file."""
        from src.common.config_loader import get_data_root, load_config

        monkeypatch.setenv("DATA_ROOT", str(tmp_path))
        cfg = load_config()
        root = get_data_root(cfg)
        assert root == tmp_path

    def test_data_root_missing_raises(self, monkeypatch):
        """If DATA_ROOT is unset and config has no data_root, raise ConfigurationError."""
        from src.common.config_loader import ConfigurationError, get_data_root, load_config

        monkeypatch.delenv("DATA_ROOT", raising=False)
        cfg = load_config()
        cfg.setdefault("data", {})["data_root"] = None  # force null

        with pytest.raises(ConfigurationError):
            get_data_root(cfg)

    def test_data_root_nonexistent_raises(self, monkeypatch):
        """If DATA_ROOT points to a nonexistent directory, raise ConfigurationError."""
        from src.common.config_loader import ConfigurationError, get_data_root, load_config

        monkeypatch.setenv("DATA_ROOT", "/this/path/does/not/exist/anywhere/12345")
        cfg = load_config()

        with pytest.raises(ConfigurationError):
            get_data_root(cfg)


# ---------------------------------------------------------------------------
# Test: build_manifest (synthetic in-memory test — no real dataset needed)
# ---------------------------------------------------------------------------


class TestBuildManifest:
    def test_raises_when_no_classes_specified(self, tmp_path, monkeypatch):
        """build_manifest must raise if real/fake classes are not specified."""
        from src.data.build_manifest import ManifestBuildError, build_manifest

        monkeypatch.setenv("DATA_ROOT", str(tmp_path))
        with pytest.raises(ManifestBuildError, match="real_classes"):
            build_manifest(data_root=tmp_path)

    def test_raises_when_class_dir_missing(self, tmp_path, monkeypatch):
        """build_manifest must raise if a specified class dir doesn't exist."""
        from src.data.build_manifest import ManifestBuildError, build_manifest

        monkeypatch.setenv("DATA_ROOT", str(tmp_path))
        with pytest.raises(ManifestBuildError, match="not found"):
            build_manifest(
                data_root=tmp_path,
                real_classes=["original"],
                fake_classes=["DeepFakes"],
            )

    def test_basic_manifest_creation(self, tmp_path, monkeypatch):
        """Create a tiny dataset and verify the manifest is built correctly."""
        from src.data.build_manifest import build_manifest

        monkeypatch.setenv("DATA_ROOT", str(tmp_path))

        # Create minimal fake dataset structure
        real_dir = tmp_path / "original"
        fake_dir = tmp_path / "DeepFakes"
        real_dir.mkdir()
        fake_dir.mkdir()

        (real_dir / "000_001_frame_0001.jpg").write_bytes(b"\xff\xd8\xff")
        (real_dir / "000_001_frame_0002.jpg").write_bytes(b"\xff\xd8\xff")
        (fake_dir / "000_002_frame_0001.jpg").write_bytes(b"\xff\xd8\xff")

        out_path = tmp_path / "manifest.csv"
        df = build_manifest(
            data_root=tmp_path,
            real_classes=["original"],
            fake_classes=["DeepFakes"],
            output_path=out_path,
        )

        assert len(df) == 3
        assert set(df["label"].unique()) == {"real", "fake"}
        assert "manipulation_type" in df.columns
        assert (df[df["label"] == "real"]["manipulation_type"] == "real").all()
        assert (df[df["label"] == "fake"]["manipulation_type"] == "DeepFakes").all()
        assert out_path.exists()

    def test_source_id_inferred_from_ff_style_filename(self, tmp_path, monkeypatch):
        """FF++-style filenames should yield source_id like '000_001'."""
        from src.data.build_manifest import build_manifest

        monkeypatch.setenv("DATA_ROOT", str(tmp_path))

        real_dir = tmp_path / "original"
        real_dir.mkdir()
        fake_dir = tmp_path / "DeepFakes"
        fake_dir.mkdir()

        (real_dir / "000_001_frame_0001.jpg").write_bytes(b"\xff\xd8\xff")
        (fake_dir / "000_002_frame_0001.jpg").write_bytes(b"\xff\xd8\xff")

        df = build_manifest(
            data_root=tmp_path,
            real_classes=["original"],
            fake_classes=["DeepFakes"],
            output_path=tmp_path / "manifest.csv",
        )

        real_row = df[df["label"] == "real"].iloc[0]
        assert real_row["source_id"] == "000_001"

        fake_row = df[df["label"] == "fake"].iloc[0]
        assert fake_row["source_id"] == "000_002"


# ---------------------------------------------------------------------------
# Test: build_splits (synthetic in-memory test)
# ---------------------------------------------------------------------------


class TestBuildSplits:
    def _create_manifest(self, tmp_path, n_sources: int = 10) -> "pd.DataFrame":
        """Create a synthetic manifest with source IDs for split testing."""
        import pandas as pd
        rows = []
        for i in range(n_sources):
            src = f"{i:03d}_{i+1:03d}"
            label = "real" if i % 2 == 0 else "fake"
            for frame in range(5):
                rows.append({
                    "file_path": f"cls_{label}/{src}_frame_{frame:04d}.jpg",
                    "label": label,
                    "modality": "image",
                    "dataset": "test_dataset",
                    "source_id": src,
                    "person_id": "",
                    "speaker_id": "",
                    "manipulation_type": label,
                    "compression": "",
                    "split": "",
                    "generator": "",
                })
        df = pd.DataFrame(rows)
        manifest_path = tmp_path / "data" / "manifest.csv"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(manifest_path, index=False)
        return df

    def test_no_source_overlap_after_split(self, tmp_path, monkeypatch):
        """After splitting, no source_id should appear in more than one split."""
        from src.data.build_splits import build_splits
        from src.data.validate_dataset import check_source_leakage

        monkeypatch.setenv("DATA_ROOT", str(tmp_path))
        self._create_manifest(tmp_path, n_sources=20)

        splits_dir = tmp_path / "data" / "splits"
        train_df, val_df, test_df = build_splits(
            manifest_path=tmp_path / "data" / "manifest.csv",
            splits_dir=splits_dir,
            train_ratio=0.70,
            val_ratio=0.15,
            test_ratio=0.15,
            random_seed=42,
            allow_file_split=False,
        )

        # Verify no leakage
        for split_name, df in [("train", train_df), ("val", val_df), ("test", test_df)]:
            assert "split" in df.columns or len(df) > 0  # basic sanity

        # Check source leakage
        import pandas as pd
        combined = pd.concat([
            train_df.assign(split="train"),
            val_df.assign(split="val"),
            test_df.assign(split="test"),
        ])
        leakage_result = check_source_leakage(combined)
        assert leakage_result["passed"] is True, (
            f"Source leakage detected: {leakage_result['issues']}"
        )

    def test_reproducible_split(self, tmp_path, monkeypatch):
        """Running split twice with the same seed must produce identical results."""
        from src.data.build_splits import build_splits

        monkeypatch.setenv("DATA_ROOT", str(tmp_path))
        self._create_manifest(tmp_path, n_sources=20)

        manifest_path = tmp_path / "data" / "manifest.csv"
        splits_dir = tmp_path / "data" / "splits"

        train1, val1, test1 = build_splits(
            manifest_path=manifest_path,
            splits_dir=splits_dir,
            random_seed=42,
        )

        # Re-create manifest (split column gets overwritten)
        self._create_manifest(tmp_path, n_sources=20)

        train2, val2, test2 = build_splits(
            manifest_path=manifest_path,
            splits_dir=splits_dir,
            random_seed=42,
        )

        assert sorted(train1["file_path"].tolist()) == sorted(train2["file_path"].tolist())
        assert sorted(val1["file_path"].tolist()) == sorted(val2["file_path"].tolist())
        assert sorted(test1["file_path"].tolist()) == sorted(test2["file_path"].tolist())

    def test_ratio_sum_validation(self, tmp_path, monkeypatch):
        """Invalid ratios should raise SplitError."""
        from src.data.build_splits import SplitError, build_splits

        monkeypatch.setenv("DATA_ROOT", str(tmp_path))
        self._create_manifest(tmp_path)

        with pytest.raises(SplitError, match="sum to 1.0"):
            build_splits(
                manifest_path=tmp_path / "data" / "manifest.csv",
                splits_dir=tmp_path / "data" / "splits",
                train_ratio=0.50,
                val_ratio=0.30,
                test_ratio=0.30,  # sums to 1.10
            )
