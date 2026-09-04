from __future__ import annotations

import sys
from pathlib import Path

sys.pycache_prefix = str(Path(__file__).resolve().parents[1] / ".test-cache.nosync" / "pycache")
