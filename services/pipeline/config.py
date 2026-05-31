"""
config.py — Pipeline configuration loaded from environment variables.

All settings have sensible defaults so the pipeline runs out-of-the-box
against a local webcam and a locally running API server.  Override any
value by setting the corresponding environment variable before launch.

Environment variables
---------------------
VIDEO_SOURCE          Path/URL/device index for cv2.VideoCapture.
                      Use "0" for the default webcam, or an RTSP/HTTP
                      stream URL, or an absolute path to a video file.
                      Default: "0"

API_URL               Base URL of the running FastAPI ingest service.
                      Default: "http://localhost:8000"

STORE_ID              Store identifier written to every emitted event.
                      Default: "STORE_BLR_002"

CAMERA_ID             Fallback camera identifier (used when zone_camera_map
                      has no entry for a given zone).
                      Default: "cam_entry"

LAYOUT_PATH           Absolute or relative path to store_layout.json.
                      Default: "data/generated/store_layout.json"

YOLO_WEIGHTS          Path to YOLOv8 weights file, or a model name that
                      ultralytics will auto-download on first run.
                      Default: "yolov8n.pt"

REID_THRESHOLD        Minimum cosine similarity for a Re-ID match to be
                      accepted as a returning visitor (0.0–1.0).
                      Default: 0.85

STAFF_HSV_LOWER       Comma-separated H,S,V lower bound for the staff
                      uniform colour filter (OpenCV HSV scale).
                      Default: "100,50,50"  (blue-ish)

STAFF_HSV_UPPER       Comma-separated H,S,V upper bound.
                      Default: "130,255,255"

STAFF_THRESHOLD       Fraction of in-range pixels required to flag a crop
                      as staff (0.0–1.0).
                      Default: 0.25

BATCH_SIZE            Maximum events per /events/ingest POST request.
                      Default: 100

RETRY_DELAYS          Comma-separated seconds to wait before each retry on
                      HTTP 503.  Length determines max retry count.
                      Default: "1.0,2.0,4.0"

LOG_LEVEL             Python logging level name.
                      Default: "INFO"
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _parse_hsv(env_value: str) -> list[int]:
    """Parse "H,S,V" string into a three-element int list."""
    parts = [p.strip() for p in env_value.split(",")]
    if len(parts) != 3:
        raise ValueError(
            f"HSV value must be 'H,S,V' (3 comma-separated ints), got: {env_value!r}"
        )
    return [int(p) for p in parts]


def _parse_delays(env_value: str) -> tuple[float, ...]:
    """Parse "1.0,2.0,4.0" string into a tuple of floats."""
    if not env_value.strip():
        return ()
    return tuple(float(p.strip()) for p in env_value.split(","))


def _video_source(raw: str) -> str | int:
    """Return int if raw is a digit string (webcam index), else the raw string."""
    return int(raw) if raw.isdigit() else raw


@dataclass(frozen=True)
class PipelineConfig:
    video_source: str | int
    api_url: str
    store_id: str
    camera_id: str
    layout_path: str
    yolo_weights: str
    reid_threshold: float
    staff_hsv_lower: list[int]
    staff_hsv_upper: list[int]
    staff_threshold: float
    batch_size: int
    retry_delays: tuple[float, ...]
    log_level: str

    @classmethod
    def from_env(cls) -> "PipelineConfig":
        return cls(
            video_source=_video_source(os.getenv("VIDEO_SOURCE", "0")),
            api_url=os.getenv("API_URL", "http://localhost:8000"),
            store_id=os.getenv("STORE_ID", "STORE_BLR_002"),
            camera_id=os.getenv("CAMERA_ID", "cam_entry"),
            layout_path=os.getenv("LAYOUT_PATH", "data/generated/store_layout.json"),
            yolo_weights=os.getenv("YOLO_WEIGHTS", "yolov8n.pt"),
            reid_threshold=float(os.getenv("REID_THRESHOLD", "0.85")),
            staff_hsv_lower=_parse_hsv(os.getenv("STAFF_HSV_LOWER", "100,50,50")),
            staff_hsv_upper=_parse_hsv(os.getenv("STAFF_HSV_UPPER", "130,255,255")),
            staff_threshold=float(os.getenv("STAFF_THRESHOLD", "0.25")),
            batch_size=int(os.getenv("BATCH_SIZE", "100")),
            retry_delays=_parse_delays(os.getenv("RETRY_DELAYS", "1.0,2.0,4.0")),
            log_level=os.getenv("LOG_LEVEL", "INFO"),
        )
