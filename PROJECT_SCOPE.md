# AI-Powered Digital Media Forensics and Deepfake Detection System
## Project Scope Document

---

## 1. Project Goal

Build a modular, reproducible deepfake detection and digital-media-forensics system capable of
detecting manipulated or synthetic media across three modalities: **image**, **video**, and **audio**.

The system must:
- Classify media as **REAL**, **FAKE**, or **UNCERTAIN** (borderline predictions)
- Provide forensic evidence, confidence scores, and metadata alongside each classification
- Support explainability outputs (Grad-CAM for images, frame timelines for video, spectrograms for audio)
- Be deployable in both VS Code (local CPU development) and Google Colab (GPU training)
- Remain fully modular so each modality can be developed and tested independently

---

## 2. System Architecture

```
INPUT MEDIA
|
+--------------------+
|                    |
IMAGE               VIDEO
|                   |
Image detector       Frame extraction
|                   |
|                Face detection
|                   |
|                Frame detector
|                   |
|                Temporal analysis
|                   |
+---------+---------+
|
AUDIO
|
Audio detector
|
v
Multimodal Fusion
(modality-aware: missing modality != zero score)
|
v
REAL / FAKE / UNCERTAIN
|
v
Forensic Evidence
|
v
Final Forensic Report
```

---

## 3. Modalities

| Modality | Initial Model              | Advanced Model                        |
|----------|---------------------------|---------------------------------------|
| Image    | Xception (ImageNet)        | Fine-tuned Xception                   |
| Video    | Frame-level Xception       | Xception -> LSTM/GRU temporal model   |
| Audio    | Mel-CNN / MFCC + SVM-RF   | Wav2Vec2                              |

---

## 4. Datasets

### Visual (FF++-derived)
- **Location**: Google Colab Library / cloud environment (NOT local)
- **Approximate size**: ~30,000 files, reportedly ~6 classes
- **Classes**: UNKNOWN until dataset is inspected — do not assume class names
- **Binary task**: Original -> REAL; all manipulation classes -> FAKE
- **Requirement**: Preserve original manipulation class in `manipulation_type` metadata
- **Split**: Must happen at source-video level, not per-frame level (leakage prevention)

### Audio
- **Primary candidate**: ASVspoof 2021 DeepFake
- **Status**: Not downloaded; dataset path is configurable
- **Do not download automatically**

---

## 5. Model Strategy

### Image

Primary: **Xception pretrained on ImageNet**
- GlobalAveragePooling -> Dropout -> Dense -> Sigmoid
- Binary output: REAL / FAKE
- Do NOT train from scratch as the default

Planned experiments:
1. Frozen Xception (feature extraction only)
2. Fine-tuned Xception (unfreeze top layers)
3. Xception + augmentation
4. Xception + augmentation + fine-tuning

### Video

Baseline (implement first):
- Uniform frame sampling (configurable, default ~20 frames)
- Face detection/cropping per frame
- Xception per-frame inference
- Frame-level fake score aggregation -> video-level prediction

Advanced (implement after baseline):
- Xception frame features -> LSTM/GRU -> video prediction

### Audio

Baseline:
- Audio -> Mel spectrogram -> CNN -> REAL/FAKE
- Audio -> MFCC/statistical features -> SVM or Random Forest -> REAL/FAKE

Advanced:
- Audio -> Wav2Vec2 -> classifier -> REAL/FAKE

Additional metric: **Equal Error Rate (EER)** for audio evaluation

---

## 6. Evaluation

Every modality will report:
- Accuracy, Precision, Recall, F1-score, ROC-AUC
- Confusion matrix + ROC curve

Audio additionally: **Equal Error Rate (EER)**

Decision threshold **must not** be assumed as 0.5.
Threshold is selected on the validation set and frozen before test evaluation.

---

## 7. Data Leakage Policy

**Critical rule**: Never split individual frames from the same source video across train/val/test.

Correct split strategy:
- source video A -> all frames -> train
- source video B -> all frames -> val
- source video C -> all frames -> test

