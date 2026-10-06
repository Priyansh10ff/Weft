"""CLI: python -m app.evaluation run [--dataset demo] [--k 5] [--answers] [--markdown EVALUATION.md]"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from app.evaluation.dataset import available_datasets
from app.evaluation.runner import SYSTEMS, evaluate, save_report, to_markdown


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.evaluation")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="Evaluate every system on a dataset.")
    run.add_argument("--dataset", default="demo", help="Dataset name or path to a JSON file.")
    run.add_argument("--k", type=int, default=5)
    run.add_argument("--systems", nargs="*", choices=list(SYSTEMS))
    run.add_argument("--answers", action="store_true", help="Also synthesize answers (uses Gemini if configured).")
    run.add_argument("--out", type=Path, help="Write the JSON report here (default: data/eval/latest.json).")
    run.add_argument("--markdown", type=Path, help="Also write a Markdown report (e.g. EVALUATION.md).")
    sub.add_parser("list", help="List bundled datasets.")
    args = parser.parse_args(argv)

    if args.command == "list":
        print("\n".join(available_datasets()))
        return 0

    report = evaluate(args.dataset, k=args.k, systems=args.systems, with_answers=args.answers)
    path = save_report(report, args.out)
    markdown = to_markdown(report)
    if args.markdown:
        args.markdown.write_text(markdown, encoding="utf-8")
    print(markdown)
    print(f"JSON report: {path}")
    if report.get("improvement_over_baseline"):
        print(json.dumps(report["improvement_over_baseline"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
