#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from coding.crossref_abs_journal_monitor import DEFAULT_APP_NAME, DEFAULT_MAILTO, load_journals


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "config" / "crossref_abs_journals.json"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Regenerate the Crossref ABS journal config from an .xlsx file.")
    parser.add_argument("input", type=Path, help="Path to an ABS/AJG journal-list .xlsx file.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--mailto", default=DEFAULT_MAILTO)
    parser.add_argument("--app-name", default=DEFAULT_APP_NAME)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    journals = load_journals(args.input)
    data = {
        "source_note": f"ABS/AJG 2024 rows generated from {args.input.name}.",
        "crossref_mailto": args.mailto,
        "app_name": args.app_name,
        "journals": [
            {
                "name": journal.title,
                "issn": journal.issn,
                "publisher": journal.publisher,
                "field": journal.field,
                "ajg_2024": journal.ajg2024,
                "ajg_2021": journal.ajg2021,
                "ajg_2018": journal.ajg2018,
            }
            for journal in journals
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {len(journals)} journals to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
