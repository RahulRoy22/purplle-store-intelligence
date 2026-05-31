#!/usr/bin/env python3
"""
Generates data/generated/store_layout.json

Schema:
{
  "store_id": str,
  "store_name": str,
  "total_area_sqft": int,
  "zones": [
    {
      "zone_id": str,
      "name": str,
      "category": "ENTRY" | "FLOOR" | "BILLING",
      "area_sqft": int,
      "cameras": [str],
      "adjacent_zones": [str]
    }
  ]
}
"""
import json
import pathlib

OUTPUT = pathlib.Path(__file__).parent.parent / "generated" / "store_layout.json"
OUTPUT.parent.mkdir(parents=True, exist_ok=True)

layout = {
    "store_id": "STORE_BLR_002",
    "store_name": "Purplle Flagship — Koramangala",
    "total_area_sqft": 3200,
    "zones": [
        {
            "zone_id": "zone_entry",
            "name": "Store Entry / Exit",
            "category": "ENTRY",
            "area_sqft": 200,
            "cameras": ["cam_entry"],
            "adjacent_zones": ["zone_fragrance", "zone_skincare"],
        },
        {
            "zone_id": "zone_skincare",
            "name": "Skincare Aisle",
            "category": "FLOOR",
            "area_sqft": 600,
            "cameras": ["cam_floor_01"],
            "adjacent_zones": ["zone_entry", "zone_makeup", "zone_haircare"],
        },
        {
            "zone_id": "zone_makeup",
            "name": "Makeup & Colour Cosmetics",
            "category": "FLOOR",
            "area_sqft": 700,
            "cameras": ["cam_floor_02"],
            "adjacent_zones": ["zone_skincare", "zone_fragrance", "zone_billing"],
        },
        {
            "zone_id": "zone_haircare",
            "name": "Haircare & Styling",
            "category": "FLOOR",
            "area_sqft": 500,
            "cameras": ["cam_floor_03"],
            "adjacent_zones": ["zone_skincare", "zone_billing"],
        },
        {
            "zone_id": "zone_fragrance",
            "name": "Fragrance & Wellness",
            "category": "FLOOR",
            "area_sqft": 400,
            "cameras": ["cam_floor_04"],
            "adjacent_zones": ["zone_entry", "zone_makeup"],
        },
        {
            "zone_id": "zone_billing",
            "name": "Billing Counter",
            "category": "BILLING",
            "area_sqft": 150,
            "cameras": ["cam_billing"],
            "adjacent_zones": ["zone_makeup", "zone_haircare"],
        },
    ],
}

OUTPUT.write_text(json.dumps(layout, indent=2))
print(f"[OK] Written {OUTPUT}")
