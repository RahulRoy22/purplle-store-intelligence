#!/usr/bin/env bash
# ==============================================================================
# run_all_cameras.sh — launch one independent pipeline process per camera.
#
# Each process is a separate OS-level Python process, so GPU/CPU work
# is distributed across 5 independent runtimes rather than bottlenecking
# inside a single asyncio loop.  Each process writes its own log file.
#
# Prerequisites
# -------------
#   1. pip install -r services/pipeline/requirements.txt
#   2. API service is running:  docker compose up api -d
#   3. Every camera has been calibrated:
#        python scripts/calibrate.py \
#            --video  "data/resource/CCTV Footage/CAM 1.mp4" \
#            --output data/resource/camera_1_layout.json \
#            --camera-id cam_entry
#      (repeat for CAM 2–5 with the IDs in the CAMERAS array below)
#
# Usage
# -----
#   ./run_all_cameras.sh                    # use defaults
#   API_URL=http://192.168.1.10:8000 ./run_all_cameras.sh
#   STORE_ID=store_002 YOLO_WEIGHTS=yolov8s.pt ./run_all_cameras.sh
#
# Environment overrides (all optional)
# -------------------------------------
#   API_URL        Base URL of the ingest API.  Default: http://localhost:8000
#   STORE_ID       Store ID written to every event.  Default: store_001
#   YOLO_WEIGHTS   YOLOv8 weights file or name.    Default: yolov8n.pt
#   BATCH_SIZE     Events per POST.                Default: 100
#   RETRY_DELAYS   Comma-separated retry seconds.  Default: 1.0,2.0,4.0
#   LOG_LEVEL      Python logging level.           Default: INFO
# ==============================================================================
set -euo pipefail

# ---------------------------------------------------------------------------
# Paths (all resolved relative to this script, so it runs from any cwd)
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$SCRIPT_DIR"
PIPELINE_DIR="$PROJECT_ROOT/services/pipeline"
FOOTAGE_DIR="$PROJECT_ROOT/data/resource/CCTV Footage"
RESOURCE_DIR="$PROJECT_ROOT/data/resource"
LOG_DIR="$PROJECT_ROOT/logs/pipeline"

# ---------------------------------------------------------------------------
# Python interpreter — prefer the project venv so the script works correctly
# whether or not the caller has manually activated the venv first.
# Windows (Git Bash / MINGW64) stores the interpreter at Scripts/python;
# Linux/macOS use bin/python.
# ---------------------------------------------------------------------------
if [[ -f "$PROJECT_ROOT/venv/Scripts/python" ]]; then
  PYTHON="$PROJECT_ROOT/venv/Scripts/python"
elif [[ -f "$PROJECT_ROOT/venv/bin/python" ]]; then
  PYTHON="$PROJECT_ROOT/venv/bin/python"
else
  PYTHON="python"
  echo "[WARN]  No venv found at $PROJECT_ROOT/venv — falling back to system Python."
  echo "        Run: python -m venv venv && pip install -r services/pipeline/requirements.txt"
fi
echo " Python      : $PYTHON"

# ---------------------------------------------------------------------------
# Shared config (can be overridden by env)
# ---------------------------------------------------------------------------
API_URL="${API_URL:-http://localhost:8000}"
STORE_ID="${STORE_ID:-store_001}"
YOLO_WEIGHTS="${YOLO_WEIGHTS:-yolov8n.pt}"
BATCH_SIZE="${BATCH_SIZE:-100}"
RETRY_DELAYS="${RETRY_DELAYS:-1.0,2.0,4.0}"
LOG_LEVEL="${LOG_LEVEL:-INFO}"

# ---------------------------------------------------------------------------
# Camera definitions — FORMAT: "CAMERA_ID|VIDEO_FILE|LAYOUT_FILE"
# Adjust CAMERA_ID to match the camera_id values used during calibrate.py
# ---------------------------------------------------------------------------
declare -a CAMERAS=(
  "cam_entry|CAM 1.mp4|camera_1_layout.json"
  "cam_floor_01|CAM 2.mp4|camera_2_layout.json"
  "cam_floor_02|CAM 3.mp4|camera_3_layout.json"
  "cam_floor_03|CAM 4.mp4|camera_4_layout.json"
  "cam_billing|CAM 5.mp4|camera_5_layout.json"
)

# ---------------------------------------------------------------------------
# PID tracking for graceful shutdown
# ---------------------------------------------------------------------------
declare -a PIDS=()
declare -a CAM_IDS=()

cleanup() {
  echo ""
  echo "[run_all_cameras] Signal received — stopping all pipeline processes…"

  for i in "${!PIDS[@]}"; do
    pid="${PIDS[$i]}"
    cam="${CAM_IDS[$i]}"
    if kill -0 "$pid" 2>/dev/null; then
      # SIGINT triggers the KeyboardInterrupt path in main.py,
      # which calls flush_exits() before shutdown.
      kill -SIGINT "$pid"
      echo "  → Sent SIGINT to $cam (PID $pid) — awaiting flush_exits()…"
    fi
  done

  # Allow up to 10 s for graceful flush before force-killing
  local deadline=$(( $(date +%s) + 10 ))
  while [[ $(date +%s) -lt $deadline ]]; do
    local alive=0
    for pid in "${PIDS[@]}"; do
      kill -0 "$pid" 2>/dev/null && alive=1
    done
    [[ $alive -eq 0 ]] && break
    sleep 1
  done

  for i in "${!PIDS[@]}"; do
    pid="${PIDS[$i]}"
    cam="${CAM_IDS[$i]}"
    if kill -0 "$pid" 2>/dev/null; then
      kill -SIGKILL "$pid"
      echo "  → Force-killed $cam (PID $pid) after timeout."
    else
      echo "  → $cam (PID $pid) shut down cleanly."
    fi
  done

  echo "[run_all_cameras] All processes stopped."
  echo "[run_all_cameras] Logs → $LOG_DIR"
  exit 0
}

