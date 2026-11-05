import sys
from pathlib import Path

# The shared helpers (`helpers.py`) live in tests/, and the line helpers (`lines.py`) in tests/wrapper/.
_TESTS = Path(__file__).resolve().parent.parent
for _path in (_TESTS / "wrapper", _TESTS):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))
