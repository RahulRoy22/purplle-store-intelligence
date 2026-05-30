from __future__ import annotations

from pydantic import BaseModel


class StoreMetrics(BaseModel):
    store_id: str
    unique_visitors: int
    conversion_rate: float
    avg_dwell_ms: float | None
    billing_abandonment_rate: float | None
    checked_at: str


class FunnelStage(BaseModel):
    stage: str
    visitors: int
    pct_of_entry: float


class FunnelReport(BaseModel):
    store_id: str
    stages: list[FunnelStage]
    checked_at: str


class HeatZone(BaseModel):
    zone_id: str
    visit_count: int
    avg_dwell_ms: float | None
    heat_score: float  # normalised 0–100


class HeatmapReport(BaseModel):
    store_id: str
    zones: list[HeatZone]
    checked_at: str


class Anomaly(BaseModel):
    type: str
    severity: str          # INFO | WARN | CRITICAL
    zone_id: str | None = None
    value: float | None = None
    message: str


class AnomalyReport(BaseModel):
    store_id: str
    anomalies: list[Anomaly]
    checked_at: str
