# 🛡️ DeepDetect — AI-Powered Digital Media Forensics & Deepfake Detection

> **Final Year Project | B.Tech CSE | October 2026**  
> An end-to-end deepfake detection system using fine-tuned Xception + Grad-CAM, with a real-time web interface.

---

## 🏆 Results at a Glance

| Dataset | AUC | Accuracy | F1 |
|---|---|---|---|
| **FaceForensics++ (In-domain)** | **94.8%** | 88.1% | 92.8% |
| **Celeb-DF-v2 (Cross-dataset)** | **78.3%** | 71.5% | — |

> Celeb-DF result **outperforms the CVPR 2020 paper baseline of 65.3%** (official 518 test split: 72.2% AUC).

---

## 🚀 Run the Live Demo (Locally)

```powershell
# Step 1: Clone the repo (frontend branch)
git clone -b frontend https://github.com/PurshottamKumar07/Deepfake_detection.git
cd Deepfake_detection

# Step 2: Install dependencies
python -m pip install -r requirements.txt
# OR if using uv:
uv sync

# Step 3: Start the FastAPI server
python -m uvicorn api.app:app --host 127.0.0.1 --port 8080

# Step 4: Open in browser
# → http://127.0.0.1:8080
```

---

## 🧠 Model Architecture

- **Backbone:** Xception (ImageNet pre-trained)
- **Fine-tuning:** Full backbone unfrozen, 5 epochs on FaceForensics++
- **Loss:** `BCEWithLogitsLoss` with `pos_weight=0.1765` (handles 6.3:1 class imbalance)
- **Optimizer:** Adam, LR = `1e-5`
- **Checkpoint:** `models/checkpoints/best_model.pth` (238 MB)
- **Explainability:** Grad-CAM on last Xception block → heatmap overlay
- **Face Detection:** MTCNN (Multi-task CNN) for face cropping before inference

---

## 🌐 Features

- **Image Detection** — Upload any JPG/PNG → Xception inference → Grad-CAM heatmap
- **Video Detection** — Upload MP4/MOV → Frame sampling → Per-frame analysis → Timeline
- **Real-time Grad-CAM** — Highlights suspicious facial regions (eyes, mouth, boundaries)
- **Detection History** — LocalStorage-based history of all scans
- **Demo Presets** — Quick demo without uploading files

---

## 📁 Project Structure

```
Deepfake_detection/
├── api/
│   └── app.py              # FastAPI backend — /detect, /health endpoints
├── frontend/
│   ├── index.html           # DeepDetect UI (single-page app)
│   ├── css/style.css        # All styling
│   └── js/app.js            # API integration, Grad-CAM rendering, routing
├── scripts/
│   └── main.py              # Inference pipeline (image + video)
├── src/
│   ├── image/               # Xception model, preprocessing, transforms
│   ├── video/               # VideoPredictor, frame sampling, evaluate_celebdf.py
│   ├── forensic/            # Grad-CAM (GradCAM class, _find_target_layer)
│   ├── common/              # Config loader, project root utils
│   └── evaluation/          # Metrics, ROC curves, threshold finder
├── models/
│   └── checkpoints/
│       └── best_model.pth   # Fine-tuned Xception (Val AUC = 94.89%)
├── results/
│   ├── metrics/finetuned_best/   # FF++ evaluation results
│   └── metrics/celebdf_2k/       # Celeb-DF-v2 cross-dataset results
└── configs/
    └── config.yaml          # All hyperparameters and paths
```

---

## 📊 Evaluation Details

### FaceForensics++ (In-domain, 10,920 test images)

| Metric | Value |
|---|---|
| ROC-AUC | **94.8%** |
| Accuracy (τ=0.74) | 83.8% |
| Precision | 98.3% |
| F1-Score | 92.8% |
| Real Accuracy | 91.1% |
| Fake Recall | 82.7% |

### Celeb-DF-v2 (Cross-dataset generalization, 2,000 videos)

| Metric | Value |
|---|---|
| ROC-AUC (2K random) | **78.3%** |
| ROC-AUC (Official 518 test split) | **72.2%** |
| CVPR 2020 Baseline | 65.3% |
| **Our improvement** | **+6.9%** |

---

## 🔧 API Endpoints

| Endpoint | Method | Description |
|---|---|---|
| `/health` | GET | Returns `{"status": "ok"}` |
| `/detect` | POST | Upload file(s) → Returns JSON with `is_deepfake`, `confidence`, `gradcam_overlay` (base64), `key_observations` |
| `/` | GET | Serves the DeepDetect frontend UI |

---

## 🗝️ Key Design Decisions

- **No data leakage:** Train/val/test splits at source-video level (not per-frame)
- **Calibrated threshold:** Youden-optimal τ=0.74 for scientific evaluation; τ=0.90 for demo deployment
- **Domain mismatch acknowledged:** FF++ trains on compressed video frames; personal photos may differ (out-of-distribution)
- **Modular pipeline:** Image and video inference are independent, unified via `scripts/main.py`