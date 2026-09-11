from __future__ import annotations

import os
import sys
from pathlib import Path

sys.pycache_prefix = str(Path(__file__).resolve().parents[1] / ".test-cache.nosync" / "pycache")

# Starting the app lifespan in a test spawns the background workers, and several of them
# reach the network immediately and archive what they fetch into whatever database the
# current test has pointed at. Keep them off for the whole suite.
os.environ.setdefault("BACKGROUND_WORKERS_ENABLED", "0")
