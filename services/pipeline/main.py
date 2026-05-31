"""
main.py — Phase 4 pipeline orchestrator.

Ties together every pipeline component into a single async video loop:

    VideoCapture → PersonDetector → PersonTracker
        → StaffClassifier (HSV) → ReIdentifier (OSNet)
        → ZoneMapper → TrackStateMachine
        → EventBuilder → IngestClient (async, non-blocking)

Graceful shutdown on KeyboardInterrupt or stream EOF:
  1. flush_exits() is called on the state machine so in-flight tracks
     receive EXIT events.
  2. All pending HTTP tasks are awaited before the process exits.
  3. cv2.VideoCapture and windows are released cleanly.

Usage
-----
Run directly from the pipeline service directory:

    cd services/pipeline
    python main.py

Or via environment variables:

    VIDEO_SOURCE=/path/to/video.mp4 \\
    API_URL=http://localhost:8000    \\
    LAYOUT_PATH=../../data/generated/store_layout.json \\
    python main.py

See config.py for the full list of configurable environment variables.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import cv2
import numpy as np

# Local pipeline modules (all in the same directory)
from config import PipelineConfig
from detector import PersonDetector
from event_builder import EventBuilder
from ingest_client import IngestClient, IngestError
from reid import ReIdentifier, SharedRegistry
from staff_classifier import StaffClassifier
from state_machine import TrackStateMachine
from tracker import PersonTracker
from zone_mapper import ZoneMapper


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

log = logging.getLogger("pipeline")


# ---------------------------------------------------------------------------
# Model adapter factories
# (Lazy-imported heavy deps so this module can be imported in test contexts
#  that don't have ultralytics / torch / torchreid installed.)
# ---------------------------------------------------------------------------

def _build_yolo_detector(yolo) -> Callable:
    """
    Wrap a YOLO model into the PersonDetector callable protocol:
        frame_bgr -> list[{"bbox", "confidence", "class_id"}]
    """
    def detect(frame_bgr: np.ndarray) -> list[dict]:
        results = yolo(frame_bgr, verbose=False)[0]
        out = []
        for box in results.boxes:
            out.append({
                "bbox":       box.xyxy[0].tolist(),
                "confidence": float(box.conf[0]),
                "class_id":   int(box.cls[0]),
            })
        return out
    return detect


def _build_bytetrack_adapter(yolo) -> Callable:
    """
    Wrap the same YOLO model — now called via .track() — into the
    PersonTracker callable protocol:
        (detections, frame_bgr) -> list[{"track_id", "bbox", "confidence"}]

    The `detections` argument is intentionally ignored: ByteTrack is
    integrated directly into ultralytics and runs its own detection pass
    on the raw frame with `persist=True` to maintain track continuity
    across calls.
    """
    def track(_detections: list, frame_bgr: np.ndarray) -> list[dict]:
        results = yolo.track(
            frame_bgr,
            persist=True,
            tracker="bytetrack.yaml",
            verbose=False,
            classes=[0],          # COCO class 0 = person
        )[0]
        out = []
        if results.boxes.id is not None:
            for box in results.boxes:
                if box.id is not None:
                    out.append({
                        "track_id":   int(box.id[0]),
                        "bbox":       box.xyxy[0].tolist(),
                        "confidence": float(box.conf[0]),
                    })
        return out
    return track


def _build_osnet_embedder(device: str = "cpu") -> Callable:
    """
    Build an OSNet-x0_25 embedding callable using torchreid.

    Returns a function:
        crop_bgr (np.ndarray, BGR) -> np.ndarray, shape (D,), raw float32

    ReIdentifier.extract_embedding() L2-normalises the output, so the
    raw vector returned here does not need to be normalised.
    """
    import torch
    import torchvision.transforms as T
    import torchreid

    model = torchreid.models.build_model(
        name="osnet_x0_25",
        num_classes=751,      # Market-1501 pretrained checkpoint
        pretrained=True,
        use_gpu=(device == "cuda"),
    )
    model.eval()
    if device == "cuda":
        model = model.cuda()

    # Standard person ReID preprocessing (ImageNet normalisation, 256×128 crop)
    transform = T.Compose([
        T.ToPILImage(),
        T.Resize((256, 128)),
        T.ToTensor(),
        T.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
    ])

    def embed(crop_bgr: np.ndarray) -> np.ndarray:
        rgb = cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2RGB)
        tensor = transform(rgb).unsqueeze(0)
        if device == "cuda":
            tensor = tensor.cuda()
        with torch.no_grad():
            feat = model(tensor)
        return feat.squeeze().cpu().numpy().astype(np.float32)

    return embed


# ---------------------------------------------------------------------------
# Frame processor
# ---------------------------------------------------------------------------

def _process_frame(
    frame: np.ndarray,
    frame_ts: datetime,
    detector: PersonDetector,
    tracker: PersonTracker,
    staff_clf: StaffClassifier,
    re_id: ReIdentifier,
    registry: SharedRegistry,
    registry_threshold: float,
    zone_mapper: ZoneMapper,
    state_machine: TrackStateMachine,
    # Mutable state carried across frames:
    known_track_ids: set[int],
) -> list[dict]:
    """
    Run one frame through the full pipeline.

    Returns a (possibly empty) list of raw state-machine event dicts.
    Mutates *known_track_ids* to track which ByteTrack IDs are new vs.
    seen before (Re-ID only runs on first appearance of a track).
    """
    # --- Detect ---
    detections = detector.detect(frame)

    # --- Track (ByteTrack via ultralytics) ---
    tracks = tracker.update(detections, frame)
    active_ids = tracker.get_active_ids()

    raw_events: list[dict] = []

    for t in tracks:
        track_id = t["track_id"]
        x1, y1, x2, y2 = (max(0, int(v)) for v in t["bbox"])
        crop = frame[y1:y2, x1:x2]

        # --- Zone mapping (centroid) ---
        cx, cy = t["centroid"]
        zone_id = zone_mapper.pixel_to_zone(cx, cy)

        # --- Staff classification ---
        is_staff = staff_clf.is_staff(crop)

        # --- Re-ID: only on first appearance of this track_id ---
        visitor_id: str | None = None
        if track_id not in known_track_ids and not is_staff and crop.size > 0:
            try:
                embedding = re_id.extract_embedding(crop)
                # Cross-camera identity lives in the SHARED registry (SQLite-backed),
                # so the same person seen on the entry cam and a floor cam resolves
                # to ONE visitor_id instead of being double-counted per process.
                visitor_id = registry.lookup(embedding, registry_threshold)  # None = new
                # Defer embedding registration until we have the actual visitor_id
                # (state machine assigns it below on first update)
                _pending_embed = embedding
            except Exception as exc:
                log.warning("Re-ID extract/identify failed for track %d: %s", track_id, exc)
                _pending_embed = None
        else:
            _pending_embed = None

        # --- State machine ---
        events = state_machine.update_track(
            track_id=track_id,
            zone_id=zone_id,
            is_staff=is_staff,
            confidence=t["confidence"],
            frame_ts=frame_ts,
            visitor_id=visitor_id,
        )
        raw_events.extend(events)

        # --- Register embedding under the now-known visitor_id ---
        if track_id not in known_track_ids:
            known_track_ids.add(track_id)
            if _pending_embed is not None and events:
                # ENTRY or REENTRY is always the first event for a new track
                actual_vid = events[0]["visitor_id"]
                registry.register(actual_vid, _pending_embed)

    # --- Flush exits for tracks that disappeared this frame ---
    raw_events.extend(state_machine.flush_exits(active_ids, frame_ts))

    # Clean up known_track_ids for tracks that have now exited
    known_track_ids.intersection_update(active_ids)

    return raw_events


# ---------------------------------------------------------------------------
# Async pipeline runner
# ---------------------------------------------------------------------------

async def run_pipeline(cfg: PipelineConfig) -> None:
    """
    Main async loop.  Call this from __main__ via asyncio.run().
    """
    logging.basicConfig(
        level=cfg.log_level,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        stream=sys.stdout,
    )

    log.info("Initialising models…")

    # --- Heavy model loading (happens once at startup) ---
    import torch
    from ultralytics import YOLO

    device = "cuda" if torch.cuda.is_available() else "cpu"
    log.info("Using device: %s", device)

    yolo = YOLO(cfg.yolo_weights)

    detector   = PersonDetector(_build_yolo_detector(yolo))
    tracker    = PersonTracker(_build_bytetrack_adapter(yolo))
    staff_clf  = StaffClassifier(
        hsv_lower=np.array(cfg.staff_hsv_lower, dtype=np.uint8),
        hsv_upper=np.array(cfg.staff_hsv_upper, dtype=np.uint8),
        threshold=cfg.staff_threshold,
    )
    # ReIdentifier owns the OSNet model call + L2 normalisation (extract_embedding).
    # Identity matching itself is delegated to the SHARED, SQLite-backed registry
    # so every camera process resolves a person to the same visitor_id.
    re_id      = ReIdentifier(_build_osnet_embedder(device), threshold=cfg.reid_threshold)
    registry   = SharedRegistry(cfg.store_id)
    zone_mapper = ZoneMapper.from_file(cfg.layout_path)

    # Build camera map from layout for EventBuilder
    layout = json.loads(Path(cfg.layout_path).read_text(encoding="utf-8"))
    zone_camera_map: dict[str, str] = {
        z["zone_id"]: z["cameras"][0]
        for z in layout.get("zones", [])
        if z.get("cameras")
    }

    state_machine  = TrackStateMachine(cfg.store_id, cfg.camera_id)
    event_builder  = EventBuilder(cfg.store_id, zone_camera_map)
    ingest_client  = IngestClient(
        cfg.api_url,
        batch_size=cfg.batch_size,
        retry_delays=cfg.retry_delays,
    )

    log.info("Opening video source: %r", cfg.video_source)
    cap = cv2.VideoCapture(cfg.video_source)
    if not cap.isOpened():
        raise RuntimeError(f"Cannot open video source: {cfg.video_source!r}")

    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    log.info("Stream opened — %.1f FPS", fps)

    # Mutable per-frame state
    known_track_ids: set[int] = set()
    # Background HTTP dispatch tasks
    pending: set[asyncio.Task] = set()

    def _on_task_done(task: asyncio.Task) -> None:
        pending.discard(task)
        exc = task.exception() if not task.cancelled() else None
        if exc is not None:
            log.error("Ingest task failed: %s", exc)

    frame_count = 0

    try:
        while True:
            ret, frame = cap.read()
            if not ret:
                log.info("Stream ended (EOF or unreadable frame).")
                break

            frame_count += 1
            frame_ts = datetime.now(timezone.utc)

            raw_events = _process_frame(
                frame, frame_ts,
                detector, tracker, staff_clf, re_id, registry, cfg.reid_threshold,
                zone_mapper, state_machine,
                known_track_ids,
            )

            if raw_events:
                stamped = event_builder.build(raw_events)
                task = asyncio.create_task(ingest_client.send(stamped))
                pending.add(task)
                task.add_done_callback(_on_task_done)

            # Yield to the event loop so HTTP tasks can progress
            await asyncio.sleep(0)

    except KeyboardInterrupt:
        log.info("KeyboardInterrupt received — flushing exits.")

    finally:
        log.info("Processed %d frames.", frame_count)

        # --- Flush all remaining tracked visitors as EXITs ---
        final_raw = state_machine.flush_exits(set(), datetime.now(timezone.utc))
        if final_raw:
            log.info("Flushing %d final exit event(s).", len(final_raw))
            try:
                stamped = event_builder.build(final_raw)
                await ingest_client.send(stamped)
            except IngestError as exc:
                log.error("Failed to flush final exits: %s", exc)

        # --- Wait for all in-flight HTTP tasks ---
        if pending:
            log.info("Waiting for %d pending HTTP task(s)…", len(pending))
            await asyncio.gather(*pending, return_exceptions=True)

        cap.release()
        cv2.destroyAllWindows()
        log.info("Pipeline shutdown complete.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    asyncio.run(run_pipeline(PipelineConfig.from_env()))
