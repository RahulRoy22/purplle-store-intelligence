# Purplle Store Intelligence

Real-time CCTV analytics pipeline for retail stores. Detects visitors via YOLOv8, tracks them with ByteTrack, re-identifies across frames with OSNet, maps positions to store zones, and streams structured events to a FastAPI analytics API backed by SQLite.

---

## Quick Start (API + Mock Data)

Five commands to get the full stack running:

```bash
# 1. Clone and enter the project
git clone https://github.com/RahulRoy22/purplle-store-intelligence && cd purplle-store-intelligence

# 2. Copy environment config
cp .env.example .env

# 3. Build and start the API (includes mock data seed)
docker compose up --build -d

# 4. Wait for health check to pass, then verify
curl http://localhost:8000/health

# 5. Query live analytics
curl http://localhost:8000/stores/STORE_BLR_002/metrics
```

Expected `/health` response:
```json
{
  "status": "ok",
  "db": "connected",
  "stale_feed": false,
  "last_event_at": null,
  "checked_at": "2026-05-30T..."
}
```

API docs (Swagger UI): http://localhost:8000/docs

---

## Running the Detection Pipeline

The CV pipeline processes CCTV footage and streams events to the API. It requires Python 3.11+ and the pipeline dependencies.

### Prerequisites

1. **Install pipeline dependencies** (inside a virtual environment):

```bash
python -m venv venv
# Windows:
venv\Scripts\activate
# Linux/macOS:
source venv/bin/activate

pip install -r services/pipeline/requirements.txt
```

> **GPU acceleration (optional):** Edit `services/pipeline/requirements.txt` and uncomment the CUDA index line matching your driver version before installing.

2. **Download CCTV footage** — place the 5 MP4 files into `data/resource/CCTV Footage/`:
   - `CAM 1.mp4` through `CAM 5.mp4`

3. **Calibrate cameras** — draw zone polygons interactively for each camera:

```bash
python scripts/calibrate.py \
    --video  "data/resource/CCTV Footage/CAM 1.mp4" \
    --output data/resource/camera_1_layout.json \
    --camera-id cam_entry
```

Controls: **Left-click** to add polygon vertex | **Backspace** to undo | **Enter** to confirm (≥ 3 points) | **S** to skip zone | **R** to reset | **Q** to finish

Repeat for cameras 2–5 with these IDs and output files:

| Camera | `--camera-id`  | `--output`                        |
|--------|---------------|----------------------------------|
| CAM 2  | cam_floor_01  | data/resource/camera_2_layout.json |
| CAM 3  | cam_floor_02  | data/resource/camera_3_layout.json |
| CAM 4  | cam_floor_03  | data/resource/camera_4_layout.json |
| CAM 5  | cam_billing   | data/resource/camera_5_layout.json |

4. **Ensure the API is running:**

```bash
docker compose up api -d
```

### Run All Cameras

```bash
./run_all_cameras.sh
```

This launches one independent Python process per camera. Each process writes its log to `logs/pipeline/<cam_id>.log`. Press `Ctrl+C` to flush all in-flight visitor exits and shut down gracefully.

**Tail a camera log:**
```bash
tail -f logs/pipeline/cam_entry.log
```

**Override defaults:**
```bash
API_URL=http://192.168.1.10:8000 \
STORE_ID=store_002 \
YOLO_WEIGHTS=yolov8s.pt \
./run_all_cameras.sh
```

### Run a Single Camera

```bash
cd services/pipeline
VIDEO_SOURCE="../../data/resource/CCTV Footage/CAM 1.mp4" \
CAMERA_ID=cam_entry \
LAYOUT_PATH=../../data/resource/camera_1_layout.json \
STORE_ID=STORE_BLR_002 \
DB_PATH=/data/store_intelligence.db \
API_URL=http://localhost:8000 \
python main.py
```

---

## Live Dashboard

A web dashboard is served directly by the API — no separate process needed.

```
http://localhost:8000/dashboard
```

Open that URL after `docker compose up` and you'll see a live-updating display that auto-refreshes every 5 seconds:

