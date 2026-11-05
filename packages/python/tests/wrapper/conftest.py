import sys
from pathlib import Path

# The shared helpers (`helpers.py`) live one level up.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
