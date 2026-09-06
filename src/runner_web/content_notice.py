from __future__ import annotations

import argparse
import json
from collections.abc import Sequence

from runner_web.content_notices import NOTICE_LABELS, record_content_notice


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Append a public correction or relationship disclosure to a report or comment "
            "using configured DATABASE_URL or DATABASE_PATH access. The original content "
            "and earlier notices stay in the record. Report notices follow report visibility. "
            "Each notice includes its time. Prints a JSON receipt."
        ),
        epilog=(
            "Example: python -m runner_web.content_notice report PUBLIC_ID "
            '--kind correction --text "Revenue was $2 million." '
            '--reason "The report used the wrong unit." '
            "For price outcome corrections use python -m runner_web.flash_correction. "
            "Disclosure text should name the party and describe the payment, holdings, "
            "or relationship."
        ),
    )
    parser.add_argument("subject", choices=("report", "comment"))
    parser.add_argument("target_id", help="Report public ID or comment ID")
    parser.add_argument("--kind", required=True, choices=tuple(NOTICE_LABELS))
    parser.add_argument("--text", required=True, help="Corrected text or relationship details")
    parser.add_argument("--reason", help="Required public reason for a correction")
    args = parser.parse_args(argv)
    try:
        receipt = record_content_notice(
            args.subject, args.target_id, kind=args.kind, text=args.text, reason=args.reason
        )
    except (ValueError, KeyError) as exc:
        parser.error(str(exc.args[0]))
    print(json.dumps(receipt, sort_keys=True, allow_nan=False))


if __name__ == "__main__":
    main()
