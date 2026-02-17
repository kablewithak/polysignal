# api/index.py
import os
import sys

# Make /src importable so `import index` works (src/index.py)
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from index import app  # src/index.py must define `app`
