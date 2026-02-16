import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

# Allow running tests without an editable install.
if SRC.exists():
    p = str(SRC)
    if p not in sys.path:
        sys.path.insert(0, p)
