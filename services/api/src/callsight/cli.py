"""Command line interface: `python -m callsight analyze call.txt`."""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys

from .config import Settings
from .models import CallReport, format_timestamp
from .service import analyze_text


def format_report(report: CallReport) -> str:
    share = ", ".join(f"{name} {value:.0%}" for name, value in report.talk_share.items())
    lines = [
        f"Call {report.call_id}: {len(report.speakers)} speakers, ~{format_timestamp(report.duration_s)}",
        f"Talk share: {share}",
        "",
        f"Summary: {report.summary}",
        "",
        "Insights",
    ]
    if not report.insights:
        lines.append("  (none)")
    for insight in report.insights:
        lines.append(f"  [{insight.severity.upper()}] {insight.title}")
        if insight.detail:
            lines.append(f"      {insight.detail}")
        for ev in insight.evidence:
            lines.append(f"      [{format_timestamp(ev.start_s)}] {ev.speaker}: \"{ev.quote}\"")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="callsight", description="Open-source conversation intelligence")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("analyze", help="analyze a text transcript ('-' reads stdin)")
    p.add_argument("file")
    p.add_argument("--call-id", default="adhoc")
    p.add_argument("--competitor", action="append", default=[], help="competitor name to watch for (repeatable)")
    p.add_argument("--rep", action="append", default=[], help="speaker name of the sales rep (repeatable)")
    p.add_argument("--llm", choices=["none", "ollama"], help="override CALLSIGHT_LLM_PROVIDER")
    p.add_argument("--json", action="store_true", help="print the report as JSON")

    e = sub.add_parser("eval", help="score analyzers against labelled transcripts (see evals/calls)")
    e.add_argument("directory")
    e.add_argument("--analyzers", help="comma-separated analyzer names (default: CALLSIGHT_ANALYZERS or built-ins)")
    e.add_argument("--llm", choices=["none", "ollama"], help="override CALLSIGHT_LLM_PROVIDER")
    e.add_argument("-v", "--verbose", action="store_true", help="list every false positive / negative")
    args = parser.parse_args(argv)

    if args.command == "eval":
        from pathlib import Path

        from .evals import evaluate, format_scores, load_dir

        settings = Settings.from_env()
        if args.llm:
            settings = dataclasses.replace(settings, llm_provider=args.llm)
        if args.analyzers:
            settings = dataclasses.replace(settings, analyzers=tuple(a.strip() for a in args.analyzers.split(",")))
        print(format_scores(evaluate(load_dir(Path(args.directory)), settings), verbose=args.verbose))
        return 0

    settings = Settings.from_env()
    overrides: dict[str, object] = {}
    if args.llm:
        overrides["llm_provider"] = args.llm
    if args.rep:
        overrides["rep_names"] = tuple(args.rep)
    if overrides:
        settings = dataclasses.replace(settings, **overrides)

    text = sys.stdin.read() if args.file == "-" else open(args.file, encoding="utf-8").read()
    try:
        report = analyze_text(text, call_id=args.call_id, settings=settings, extra_competitors=args.competitor)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(json.dumps(report.to_dict(), indent=2) if args.json else format_report(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
