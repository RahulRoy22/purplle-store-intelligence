# Purplle Store Intelligence

Real-time CCTV analytics pipeline for retail stores. Detects visitors via YOLOv8, tracks them with ByteTrack, re-identifies across frames with OSNet, maps positions to store zones, and streams structured events to a FastAPI analytics API backed by SQLite.

**Live demo:** https://purplle-store-intelligence-9meb.onrender.com/dashboard
**API docs:** https://purplle-store-intelligence-9meb.onrender.com/docs

> Note: hosted on Render free tier — first load after inactivity takes ~30 seconds to wake up.

---

## Quick Start (API + Mock Data)

Three commands to get the full stack running:

```bash
# 1. Clone and enter the project
git clone https://github.com/RahulRoy22/purplle-store-intelligence && cd purplle-store-intelligence

# 2. Build and start the API (includes mock data seed — no other setup needed)
docker compose up --build -d

# 3. Wait for health check to pass, then query live analytics
curl http://localhost:8000/health
curl http://localhost:8000/stores/STORE_BLR_002/metrics
```

> **Note — `.env` is optional for Docker.** All variables (`STORE_ID`, `DB_PATH`, etc.) are
> already set in `docker-compose.yml`. Copy `.env.example → .env` only if you want to run
> the API locally without Docker (`uvicorn main:app …`), where pydantic-settings reads it.

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

## Working with the Updated Organiser Dataset

The updated drop (`updated data/`) contains **two stores** (`Store 1`, `Store 2`) with per-store
camera roles, PNG floor-plan layouts, a trimmed POS CSV, and a `sample_events.jsonl` whose schema
differs from the canonical one. The system handles all of this without code changes on your part:

- **Dual-schema event ingest.** `POST /events/ingest` accepts **both** the canonical schema *and*
  the organiser's shipped detection schema (lowercase `entry`/`zone_entered`/`queue_completed`,
  `id_token`/`track_id`, naive timestamps, `gender_pred`/`age_pred`/`queue_position_at_join`, …).
  `services/api/models/normalize.py` maps the shipped shape onto the canonical one before
  validation — billing zones collapse to `zone_billing`, and a deterministic `event_id` keeps
  re-ingestion idempotent. You can POST the organiser's `sample_events.jsonl` lines directly:
  ```bash
  # batch the shipped sample (≤500 per request) straight into the API
  curl -s -X POST http://localhost:8000/events/ingest \
       -H 'Content-Type: application/json' \
       -d "{\"events\": $(python -c 'import json,sys; print(json.dumps([json.loads(l) for l in open("updated data/sample_eventsbe42122.jsonl") if l.strip()]))')}"
  ```
- **Real POS CSV.** `load_pos_from_csv` (in `services/api/main.py`) auto-detects the organiser
  schema (`order_id` header), combines `order_date` + `order_time`, treats them as IST (UTC+5:30),
  and stores UTC ISO-8601 — point `POS_CSV_PATH` at the new CSV and it just loads.
- **Two-store zone maps.** `data/mock/real_store_layouts.json` holds the ENTRY/FLOOR/BILLING zone
  definitions for both stores (derived from the floor-plan PNGs — see DESIGN.md "AI-Assisted
  Decisions"), with each zone mapped to its covering camera clip.
  `data/mock/calibration_updated/` holds first-pass per-camera `pixel_polygon` layouts (entry +
  floor + billing per store) ready for `ZoneMapper`; refine vertices with `scripts/calibrate.py`.

`STORE_BLR_002` remains the canonical, always-seeded store the acceptance gate queries.

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

**Against the updated two-store footage** (e.g. Store 1 entry camera). The pipeline is
camera-agnostic — it is driven by the zone map, so you only point it at the clip and a layout:
```bash
cd services/pipeline
VIDEO_SOURCE="../../updated data/Store 1-20260602T101818Z-3-001ec38db8/Store 1/CAM 3 - entry.mp4" \
CAMERA_ID="CAM 3 - entry" \
STORE_ID=STORE_BLR_002 \
API_URL=http://localhost:8000 \
python main.py
```
Emitted events flow through the same `/events/ingest` endpoint and are tagged with `STORE_ID`
(keep `STORE_BLR_002` for the scored demo). Zone definitions for both stores are in
`data/mock/real_store_layouts.json`.

---

## Live Dashboard

A web dashboard is served directly by the API — no separate process needed.

```
http://localhost:8000/dashboard
```

Open that URL after `docker compose up` and you'll see a live-updating display:

- **KPI cards** — unique visitors, conversion rate, avg dwell, billing abandonment
- **Conversion funnel** — 4-stage percentage bar chart (Entry → Browse → Billing → Purchase)
- **Zone heatmap** — colour-coded bars per zone with `data_confidence` badge (low / high)
- **Anomaly panel** — severity-coded cards (CRITICAL / WARN / INFO) with `suggested_action`

Updates are push-based via Server-Sent Events (`GET /stores/{id}/stream`). The API broadcasts a notification after every successful ingest; the dashboard refreshes immediately on receipt — no polling interval.

**Store selector (multi-store).** The navbar dropdown is populated from `/health`'s per-store list; selecting a store re-fetches every panel and re-subscribes its live stream, so one dashboard serves the whole chain without clutter. For illustration the seed generates **three synthetic stores** (`STORE_BLR_002`, `STORE_DEL_001`, `STORE_MUM_003` — see `data/mock/generate_extra_stores.py`), each with its own computed conversion/funnel/anomalies. These are clearly generated demo data; real stores appear in the dropdown automatically once their detection events are ingested.

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
| GET | `/stores/{id}/stream` | Server-Sent Events feed; pushes on each ingest (powers the live dashboard) |

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
