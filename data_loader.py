"""Load and normalize the app input files in ./data.

Handles both supported shapes:
  1. a top-level dict wrapping a list under "apps" (current files)
  2. a bare top-level list of app records

Normalized record:
  {name, category, url, description, source_file}
plus `hint` and `number` for traceability.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

DATA_DIR = Path(__file__).resolve().parent / "data"
_URL_RE = re.compile(r"^(https?://)?[A-Za-z0-9.-]+\.[A-Za-z]{2,}(/.*)?$")


def _record_list(payload: Any, source_file: str) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("apps", "data", "items", "records"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
    raise ValueError(f"{source_file}: no list found (top-level {type(payload).__name__})")


def _category(payload: Any, source_file: str) -> str:
    if isinstance(payload, dict) and isinstance(payload.get("category"), str):
        return payload["category"]
    # Fall back to the filename prefix (01-crm-and-sales.json -> crm and sales).
    stem = Path(source_file).stem
    return stem.split("-", 1)[-1].replace("-", " ")


def _url_from_hint(hint: str | None) -> str | None:
    if not hint:
        return None
    candidate = hint.strip()
    if not _URL_RE.match(candidate):
        return None
    return candidate if candidate.startswith(("http://", "https://")) else f"https://{candidate}"


def load_apps(data_dir: Path | str = DATA_DIR) -> tuple[list[dict[str, Any]], list[str]]:
    """Return (normalized apps, warnings). Order is stable by file then record order."""
    data_dir = Path(data_dir)
    files = sorted(data_dir.glob("*.json"))
    if not files:
        raise FileNotFoundError(f"no JSON files in {data_dir}")

    apps: list[dict[str, Any]] = []
    warnings: list[str] = []
    idx = 0
    for path in files:
        payload = json.loads(path.read_text(encoding="utf-8"))
        category = _category(payload, path.name)
        records = _record_list(payload, path.name)
        for raw in records:
            if not isinstance(raw, dict) or not raw.get("app"):
                warnings.append(f"{path.name}: skipped malformed record {raw!r}")
                continue
            idx += 1
            hint = raw.get("hint")
            apps.append(
                {
                    "id": f"{idx:03d}",
                    "name": str(raw["app"]).strip(),
                    "category": category,
                    "url": _url_from_hint(hint),
                    "description": raw.get("note") or raw.get("description"),
                    "source_file": path.name,
                    "hint": hint,
                    "number": raw.get("number"),
                }
            )

    counts = Counter(a["name"].casefold() for a in apps)
    for name, count in sorted(counts.items()):
        if count > 1:
            warnings.append(f"duplicate app name (case-insensitive): {name!r} x{count}")

    for app in apps:
        if app["url"] is None and app["hint"] and _URL_RE.match(str(app["hint"]).strip()):
            warnings.append(f"{app['name']}: hint looked like a URL but was not normalized")

    return apps, warnings


def main() -> None:
    parser = argparse.ArgumentParser(description="Inspect/normalize ./data")
    parser.add_argument("--json", action="store_true", help="print normalized records")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    apps, warnings = load_apps()
    print(f"loaded {len(apps)} apps from {len(list(DATA_DIR.glob('*.json')))} files")
    print("categories:", len({a['category'] for a in apps}))
    for warning in warnings:
        print("WARNING:", warning)
    if args.json:
        out = apps[: args.limit] if args.limit else apps
        print(json.dumps(out, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
