# PROMPT: Write a conftest.py for tests/pipeline/ that adds both
#   services/pipeline and services/api to sys.path so pipeline modules and the
#   shared EventIn Pydantic model are importable without setting PYTHONPATH
#   externally. Must not interfere with the parent conftest.py in tests/.
#
# CHANGES MADE: Used Path(__file__).resolve().parents[2] to anchor the root
#   path portably regardless of working directory. Added duplicate-path guard
#   (if str(...) not in sys.path) so repeated imports don't accumulate paths.
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
