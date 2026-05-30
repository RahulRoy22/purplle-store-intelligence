"""
routers/stores.py — Analytics endpoints for a single store.

  GET /stores/{store_id}/metrics   — KPI dashboard snapshot
  GET /stores/{store_id}/funnel    — Visitor conversion funnel
  GET /stores/{store_id}/heatmap   — Zone heat map (normalised 0–100)
  GET /stores/{store_id}/anomalies — Rule-based anomaly alerts
"""
from fastapi import APIRouter

from core.config import get_settings
from db.analytics import get_anomalies, get_funnel, get_heatmap, get_metrics
from models.stores import AnomalyReport, FunnelReport, HeatmapReport, StoreMetrics

router = APIRouter(prefix="/stores", tags=["analytics"])


@router.get("/{store_id}/metrics", response_model=StoreMetrics, summary="Store KPI snapshot")
async def store_metrics(store_id: str) -> StoreMetrics:
    data = await get_metrics(get_settings().db_path, store_id)
    return StoreMetrics(**data)


@router.get("/{store_id}/funnel", response_model=FunnelReport, summary="Visitor conversion funnel")
async def store_funnel(store_id: str) -> FunnelReport:
    data = await get_funnel(get_settings().db_path, store_id)
    return FunnelReport(**data)


@router.get("/{store_id}/heatmap", response_model=HeatmapReport, summary="Zone heat map")
async def store_heatmap(store_id: str) -> HeatmapReport:
    data = await get_heatmap(get_settings().db_path, store_id)
    return HeatmapReport(**data)


@router.get(
    "/{store_id}/anomalies", response_model=AnomalyReport, summary="Rule-based anomaly alerts"
)
async def store_anomalies(store_id: str) -> AnomalyReport:
    data = await get_anomalies(get_settings().db_path, store_id)
    return AnomalyReport(**data)
