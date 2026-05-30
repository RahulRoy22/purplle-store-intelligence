"""
zone_mapper.py — Maps pixel-coordinate centroids to store zone IDs.

Reads `pixel_polygon` definitions from store_layout.json and answers
"which zone does this (x, y) centroid belong to?" using Shapely
point-in-polygon geometry.

Boundary contract: a centroid exactly on a polygon edge is treated as
inside that zone (Shapely `covers()` includes boundary points, unlike
`contains()` which only covers the strict interior).
"""
from __future__ import annotations

import json
from pathlib import Path

from shapely.geometry import Point, Polygon


class ZoneMapper:
    """
    Immutable mapping of pixel polygons → zone_id strings.

    Parameters
    ----------
    zones : list[dict]
        Each element is a zone dict from store_layout.json.
        Elements lacking a ``pixel_polygon`` key are silently skipped
        for forward-compatibility with future schema additions.
    """

    def __init__(self, zones: list[dict]) -> None:
        self._zone_polygons: list[tuple[str, Polygon]] = []
        for zone in zones:
            if "pixel_polygon" not in zone:
                continue
            coords = zone["pixel_polygon"]
            if len(coords) < 3:
                continue   # degenerate polygon — skip
            self._zone_polygons.append((zone["zone_id"], Polygon(coords)))

    def pixel_to_zone(self, x: float, y: float) -> str | None:
        """
        Return the zone_id whose polygon covers (x, y), or None.

        Uses ``Polygon.covers()`` so points on polygon boundaries
        are treated as inside (inclusive boundary semantics).
        First matching zone wins when polygons overlap.
        """
        point = Point(x, y)
        for zone_id, polygon in self._zone_polygons:
            if polygon.covers(point):
                return zone_id
        return None

    @classmethod
    def from_layout(cls, layout: dict) -> "ZoneMapper":
        """Build a ZoneMapper from an already-loaded layout dict."""
        return cls(layout.get("zones", []))

    @classmethod
    def from_file(cls, path: str | Path) -> "ZoneMapper":
        """Load store_layout.json from *path* and build a ZoneMapper."""
        layout = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls.from_layout(layout)