Split level priority:
1. Source video ID
2. Person/speaker identity
3. Generator/manipulation source

All splits use a fixed, configurable random seed.

---

## 8. Central Dataset Manifest

`data/manifest.csv` is the single source of truth for all training and evaluation.

Fields:
| Field              | Description                                     |
|--------------------|-------------------------------------------------|
| `file_path`        | Absolute or relative path to the media file     |
| `label`            | Binary label: `real` or `fake`                  |
| `modality`         | `image`, `video`, or `audio`                    |
| `dataset`          | Dataset name (e.g., `faceforensics_plus_plus`)  |
| `source_id`        | Source video/clip identifier (if available)     |
| `person_id`        | Person identity (if available)                  |
| `speaker_id`       | Speaker identity for audio (if available)       |
| `manipulation_type`| e.g., `DeepFakes`, `FaceSwap`, `real`, etc.    |
| `compression`      | Compression level if available                  |
| `split`            | `train`, `val`, or `test`                       |
| `generator`        | Generator/method used to create fake (if known) |

---

## 9. Environment Configuration

### Key variable: DATA_ROOT

All dataset paths are relative to DATA_ROOT, which is set externally:

- **VS Code / local**: set via `configs/config.yaml` or environment variable
- **Google Colab**: set at the top of the notebook before importing anything

```python
# Colab usage
import os
os.environ["DATA_ROOT"] = "/content/drive/MyDrive/deepfake_dataset"
```

The Python modules must work without modification in either environment.

---

## 10. Reproducibility Requirements

- Fixed random seed (configurable, default: 42)
- All parameters in `configs/config.yaml` (not hardcoded)
- Versioned dependencies via `pyproject.toml` + `uv.lock`
- Best + latest checkpoints saved during training
- Training can be resumed if interrupted (checkpoint resume)
- All predictions, metrics, and experiment configs saved to `results/`

---

## 11. Current Limitations (as of Step 5)

1. **Dataset not locally accessible**: The FF++-derived dataset (~30,000 files, ~6 classes) is in
   Google Colab cloud storage. Classes are not confirmed until inspection is run from Colab.
2. **Dataset class names unknown**: The inspection script must be run against the actual dataset
   to confirm class structure.
3. **Source video IDs may not be in filenames**: If frame filenames do not embed source video IDs,
   source-aware splitting cannot be performed without additional metadata. The manifest generator
   will report this limitation.
4. **No trained models yet**: Steps 1-5 establish data pipeline foundations only.
5. **Audio dataset not acquired**: ASVspoof 2021 dataset has not been downloaded.
6. **`scripts/main.py` missing**: `api/app.py` imports `from scripts.main import process_upload`
   which does not yet exist. This is a pre-existing conflict to resolve in a later step.

---

## 12. Pre-existing Code (Conflicts to Resolve)

| File              | Status        | Notes                                                     |
|-------------------|---------------|-----------------------------------------------------------|
| `api/app.py`      | EXISTS        | References `scripts.main.process_upload` (not yet built)  |
| `main.py`         | EXISTS (stub) | Empty stub -- not yet connected to anything               |
| `scripts/`        | EXISTS (empty)| No Python modules yet                                     |

---

## 13. Next Steps (Steps 6+)

Steps 6-40 are listed in the main project spec and are NOT yet implemented. In order:
6.  Image preprocessing pipeline
7.  Xception image model
8.  Image augmentation
9.  Image training pipeline
10. Image fine-tuning
11. Image evaluation + Grad-CAM
12. Video frame extraction + face detection
13. Video frame-level inference + aggregation
14. Audio preprocessing (Mel + MFCC)
15. Audio models (CNN, SVM/RF, Wav2Vec2)
16. Audio EER evaluation
17. Common metrics + threshold calibration + uncertainty
18. Cross-manipulation evaluation
19. Compression robustness + cross-dataset tests
20. Failure analysis tools
21. Multimodal fusion
22. Forensic report generation
23. Checkpointing + experiment tracking
24. Colab training notebooks
25. Final inference API + model export
