#!/usr/bin/env bash
set -e
echo "[seed] Generating mock data..."
python /data/mock/generate_store_layout.py
python /data/mock/generate_pos_transactions.py
python /data/mock/generate_sample_events.py
echo "[seed] All mock data generated."
