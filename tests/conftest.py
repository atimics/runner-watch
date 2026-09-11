from __future__ import annotations

import os
import sys
from pathlib import Path

sys.pycache_prefix = str(Path(__file__).resolve().parents[1] / ".test-cache.nosync" / "pycache")

# Starting the app lifespan in a test spawns the background workers, and several of them
# reach the network immediately and archive what they fetch into whatever database the
# current test has pointed at. Keep them off for the whole suite.
os.environ.setdefault("BACKGROUND_WORKERS_ENABLED", "0")

# A scan spawns the Telegram dispatch on a daemon thread, which writes through the same
# connection helper. Left on, a test that runs a scan could post outward and land rows in
# whichever database the next test points at.
os.environ.setdefault("TELEGRAM_RUNNER_ALERTS", "0")
os.environ.setdefault("TELEGRAM_RUNNER_REPORTS_PER_DAY", "0")
os.environ.setdefault("TELEGRAM_WEBHOOK_SECRET", "")
