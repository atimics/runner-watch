from __future__ import annotations

import sys
from pathlib import Path

# This repo may live in a cloud-synced folder. Keep pytest's bytecode cache in
# a non-synced tree so imports never wait for offloaded __pycache__ files.
sys.pycache_prefix = str(
    Path(__file__).resolve().parents[1] / ".test-cache.nosync" / "pycache"
)
