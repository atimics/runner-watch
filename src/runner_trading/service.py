from __future__ import annotations

import logging
import signal
import threading

from runner_trading.config import Policy, mode
from runner_trading.engine import Trader
from runner_trading.providers import Jupiter
from runner_web.db import init_db

LOG = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    policy, selected_mode = Policy.from_env(), mode()
    init_db()
    provider = Jupiter(policy)
    live = None
    if selected_mode == "live":
        from runner_trading.live import Live

        live = Live(policy, provider)
    trader = Trader(policy, mode=selected_mode, provider=provider, live=live)
    stopped = threading.Event()
    for name in (signal.SIGTERM, signal.SIGINT):
        signal.signal(name, lambda *_: stopped.set())
    LOG.info("Trading service started mode=%s policy=%s", selected_mode, policy.fingerprint)
    while not stopped.is_set():
        try:
            health = trader.cycle()
            LOG.info("Trading cycle state=%s", health["state"])
        except Exception as exc:
            # URLs and transaction bodies stay out of process logs.
            LOG.error("Trading cycle interrupted type=%s", type(exc).__name__)
        stopped.wait(policy.cycle_seconds)


if __name__ == "__main__":
    main()