trap cleanup SIGINT SIGTERM

# ---------------------------------------------------------------------------
# Pre-flight checks
# ---------------------------------------------------------------------------
echo "============================================================"
echo " run_all_cameras.sh — Purplle Store Intelligence Pipeline"
echo "============================================================"
echo " API_URL     : $API_URL"
echo " STORE_ID    : $STORE_ID"
echo " YOLO_WEIGHTS: $YOLO_WEIGHTS"
echo " Log dir     : $LOG_DIR"
echo "------------------------------------------------------------"

PREFLIGHT_OK=1

# Check pipeline main.py exists
if [[ ! -f "$PIPELINE_DIR/main.py" ]]; then
  echo "[ERROR] Pipeline not found: $PIPELINE_DIR/main.py"
  PREFLIGHT_OK=0
fi

# Check API is reachable
if command -v curl &>/dev/null; then
  if ! curl -sf "$API_URL/health" >/dev/null 2>&1; then
    echo "[WARN]  API health check failed at $API_URL/health"
    echo "        Ensure the API container is running before proceeding."
    # Warn but don't abort — the API might start shortly
  else
    echo "[OK]    API is healthy."
  fi
fi

# Check each camera's video and layout
for entry in "${CAMERAS[@]}"; do
  IFS='|' read -r cam_id video_file layout_file <<< "$entry"
  video_path="$FOOTAGE_DIR/$video_file"
  layout_path="$RESOURCE_DIR/$layout_file"

  if [[ ! -f "$video_path" ]]; then
    echo "[ERROR] Missing video  : $video_path"
    PREFLIGHT_OK=0
  else
    echo "[OK]    Video found    : $video_file"
  fi

  if [[ ! -f "$layout_path" ]]; then
    echo "[ERROR] Missing layout : $layout_path"
    echo "        Run: python scripts/calibrate.py \\"
    echo "               --video  \"$video_path\" \\"
    echo "               --output \"$layout_path\" \\"
    echo "               --camera-id $cam_id"
    PREFLIGHT_OK=0
  else
    echo "[OK]    Layout found   : $layout_file"
  fi
done

if [[ $PREFLIGHT_OK -eq 0 ]]; then
  echo ""
  echo "[run_all_cameras] Pre-flight FAILED. Fix the errors above, then re-run."
  exit 1
fi

echo "------------------------------------------------------------"
echo " Pre-flight passed. Launching ${#CAMERAS[@]} pipeline process(es)…"
echo "------------------------------------------------------------"

# ---------------------------------------------------------------------------
# Create log directory
# ---------------------------------------------------------------------------
mkdir -p "$LOG_DIR"

# ---------------------------------------------------------------------------
# Launch one process per camera
# ---------------------------------------------------------------------------
for entry in "${CAMERAS[@]}"; do
  IFS='|' read -r cam_id video_file layout_file <<< "$entry"

  log_file="$LOG_DIR/${cam_id}.log"
  video_path="$FOOTAGE_DIR/$video_file"
  layout_path="$RESOURCE_DIR/$layout_file"

  (
    cd "$PIPELINE_DIR"
    exec env \
      VIDEO_SOURCE="$video_path"  \
      CAMERA_ID="$cam_id"         \
      LAYOUT_PATH="$layout_path"  \
      STORE_ID="$STORE_ID"        \
      API_URL="$API_URL"          \
      YOLO_WEIGHTS="$YOLO_WEIGHTS"\
      BATCH_SIZE="$BATCH_SIZE"    \
      RETRY_DELAYS="$RETRY_DELAYS"\
      LOG_LEVEL="$LOG_LEVEL"      \
      "$PYTHON" main.py
  ) >> "$log_file" 2>&1 &

  PID=$!
  PIDS+=("$PID")
  CAM_IDS+=("$cam_id")
  echo "  [PID $PID] $cam_id  ← $video_file  →  $log_file"
done

echo ""
echo "[run_all_cameras] All ${#CAMERAS[@]} processes running."
echo "[run_all_cameras] Press Ctrl+C to flush all exits and stop gracefully."
echo "[run_all_cameras] Tail a camera log with:"
echo "                  tail -f $LOG_DIR/<cam_id>.log"
echo ""

# ---------------------------------------------------------------------------
# Wait loop — reap children and report non-zero exits
# ---------------------------------------------------------------------------
FAILED_CAMS=()

for i in "${!PIDS[@]}"; do
  pid="${PIDS[$i]}"
  cam="${CAM_IDS[$i]}"
  if wait "$pid"; then
    echo "[run_all_cameras] $cam (PID $pid): completed OK."
  else
    code=$?
    echo "[run_all_cameras] $cam (PID $pid): exited with code $code. Check: $LOG_DIR/${cam}.log"
    FAILED_CAMS+=("$cam")
  fi
done

echo ""
if [[ ${#FAILED_CAMS[@]} -gt 0 ]]; then
  echo "[run_all_cameras] FAILED cameras: ${FAILED_CAMS[*]}"
  exit 1
else
  echo "[run_all_cameras] All cameras completed successfully."
fi
