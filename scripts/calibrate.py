#!/usr/bin/env python3
"""
calibrate.py — interactive per-camera polygon calibrator.

Extracts one representative frame from a CCTV video and lets you click
to draw a polygon for each named zone.  The result is saved as a
camera-specific layout JSON that ZoneMapper can consume directly.

Usage
-----
    python scripts/calibrate.py \\
        --video  "data/resource/CCTV Footage/CAM 1.mp4" \\
        --output data/resource/camera_1_layout.json \\
        [--zones zone_entry,zone_skincare,zone_makeup,zone_haircare,zone_fragrance,zone_billing] \\
        [--names "Store Entry,Skincare,Makeup,Haircare,Fragrance,Billing"] \\
        [--frame 60]

    # Minimal: uses default zone list and frame 60
    python scripts/calibrate.py \\
        --video  "data/resource/CCTV Footage/CAM 1.mp4" \\
        --output data/resource/camera_1_layout.json

Controls (shown in the window title bar)
-----------------------------------------
  Left-click     Add a vertex to the current polygon
  Backspace      Remove the last vertex added
  Enter / Space  Confirm polygon (minimum 3 vertices required)
  S              Skip this zone — no polygon saved, ZoneMapper ignores it
  R              Reset — clear all vertices for the current zone
  Q              Save what you have so far and quit immediately

Output format
-------------
    {
      "camera_id": "cam_entry",
      "source_video": "CAM 1.mp4",
      "frame_index": 60,
      "zones": [
        {
          "zone_id":       "zone_entry",
          "name":          "Store Entry",
          "pixel_polygon": [[x1,y1], [x2,y2], [x3,y3], ...]
        },
        ...
      ]
    }

Zones for which S was pressed are omitted entirely.  ZoneMapper already
handles missing pixel_polygon keys gracefully (it skips those zones),
so sparse calibration is valid for cameras that only cover a subset of
the store.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import cv2
import numpy as np


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULT_ZONES = [
    "zone_entry",
    "zone_skincare",
    "zone_makeup",
    "zone_haircare",
    "zone_fragrance",
    "zone_billing",
]

DEFAULT_NAMES = [
    "Store Entry / Exit",
    "Skincare Aisle",
    "Makeup & Colour Cosmetics",
    "Haircare & Styling",
    "Fragrance & Wellness",
    "Billing Counter",
]

# Visually distinct BGR colours for up to 10 completed polygons
_ZONE_COLOURS = [
    (0,  255,  0),    # bright green
    (0,  128, 255),   # orange
    (255,  0, 128),   # pink/magenta
    (255, 255,  0),   # cyan
    (0,   0,  255),   # red
    (255, 128,  0),   # sky blue
    (128,  0, 255),   # violet
    (0,  255, 255),   # yellow
    (128, 255,  0),   # lime
    (255,  0,  0),    # blue
]


# ---------------------------------------------------------------------------
# Drawing helpers
# ---------------------------------------------------------------------------

def _draw_completed(canvas: np.ndarray, zones_done: list[dict]) -> None:
    """Overlay all confirmed polygons as semi-transparent filled regions."""
    for i, z in enumerate(zones_done):
        pts = np.array(z["pixel_polygon"], dtype=np.int32)
        colour = _ZONE_COLOURS[i % len(_ZONE_COLOURS)]
        overlay = canvas.copy()
        cv2.fillPoly(overlay, [pts], colour)
        cv2.addWeighted(overlay, 0.25, canvas, 0.75, 0, canvas)
        cv2.polylines(canvas, [pts], isClosed=True, color=colour, thickness=2)
        # Label in the centroid
        cx, cy = pts.mean(axis=0).astype(int)
        cv2.putText(canvas, z["zone_id"], (cx - 40, cy),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.55, colour, 2, cv2.LINE_AA)


def _draw_in_progress(
    canvas: np.ndarray,
    points: list[tuple[int, int]],
    colour: tuple[int, int, int],
    cursor: tuple[int, int] | None,
) -> None:
    """Draw the polygon currently being constructed."""
    for pt in points:
        cv2.circle(canvas, pt, 5, colour, -1)
    if len(points) >= 2:
        cv2.polylines(canvas, [np.array(points, dtype=np.int32)],
                      isClosed=False, color=colour, thickness=2)
    # Rubber-band line from last point to cursor
    if points and cursor:
        cv2.line(canvas, points[-1], cursor, colour, 1, cv2.LINE_AA)
    # Close preview when >= 3 points
    if len(points) >= 3 and cursor:
        cv2.line(canvas, points[0], cursor, colour, 1, cv2.LINE_AA)


def _draw_hud(
    canvas: np.ndarray,
    zone_id: str,
    name: str,
    index: int,
    total: int,
    n_pts: int,
) -> None:
    """Render instructions and status onto the frame."""
    h, w = canvas.shape[:2]
    # Semi-transparent black bar at top
    bar = canvas.copy()
    cv2.rectangle(bar, (0, 0), (w, 80), (0, 0, 0), -1)
    cv2.addWeighted(bar, 0.6, canvas, 0.4, 0, canvas)

    line1 = f"Zone {index+1}/{total}: [{zone_id}]  \"{name}\"  —  {n_pts} point(s)"
    line2 = "Click=add  Backspace=undo  Enter/Space=confirm(≥3pts)  S=skip  R=reset  Q=save&quit"
    cv2.putText(canvas, line1, (8, 24),
                cv2.FONT_HERSHEY_SIMPLEX, 0.65, (255, 255, 255), 2, cv2.LINE_AA)
    cv2.putText(canvas, line2, (8, 58),
                cv2.FONT_HERSHEY_SIMPLEX, 0.48, (200, 200, 200), 1, cv2.LINE_AA)


# ---------------------------------------------------------------------------
# Calibration session
# ---------------------------------------------------------------------------

def calibrate(
    frame: np.ndarray,
    zone_ids: list[str],
    zone_names: list[str],
    window_name: str = "Calibrate",
) -> list[dict]:
    """
    Interactive polygon-drawing session.

    Returns a list of zone dicts (only confirmed zones, skipped zones omitted).
    """
    zones_done: list[dict] = []
    cursor: list[tuple[int, int]] = [(0, 0)]   # mutable container for mouse pos

    def on_mouse(event, x, y, _flags, _param):
        cursor[0] = (x, y)

    cv2.namedWindow(window_name, cv2.WINDOW_NORMAL)
    cv2.setMouseCallback(window_name, on_mouse)

    zone_index = 0
    current_pts: list[tuple[int, int]] = []
    quit_early = False

    while zone_index < len(zone_ids):
        zone_id   = zone_ids[zone_index]
        zone_name = zone_names[zone_index] if zone_index < len(zone_names) else zone_id
        colour    = _ZONE_COLOURS[zone_index % len(_ZONE_COLOURS)]

        # Rebuild display on every loop iteration
        display = frame.copy()
        _draw_completed(display, zones_done)
        _draw_in_progress(display, current_pts, colour, cursor[0])
        _draw_hud(display, zone_id, zone_name, zone_index, len(zone_ids), len(current_pts))
        cv2.imshow(window_name, display)

        key = cv2.waitKey(20) & 0xFF

        if key == 255:
            # No key pressed — check for mouse click via a flag approach
            # We handle clicks via setMouseCallback below
            pass

        # Mouse click is captured here instead of waitKey
        # We re-register the callback each loop to capture a single click
        _click: list[tuple[int, int] | None] = [None]

        def _click_handler(event, x, y, flags, param):
            cursor[0] = (x, y)
            if event == cv2.EVENT_LBUTTONDOWN:
                _click[0] = (x, y)

        cv2.setMouseCallback(window_name, _click_handler)

        # Block until a key or click event
        while True:
            display = frame.copy()
            _draw_completed(display, zones_done)
            _draw_in_progress(display, current_pts, colour, cursor[0])
            _draw_hud(display, zone_id, zone_name, zone_index, len(zone_ids), len(current_pts))
            cv2.imshow(window_name, display)

            key = cv2.waitKey(30) & 0xFF

            # Left click → add point
            if _click[0] is not None:
                current_pts.append(_click[0])
                _click[0] = None
                break

            # Backspace → undo last point
            if key in (8, 127):
                if current_pts:
                    current_pts.pop()
                break

            # Enter or Space → confirm
            if key in (13, 32):
                if len(current_pts) >= 3:
                    zones_done.append({
                        "zone_id":       zone_id,
                        "name":          zone_name,
                        "pixel_polygon": list(current_pts),
                    })
                    print(f"  [✓] {zone_id}: {len(current_pts)} vertices confirmed.")
                    current_pts = []
                    zone_index += 1
                else:
                    print(f"  [!] Need at least 3 points to confirm (have {len(current_pts)}).")
                break

            # S → skip
            if key == ord('s'):
                print(f"  [-] {zone_id}: skipped.")
                current_pts = []
                zone_index += 1
                break

            # R → reset
            if key == ord('r'):
                print(f"  [~] {zone_id}: reset.")
                current_pts = []
                break

            # Q → save and quit
            if key == ord('q') or cv2.getWindowProperty(window_name, cv2.WND_PROP_VISIBLE) < 1:
                print("  [Q] Quit early — saving confirmed zones.")
                quit_early = True
                break

        if quit_early:
            break

    cv2.destroyWindow(window_name)
    return zones_done


# ---------------------------------------------------------------------------
# Frame extraction
# ---------------------------------------------------------------------------

def extract_frame(video_path: str, frame_index: int) -> np.ndarray:
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        print(f"[ERROR] Cannot open video: {video_path!r}", file=sys.stderr)
        sys.exit(1)

    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    fps   = cap.get(cv2.CAP_PROP_FPS) or 30.0
    print(f"[calibrate] Video: {total} frames at {fps:.1f} FPS "
          f"({total/fps:.1f}s) — using frame {frame_index}")

    cap.set(cv2.CAP_PROP_POS_FRAMES, min(frame_index, max(0, total - 1)))
    ret, frame = cap.read()
    cap.release()

    if not ret or frame is None:
        print(f"[ERROR] Could not read frame {frame_index}", file=sys.stderr)
        sys.exit(1)

    return frame


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Interactive per-camera zone polygon calibrator.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--video",  required=True,
                   help="Path to the CCTV video file (mp4, avi, rtsp, …)")
    p.add_argument("--output", required=True,
                   help="Destination JSON file (e.g. data/resource/camera_1_layout.json)")
    p.add_argument("--zones",
                   help=f"Comma-separated zone_ids to calibrate. "
                        f"Default: {','.join(DEFAULT_ZONES)}")
    p.add_argument("--names",
                   help="Comma-separated human-readable names matching --zones order. "
                        "Used only for on-screen labelling; optional.")
    p.add_argument("--frame", type=int, default=60,
                   help="Video frame index to use as the calibration background. "
                        "Default: 60  (avoids dark fade-in on most footage).")
    p.add_argument("--camera-id", dest="camera_id", default="",
                   help="Camera ID written to the output JSON (informational).")
    return p.parse_args()


def main() -> None:
    args = _parse_args()

    zone_ids   = [z.strip() for z in args.zones.split(",")] if args.zones else DEFAULT_ZONES
    zone_names = [n.strip() for n in args.names.split(",")] if args.names  else DEFAULT_NAMES
    # Pad names if shorter than ids
    while len(zone_names) < len(zone_ids):
        zone_names.append(zone_ids[len(zone_names)])

    print(f"[calibrate] Zones to calibrate: {zone_ids}")
    print(f"[calibrate] Output:              {args.output}")

    frame = extract_frame(args.video, args.frame)

    print("[calibrate] Opening calibration window…")
    print("            Controls: click=add  Backspace=undo  Enter=confirm  S=skip  R=reset  Q=quit")
    print()

    zones_done = calibrate(
        frame,
        zone_ids,
        zone_names,
        window_name=f"Calibrate — {Path(args.video).name}",
    )

    if not zones_done:
        print("[calibrate] No zones confirmed — output file not written.")
        sys.exit(0)

    output = {
        "camera_id":    args.camera_id or Path(args.output).stem,
        "source_video": Path(args.video).name,
        "frame_index":  args.frame,
        "zones":        zones_done,
    }

    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(output, indent=2))

    print(f"\n[calibrate] Saved {len(zones_done)} zone(s) → {args.output}")
    for z in zones_done:
        pts = z["pixel_polygon"]
        print(f"  {z['zone_id']}: {len(pts)} vertices — "
              f"bbox x=[{min(p[0] for p in pts)},{max(p[0] for p in pts)}] "
              f"y=[{min(p[1] for p in pts)},{max(p[1] for p in pts)}]")


if __name__ == "__main__":
    main()
