"""
src/video/predict.py
====================
End-to-end video deepfake detection pipeline.

Workflow:
  1. Ingest video & validate metadata (duration, FPS, resolution).
  2. Uniformly sample N frames across the video.
  3. Detect & crop faces from each sampled frame.
  4. Run Xception deepfake classifier on each face frame.
  5. Aggregate frame-level scores into video-level verdict (Mean + Top-K).
  6. Generate Grad-CAM explainability heatmap for the most suspicious frame.
  7. Return / export structured forensic detection report.

Usage:
    # Python API
    from src.video.predict import VideoPredictor
    predictor = VideoPredictor()
    result = predictor.predict_video("path/to/video.mp4")

    # CLI
    python -m src.video.predict --input path/to/video.mp4
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from PIL import Image
import torch

from src.common.config_loader import get_project_root, load_config
from src.image.model import build_xception_model
from src.image.preprocessing import crop_face, get_eval_transforms
from src.video.extract_frames import extract_frames_to_memory, get_video_metadata

logger = logging.getLogger(__name__)


class VideoPredictor:
    """
    Video deepfake prediction pipeline combining frame extraction,
    face cropping, Xception inference, and forensic aggregation.
    """

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        checkpoint_path: str | Path | None = None,
        device: torch.device | str | None = None,
        threshold: float = 0.50,
    ):
        self.project_root = get_project_root()
        self.config = config or load_config()

        # Device selection
        if device is None:
            self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        elif isinstance(device, str):
            self.device = torch.device(device)
        else:
            self.device = device

        # Resolve checkpoint path
        if checkpoint_path is None:
            checkpoint_path = (
                self.project_root
                / "models"
                / "checkpoints"
                / "best_model.pth"
            )
        self.checkpoint_path = Path(checkpoint_path)

        # Load threshold if threshold.json exists, else fallback
        threshold_file = self.project_root / "results" / "metrics" / "default" / "threshold.json"
        if threshold == 0.50 and threshold_file.exists():
            try:
                with open(threshold_file, "r") as f:
                    t_data = json.load(f)
                    self.threshold = float(t_data.get("best_threshold", 0.50))
            except Exception:
                self.threshold = threshold
        else:
            self.threshold = threshold

        # Build & load model
        self.model = self._load_model()
        self.eval_transforms = get_eval_transforms(self.config)

        # Config settings
        self.n_frames = int(self.config.get("video", {}).get("frames_per_video", 16))
        self.use_face_crop = self.config.get("image", {}).get("use_face_crop", True)
        self.face_crop_padding = float(self.config.get("image", {}).get("face_crop_padding", 0.3))

        logger.info(
            "VideoPredictor initialized | Device: %s | Checkpoint: %s | Threshold: %.3f",
            self.device, self.checkpoint_path.name, self.threshold,
        )

    def _load_model(self) -> torch.nn.Module:
        """Instantiate model and load checkpoint weights."""
        if not self.checkpoint_path.exists():
            raise FileNotFoundError(f"Checkpoint not found at: {self.checkpoint_path}")

        model = build_xception_model(self.config)
        checkpoint = torch.load(self.checkpoint_path, map_location="cpu", weights_only=False)
        state_dict = checkpoint.get("model_state_dict", checkpoint)
        model.load_state_dict(state_dict)
        model = model.to(self.device)
        model.eval()
        return model

    def predict_video(
        self,
        video_path: str | Path,
        n_frames: int | None = None,
        generate_gradcam_heatmap: bool = True,
        output_dir: str | Path | None = None,
    ) -> dict[str, Any]:
        """
        Run deepfake detection on a single video file.

        Parameters
        ----------
        video_path:
            Path to video file (.mp4, .avi, etc.).
        n_frames:
            Number of frames to sample (default from config, typically 16-20).
        generate_gradcam_heatmap:
            Whether to compute Grad-CAM for the highest-scoring fake frame.
        output_dir:
            Directory to save Grad-CAM heatmaps and frame evidence.

        Returns
        -------
        dict with comprehensive video verdict, frame-level scores, and forensic metadata.
        """
        video_path = Path(video_path)
        if not video_path.exists():
            raise FileNotFoundError(f"Video file does not exist: {video_path}")

        n_frames = n_frames or self.n_frames
        start_time = time.time()

        # Step 1: Extract frames in memory
        frames_rgb, meta = extract_frames_to_memory(video_path, n_frames=n_frames)
        if not frames_rgb:
            return {
                "video_path": str(video_path),
                "verdict": "ERROR",
                "error": meta.get("status", "Could not extract frames from video"),
                "status": "failed",
            }

        # Step 2: Frame-by-frame analysis
        frame_results: list[dict[str, Any]] = []
        face_scores: list[float] = []
        sampled_indices = meta.get("sampled_indices", list(range(len(frames_rgb))))
        fps = meta.get("fps", 30.0)

        cropped_pil_faces: list[Image.Image | None] = []

        for i, frame_np in enumerate(frames_rgb):
            frame_idx = sampled_indices[i] if i < len(sampled_indices) else i
            timestamp_sec = round(frame_idx / fps, 2) if fps > 0 else 0.0

            pil_img = Image.fromarray(frame_np)

            # Face detection
            if self.use_face_crop:
                face_crop = crop_face(pil_img, padding=self.face_crop_padding)
                has_face = (face_crop.size != pil_img.size)
            else:
                face_crop = pil_img
                has_face = True

            cropped_pil_faces.append(face_crop)

            # Transform & Inference
            tensor_img = self.eval_transforms(face_crop).unsqueeze(0).to(self.device)

            with torch.no_grad():
                logits = self.model(tensor_img).squeeze()
                prob = torch.sigmoid(logits).item()

            face_scores.append(prob)
            frame_verdict = "FAKE" if prob >= self.threshold else "REAL"

            frame_results.append({
                "sample_order": i + 1,
                "frame_index": int(frame_idx),
                "timestamp_sec": timestamp_sec,
                "fake_probability": round(prob, 4),
                "verdict": frame_verdict,
                "face_detected": has_face,
            })

        # Step 3: Aggregate scores
        if face_scores:
            mean_prob = float(np.mean(face_scores))
            max_prob = float(np.max(face_scores))
            min_prob = float(np.min(face_scores))
            std_dev = float(np.std(face_scores))
            # Aggregate: 60% mean + 40% max (emphasizes local fake glitches)
            aggregated_score = 0.60 * mean_prob + 0.40 * max_prob
            fake_frames_count = sum(1 for s in face_scores if s >= self.threshold)
            fake_ratio = fake_frames_count / len(face_scores)
        else:
            mean_prob = max_prob = min_prob = std_dev = aggregated_score = fake_ratio = 0.0
            fake_frames_count = 0

        final_verdict = "FAKE" if aggregated_score >= self.threshold else "REAL"
        confidence = aggregated_score if final_verdict == "FAKE" else (1.0 - aggregated_score)

        # Step 4: Identify most suspicious frame
        most_suspicious_idx = int(np.argmax(face_scores)) if face_scores else 0
        most_suspicious_frame_info = frame_results[most_suspicious_idx] if frame_results else {}

        # Step 5: Optional Grad-CAM for most suspicious frame
        gradcam_paths = {}
        if generate_gradcam_heatmap and cropped_pil_faces:
            suspicious_face = cropped_pil_faces[most_suspicious_idx]
            if suspicious_face is not None:
                try:
                    from src.forensic.explain import GradCAM, _find_target_layer
                    import matplotlib
                    matplotlib.use("Agg")
                    import matplotlib.pyplot as plt

                    target_layer = _find_target_layer(self.model)
                    gradcam = GradCAM(self.model, target_layer)

                    t_input = self.eval_transforms(suspicious_face).unsqueeze(0).to(self.device)
                    heatmap, _ = gradcam(t_input)
                    gradcam.remove_hooks()

                    # Save heatmap overlay
                    if output_dir is None:
                        output_dir = (
                            self.project_root
                            / "results"
                            / "gradcam"
                            / f"video_{video_path.stem}"
                        )
                    output_dir = Path(output_dir)
                    output_dir.mkdir(parents=True, exist_ok=True)

                    overlay_path = output_dir / f"suspicious_frame_{most_suspicious_frame_info.get('frame_index', 0):06d}_gradcam.png"

                    # Render overlay
                    face_arr = np.array(suspicious_face)
                    h, w = face_arr.shape[:2]
                    resized_cam = cv2.resize(heatmap, (w, h))
                    color_cam = cv2.applyColorMap(np.uint8(255 * resized_cam), cv2.COLORMAP_JET)
                    color_cam = cv2.cvtColor(color_cam, cv2.COLOR_BGR2RGB)
                    overlay = np.uint8(0.6 * face_arr + 0.4 * color_cam)

                    Image.fromarray(overlay).save(overlay_path)
                    gradcam_paths = {
                        "overlay": str(overlay_path),
                        "frame_index": most_suspicious_frame_info.get("frame_index"),
                        "timestamp_sec": most_suspicious_frame_info.get("timestamp_sec"),
                    }
                except Exception as exc:
                    logger.warning("Could not generate Grad-CAM for video: %s", exc)

        elapsed = round(time.time() - start_time, 2)

        result: dict[str, Any] = {
            "video_path": str(video_path),
            "video_filename": video_path.name,
            "verdict": final_verdict,
            "confidence": round(confidence, 4),
            "aggregated_score": round(aggregated_score, 4),
            "threshold": round(self.threshold, 3),
            "summary": {
                "frames_analyzed": len(frames_rgb),
                "fake_frames_detected": fake_frames_count,
                "fake_frame_ratio": round(fake_ratio, 4),
                "mean_fake_probability": round(mean_prob, 4),
                "max_fake_probability": round(max_prob, 4),
                "min_fake_probability": round(min_prob, 4),
                "temporal_variance_std": round(std_dev, 4),
                "analysis_time_sec": elapsed,
            },
            "video_metadata": {
                "fps": round(fps, 2),
                "duration_sec": round(meta.get("duration_sec", 0.0), 2),
                "total_frames": meta.get("total_frames", 0),
                "resolution": f"{meta.get('width', 0)}x{meta.get('height', 0)}",
            },
            "most_suspicious_frame": most_suspicious_frame_info,
            "gradcam_evidence": gradcam_paths,
            "frame_details": frame_results,
        }

        return result


def print_video_report(result: dict[str, Any]) -> None:
    """Print a terminal-friendly summary report of video deepfake analysis."""
    print("\n" + "=" * 70)
    print("      DEEPFAKE VIDEO FORENSIC ANALYSIS REPORT")
    print("=" * 70)
    print(f"File         : {result.get('video_filename')}")
    print(f"Duration     : {result.get('video_metadata', {}).get('duration_sec')}s "
          f"({result.get('video_metadata', {}).get('total_frames')} total frames @ {result.get('video_metadata', {}).get('fps')} fps)")
    print(f"Resolution   : {result.get('video_metadata', {}).get('resolution')}")
    print("-" * 70)

    verdict = result.get("verdict", "UNKNOWN")
    conf = result.get("confidence", 0.0) * 100
    agg = result.get("aggregated_score", 0.0)
    th = result.get("threshold", 0.50)

    verdict_badge = f"[ FAKE / MANIPULATED ]" if verdict == "FAKE" else f"[ REAL / AUTHENTIC ]"
    print(f"FINAL VERDICT: {verdict_badge}")
    print(f"Confidence   : {conf:.1f}% (Aggregated Fake Probability: {agg:.4f} vs Threshold: {th:.3f})")
    print("-" * 70)

    summary = result.get("summary", {})
    print("Frame Statistics:")
    print(f"  Analyzed   : {summary.get('frames_analyzed')} sampled frames")
    print(f"  Fake count : {summary.get('fake_frames_detected')} / {summary.get('frames_analyzed')} frames "
          f"({summary.get('fake_frame_ratio', 0)*100:.1f}%)")
    print(f"  Mean Score : {summary.get('mean_fake_probability'):.4f} (Peak: {summary.get('max_fake_probability'):.4f})")
    print(f"  Temporal SD: {summary.get('temporal_variance_std'):.4f} (Inconsistency measure)")
    print(f"  Speed      : {summary.get('analysis_time_sec'):.2f}s total processing time")

    grad = result.get("gradcam_evidence", {})
    if grad and "overlay" in grad:
        print(f"\nVisual Evidence (Grad-CAM):")
        print(f"  Overlay saved: {grad['overlay']}")

    print("=" * 70 + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Predict deepfake in a video.")
    parser.add_argument("--input", "-i", type=str, required=True, help="Path to input video file")
    parser.add_argument("--frames", "-n", type=int, default=16, help="Number of frames to sample")
    parser.add_argument("--checkpoint", "-c", type=str, default=None, help="Custom checkpoint path")
    parser.add_argument("--output-json", "-o", type=str, default=None, help="Save report to JSON path")

    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-8s | %(message)s")

    predictor = VideoPredictor(checkpoint_path=args.checkpoint)
    res = predictor.predict_video(args.input, n_frames=args.frames)
    print_video_report(res)

    if args.output_json:
        out_p = Path(args.output_json)
        out_p.parent.mkdir(parents=True, exist_ok=True)
        with open(out_p, "w", encoding="utf-8") as f:
            json.dump(res, f, indent=2)
        print(f"Report saved to: {out_p}")
