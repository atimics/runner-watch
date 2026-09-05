from __future__ import annotations

import argparse
import json
from collections.abc import Sequence


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Correct a Flash forecast outcome using the configured DATABASE_URL "
            "or DATABASE_PATH. The previous result stays in the event history. "
            "The correction reason appears on the report. Prints a JSON receipt."
        ),
        epilog=(
            "Example: python -m runner_web.flash_correction FORECAST_ID "
            "--end-price 99.0 --observed-at 2026-08-25T19:55:00Z "
            '--reason "The closing bar was corrected."'
        ),
    )
    parser.add_argument("forecast_id", help="ID from flash_forecasts")
    parser.add_argument(
        "--reason",
        required=True,
        help="Public reason for the correction; stored as up to 500 characters",
    )
    outcome = parser.add_mutually_exclusive_group(required=True)
    outcome.add_argument("--end-price", type=float, help="Correct closing price, greater than zero")
    outcome.add_argument("--void", action="store_true", help="Mark the outcome as void")
    parser.add_argument(
        "--observed-at",
        help="Closing price time in ISO 8601 format; a time without an offset uses UTC",
    )
    args = parser.parse_args(argv)
    if args.end_price is not None and args.observed_at is None:
        parser.error("--end-price requires --observed-at")
    if args.void and args.observed_at is not None:
        parser.error("--observed-at requires --end-price")

    from .flash_evaluations import correct_flash_outcome

    try:
        receipt = correct_flash_outcome(
            args.forecast_id,
            reason=args.reason,
            end_price=args.end_price,
            observed_at=args.observed_at,
            void=args.void,
        )
    except (ValueError, KeyError) as exc:
        parser.error(str(exc.args[0]))
    print(json.dumps(receipt, sort_keys=True))


if __name__ == "__main__":
    main()