- **KPI cards** — unique visitors, conversion rate, avg dwell, billing abandonment
- **Conversion funnel** — 4-stage percentage bar chart (Entry → Browse → Billing → Purchase)  
- **Zone heatmap** — colour-coded bars per zone with `data_confidence` badge (low / high)
- **Anomaly panel** — severity-coded cards (CRITICAL / WARN / INFO) with `suggested_action`

All data comes from the same REST endpoints; the page uses plain JavaScript `fetch` with `setInterval`.

---

## Analytics API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Service health + last event timestamp, global and **per store** |
| POST | `/events/ingest` | Ingest a batch of CV events (idempotent) |
| GET | `/stores/{id}/metrics` | KPI snapshot: visitors, conversion, **per-zone dwell**, **current queue depth**, abandonment |
| GET | `/stores/{id}/funnel` | Entry → browse → billing → purchase funnel |
| GET | `/stores/{id}/heatmap` | Zone visit frequency and heat score (0–100) |
| GET | `/stores/{id}/anomalies` | Rule-based alerts: queue depth, abandonment, **dead zone**, **conversion drop** |

Full interactive documentation: http://localhost:8000/docs

---

## Running Tests

```bash
# API tests
cd purplle-store-intelligence
pip install -r services/api/requirements.txt
PYTHONPATH=services/api pytest tests/ -v

# Pipeline tests
PYTHONPATH="services/pipeline:services/api" pytest tests/pipeline/ -v

# Data contract assertions
pytest data/mock/assertions.py -v
```

---

## Project Structure

```
purplle-store-intelligence/
├── docker-compose.yml          # Zero-touch stack: api + seed
├── .env.example                # Environment variable reference
├── run_all_cameras.sh          # Launch all 5 camera pipelines
├── scripts/
│   └── calibrate.py            # Interactive zone polygon drawing tool
├── services/
│   ├── api/                    # FastAPI analytics API
│   │   ├── main.py             # App factory, DB init, POS CSV loader
│   │   ├── routers/            # health, ingest, stores endpoints
│   │   ├── models/             # Pydantic request/response models
│   │   └── db/                 # SQL query functions
│   └── pipeline/               # CV detection pipeline
│       ├── main.py             # Async orchestrator
│       ├── detector.py         # YOLOv8 person detection wrapper
│       ├── tracker.py          # ByteTrack multi-object tracking wrapper
│       ├── reid.py             # OSNet re-identification
│       ├── staff_classifier.py # HSV-based staff detection
│       ├── zone_mapper.py      # Shapely polygon zone mapping
│       ├── state_machine.py    # Per-track event synthesis
│       ├── event_builder.py    # EventIn schema stamping
│       └── ingest_client.py    # Async HTTP batch sender with retry
├── data/
│   ├── mock/                   # Mock data generators
│   ├── generated/              # Generated data (gitignored)
│   └── resource/               # CCTV footage + layout files (gitignored)
├── tests/                      # Pytest suite (API + pipeline)
├── docs/
│   └── CHOICES.md              # Architectural decision records
└── DESIGN.md                   # System design document
```

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `STORE_ID` | `STORE_BLR_002` | Store identifier written to every event |
| `API_URL` | `http://localhost:8000` | API base URL (pipeline → API) |
| `DB_PATH` | `/data/store_intelligence.db` | SQLite database path |
| `POS_CSV_PATH` | *(none)* | Path to POS transactions CSV |
| `LOG_LEVEL` | `INFO` | Python logging level |
| `YOLO_WEIGHTS` | `yolov8n.pt` | YOLO model weights (auto-downloaded) |
| `REID_THRESHOLD` | `0.75` | Cosine similarity threshold for Re-ID |
| `STAFF_THRESHOLD` | `0.30` | HSV pixel fraction to classify as staff |
| `BATCH_SIZE` | `100` | Events per POST to `/events/ingest` |
| `RETRY_DELAYS` | `1.0,2.0,4.0` | Retry backoff delays in seconds |
