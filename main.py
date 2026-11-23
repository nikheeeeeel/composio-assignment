"""CLI entry point.

Examples:
  python main.py --limit 3
  python main.py --app "Linear"
  python main.py --category "Finance"
  python main.py --all
  python main.py --all --force --concurrency 6
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone

from composio_tools import ComposioRuntime
from config import settings
from data_loader import load_apps
from pipeline import Pipeline, utcnow, write_json


def select_apps(apps, *, app_name: str | None, category: str | None, all_apps: bool, limit: int | None):
    selected = apps
    if app_name:
        needle = app_name.casefold().strip()
        exact = [a for a in selected if a["name"].casefold() == needle]
        if exact:
            selected = exact
        else:
            matches = [a for a in selected if needle in a["name"].casefold()]
            if not matches:
                raise SystemExit(f"No app matched --app {app_name!r}")
            if len(matches) > 1:
                names = ", ".join(a["name"] for a in matches[:10])
                raise SystemExit(f"--app {app_name!r} is ambiguous; matches: {names}")
            selected = matches
    if category:
        needle = category.casefold().strip()
        matches = [a for a in selected if needle in a["category"].casefold()]
        if not matches:
            categories = sorted({a["category"] for a in apps})
            raise SystemExit(f"No category matched {category!r}. Available: {categories}")
        selected = matches
    if not all_apps and not app_name and not category:
        selected = selected[: (limit or 3)]
    elif limit is not None:
        selected = selected[:limit]
    return selected


def main() -> None:
    parser = argparse.ArgumentParser(description="Research 100 apps for agent-toolkit buildability")
    parser.add_argument("--limit", type=int, default=None, help="process at most N apps")
    parser.add_argument("--app", type=str, default=None, help="exact or unique substring app name")
    parser.add_argument("--category", type=str, default=None, help="case-insensitive category substring")
    parser.add_argument("--all", action="store_true", help="process all selected apps (default: first 3)")
    parser.add_argument("--force", action="store_true", help="re-run even when a final result exists")
    parser.add_argument("--concurrency", type=int, default=None)
    parser.add_argument("--tool-concurrency", type=int, default=None)
    parser.add_argument("--max-revision-rounds", type=int, default=None)
    parser.add_argument("--researcher-model", type=str, default=None)
    parser.add_argument("--verifier-model", type=str, default=None)
    parser.add_argument(
        "--fast",
        action="store_true",
        help="throughput profile: concurrency 8, 8 pages, smaller evidence, 1 revision round",
    )
    parser.add_argument(
        "--pass1-only",
        action="store_true",
        help="research only: save results/pass1 and logs, skip mechanical + LLM verification",
    )
    parser.add_argument("--dry-run", action="store_true", help="print the selection and exit")
    args = parser.parse_args()

    if args.fast:
        settings.concurrency = 8
        settings.tool_concurrency = 6
        settings.max_revision_rounds = 1
        settings.max_pages_per_app = 8
        settings.page_char_budget = 4200
        settings.evidence_char_budget = 38000
    if args.concurrency:
        settings.concurrency = args.concurrency
    if args.tool_concurrency:
        settings.tool_concurrency = args.tool_concurrency
    if args.max_revision_rounds is not None:
        settings.max_revision_rounds = args.max_revision_rounds
    if args.researcher_model:
        settings.researcher_model = args.researcher_model
    if args.verifier_model:
        settings.verifier_model = args.verifier_model

    apps, warnings = load_apps(settings.data_dir)
    for warning in warnings:
        print("WARNING:", warning)
    selected = select_apps(
        apps,
        app_name=args.app,
        category=args.category,
        all_apps=args.all,
        limit=args.limit,
    )
    print(f"selected {len(selected)} of {len(apps)} apps")
    for app in selected:
        print(f"  - {app['name']} ({app['category']})")
    if args.dry_run:
        return

    settings.validate()
    settings.ensure_dirs()
    print(f"researcher={settings.researcher_model}")
    print(f"verifier={settings.verifier_model}")
    print(
        f"concurrency={settings.concurrency} tool_concurrency={settings.tool_concurrency} "
        f"max_revision_rounds={settings.max_revision_rounds} force={args.force} fast={args.fast}"
    )
    print(f"results={settings.results_dir}")

    async def run() -> dict:
        runtime = ComposioRuntime(settings)
        await runtime.start()
        pipeline = Pipeline(settings, runtime, force=args.force, pass1_only=args.pass1_only)
        await pipeline.run(selected)
        summary = pipeline.summary()
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        write_json(settings.logs_dir / f"run_{timestamp}.json", summary)
        write_json(settings.logs_dir / "run_summary.json", summary)
        return summary

    summary = asyncio.run(run())
    print("\n=== RUN SUMMARY ===")
    print(
        f"processed={summary['processed']} drafted={summary['drafted']} "
        f"verified={summary['verified']} needs_human_review={summary['needs_human_review']} "
        f"errors={summary['errors']} skipped={summary['skipped']}"
    )
    for app in summary["apps"]:
        print(
            f"  {app['app']:<28} {app['status']:<20} rounds={app['rounds']} "
            f"tools={app['tool_calls']} {app['duration_s']:.0f}s ${app['cost_usd']:.4f}"
        )
    if summary["errors"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
