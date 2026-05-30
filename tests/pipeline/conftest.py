"""
conftest.py for tests/pipeline/

Adds services/pipeline AND services/api to sys.path so pipeline modules
and the shared EventIn Pydantic schema are importable without a PYTHONPATH
override — consistent with how tests/conftest.py handles services/api via
pytest.ini's pythonpath (or explicit PYTHONPATH).
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]

_PIPELINE_SRC = _ROOT / "services" / "pipeline"
if str(_PIPELINE_SRC) not in sys.path:
    sys.path.insert(0, str(_PIPELINE_SRC))

_API_SRC = _ROOT / "services" / "api"
if str(_API_SRC) not in sys.path:
    sys.path.insert(0, str(_API_SRC))
