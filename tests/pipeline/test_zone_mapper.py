"""
Phase 4 Tests — Task 1: ZoneMapper  (RED phase)

ZoneMapper reads pixel-coordinate polygon definitions from store_layout.json
and answers: "which zone does this (x, y) centroid belong to?"

Contract:
  - Point clearly inside a polygon  → zone_id string
  - Point outside every polygon     → None
  - Point on polygon boundary       → zone_id (inclusive, Shapely default)
  - Zone missing pixel_polygon key  → silently skipped (schema forward-compat)
  - from_layout() / from_file()     → factory parity with __init__

Prompt used to design adversarial fixtures (AI-assisted):
  "Design pytest fixtures for a pixel-coordinate zone mapper. Include:
   adjacent non-overlapping zones, a point on a shared edge, a zone
   missing the pixel_polygon key, and fractional coordinates."
"""
import json
import pytest
from pathlib import Path

from zone_mapper import ZoneMapper


# ---------------------------------------------------------------------------
# Shared test layout
# ---------------------------------------------------------------------------

# Two non-overlapping zones:
#   zone_entry  : square (0,0)–(100,100)
#   zone_floor  : square (200,0)–(400,200)
_ZONES = [
    {
        "zone_id": "zone_entry",
        "pixel_polygon": [[0, 0], [100, 0], [100, 100], [0, 100]],
    },
    {
        "zone_id": "zone_floor",
        "pixel_polygon": [[200, 0], [400, 0], [400, 200], [200, 200]],
    },
]

_LAYOUT = {
    "store_id": "store_test",
    "zones": _ZONES,
}


@pytest.fixture
def mapper():
    return ZoneMapper(_ZONES)


# ---------------------------------------------------------------------------
# Core mapping behaviour
# ---------------------------------------------------------------------------

class TestPixelToZone:

    def test_point_inside_first_zone(self, mapper):
        """Centroid clearly inside zone_entry → 'zone_entry'."""
        assert mapper.pixel_to_zone(50, 50) == "zone_entry"

    def test_point_inside_second_zone(self, mapper):
        """Centroid clearly inside zone_floor → 'zone_floor'."""
        assert mapper.pixel_to_zone(300, 100) == "zone_floor"

    def test_point_outside_all_zones_returns_none(self, mapper):
        """Centroid in the aisle between zones → None."""
        assert mapper.pixel_to_zone(150, 50) is None

    def test_point_far_outside_returns_none(self, mapper):
        """Centroid far below the store map → None."""
        assert mapper.pixel_to_zone(500, 500) is None

    def test_point_on_polygon_boundary_is_inside(self, mapper):
        """
        A centroid sitting exactly on an edge counts as inside that zone.
        Shapely's `contains` is True for boundary points with `covers()`;
        ZoneMapper must use an inclusive check.
        """
        # (0, 50) lies on the left edge of zone_entry
        result = mapper.pixel_to_zone(0, 50)
        assert result == "zone_entry", (
            f"Boundary point must be included in zone, got {result!r}"
        )

    def test_fractional_coordinates_work(self, mapper):
        """Sub-pixel centroids (floats) must be handled correctly."""
        assert mapper.pixel_to_zone(49.7, 49.9) == "zone_entry"
        assert mapper.pixel_to_zone(150.5, 50.1) is None

    def test_origin_corner_is_inside_zone_entry(self, mapper):
        """Corner vertex of a polygon is inside that zone."""
        assert mapper.pixel_to_zone(0, 0) == "zone_entry"

    def test_shared_boundary_between_adjacent_zones(self):
        """
        When two zones share an edge (touching, not overlapping),
        a point on that shared edge belongs to exactly one zone.
        The test just checks: no crash, returns a str or None.
        """
        touching_zones = [
            {
                "zone_id": "zone_a",
                "pixel_polygon": [[0, 0], [100, 0], [100, 100], [0, 100]],
            },
            {
                "zone_id": "zone_b",
                "pixel_polygon": [[100, 0], [200, 0], [200, 100], [100, 100]],
            },
        ]
        m = ZoneMapper(touching_zones)
        result = m.pixel_to_zone(100, 50)  # on the shared edge x=100
        assert result is None or isinstance(result, str)


# ---------------------------------------------------------------------------
# Robustness / schema forward-compatibility
# ---------------------------------------------------------------------------

class TestEdgeCases:

    def test_zone_without_pixel_polygon_is_skipped(self):
        """
        A zone missing the pixel_polygon key must not crash the mapper —
        it is silently ignored (forward-compatibility with future schema fields).
        """
        zones_mixed = [
            {"zone_id": "zone_no_polygon"},          # missing pixel_polygon
            {
                "zone_id": "zone_with_polygon",
                "pixel_polygon": [[0, 0], [50, 0], [50, 50], [0, 50]],
            },
        ]
        m = ZoneMapper(zones_mixed)
        assert m.pixel_to_zone(25, 25) == "zone_with_polygon"
        assert m.pixel_to_zone(100, 100) is None

    def test_empty_zones_list_always_returns_none(self):
        """ZoneMapper with no zones should not crash; all lookups return None."""
        m = ZoneMapper([])
        assert m.pixel_to_zone(0, 0) is None
        assert m.pixel_to_zone(9999, 9999) is None

    def test_all_zones_missing_pixel_polygon_returns_none(self):
        """When every zone lacks pixel_polygon, all lookups return None."""
        m = ZoneMapper([{"zone_id": "z"}, {"zone_id": "z2"}])
        assert m.pixel_to_zone(50, 50) is None


# ---------------------------------------------------------------------------
# Factory methods
# ---------------------------------------------------------------------------

class TestFactories:

    def test_from_layout_matches_direct_construction(self):
        """from_layout(layout_dict) must produce identical behaviour to ZoneMapper(zones)."""
        m_direct = ZoneMapper(_ZONES)
        m_factory = ZoneMapper.from_layout(_LAYOUT)

        assert m_direct.pixel_to_zone(50, 50) == m_factory.pixel_to_zone(50, 50)
        assert m_direct.pixel_to_zone(300, 100) == m_factory.pixel_to_zone(300, 100)
        assert m_direct.pixel_to_zone(150, 50) == m_factory.pixel_to_zone(150, 50)

    def test_from_file_loads_and_maps_correctly(self, tmp_path):
        """from_file(path) reads JSON and maps points identically."""
        layout_file = tmp_path / "store_layout.json"
        layout_file.write_text(json.dumps(_LAYOUT))

        m = ZoneMapper.from_file(layout_file)
        assert m.pixel_to_zone(50, 50) == "zone_entry"
        assert m.pixel_to_zone(300, 100) == "zone_floor"
        assert m.pixel_to_zone(150, 50) is None

    def test_from_file_accepts_string_path(self, tmp_path):
        """from_file should accept both str and Path objects."""
        layout_file = tmp_path / "layout.json"
        layout_file.write_text(json.dumps(_LAYOUT))

        m = ZoneMapper.from_file(str(layout_file))
        assert m.pixel_to_zone(50, 50) == "zone_entry"
