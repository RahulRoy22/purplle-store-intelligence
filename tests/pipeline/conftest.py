"""
conftest.py for tests/pipeline/

Adds services/pipeline to sys.path so `from zone_mapper import ZoneMapper`
works without a PYTHONPATH override — consistent with how tests/conftest.py
handles services/api via pytest.ini's pythonpath (or explicit PYTHONPATH).
"""
import sys
from pathlib import Path

_PIPELINE_SRC = Path(__file__).resolve().parents[2] / "services" / "pipeline"
if str(_PIPELINE_SRC) not in sys.path:
    sys.path.insert(0, str(_PIPELINE_SRC))
