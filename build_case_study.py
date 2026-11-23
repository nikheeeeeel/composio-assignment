"""Build the single-page research case study.

Reads the actual pass-1 records, verification finals/logs, and the independent
Google Sheets Research tab snapshot. Writes case-study.html with embedded data.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from schema import CLAIM_FIELDS, FIELD_DESCRIPTIONS, AppResearch

BASE = Path(__file__).resolve().parent
TEMPLATE = BASE / "case_study_template.html"
OUT = BASE / "case-study.html"

FIELD_LABELS = {
    "one_line_description": "One-line description",
    "has_public_api": "Public API",
    "api_type": "API type",
    "api_breadth": "API breadth",
    "docs_url": "Docs URL",
    "auth_methods": "Auth methods",
    "oauth_scopes_documented": "OAuth scopes documented",
    "access_model": "Access model",
    "access_notes": "Access notes",
    "official_mcp_server": "Official MCP server",
    "community_mcp_servers": "Community MCP servers",
    "composio_toolkit_exists": "Composio toolkit",
    "rate_limits": "Rate limits",
    "webhooks": "Webhooks",
    "sandbox_or_test_env": "Sandbox / test env",
    "sdk_languages": "SDK languages",
    "api_versioning_or_deprecation_notes": "Versioning / deprecation",
}

EXAMPLES = ["attio", "zendesk", "ahrefs", "salesforce", "mermaid-cli", "pitchbook", "stripe"]


def load_pass1():
    records = []
    raw = {}
    for path in sorted((BASE / "results" / "pass1").glob("*.json")):
        data = json.loads(path.read_text())
        raw[path.stem] = data
        research = AppResearch.model_validate(data)
        log_path = BASE / "results" / "logs" / f"{path.stem}.json"
        log = json.loads(log_path.read_text()) if log_path.exists() else {}
        conf = Counter(research.claim(f).confidence for f in CLAIM_FIELDS)
        no_source = [f for f in CLAIM_FIELDS if not research.claim(f).source_url]
        records.append(
            {
                "app": research.app,
                "slug": path.stem,
                "category": research.category,
                "verdict": research.buildability_verdict,
                "access": research.access_model.value,
                "auth": research.auth_methods.value or [],
                "hasApi": research.has_public_api.value,
                "apiType": research.api_type.value,
                "mcp": bool(research.official_mcp_server.value),
                "mcpUrl": research.official_mcp_server.value,
                "composio": research.composio_toolkit_exists.value,
                "outreach": research.needs_outreach,
                "blocker": research.main_blocker,
                "reason": research.verdict_reason,
                "openQuestions": research.open_questions,
                "claimsSourced": sum(1 for f in CLAIM_FIELDS if research.claim(f).source_url),
                "officialSources": sum(1 for s in research.audit.get("sources", []) if s.get("official")),
                "confidence": {k: conf.get(k, 0) for k in ("high", "medium", "low", "not_found")},
                "noSourceFields": no_source,
                "status": research.status,
                "tools": log.get("tool_call_count"),
                "durationS": log.get("duration_s"),
                "cost": (log.get("usage") or {}).get("cost_usd"),
            }
        )
    return records, raw


def example_record(slug: str, raw: dict) -> dict:
    data = raw[slug]
    research = AppResearch.model_validate(data)
    claims = []
    for field in CLAIM_FIELDS:
        claim = research.claim(field)
        claims.append(
            {
                "field": field,
                "label": FIELD_LABELS.get(field, field),
                "value": claim.value,
                "confidence": claim.confidence,
                "source_url": claim.source_url,
                "quote": claim.quote,
                "retrieved_at": claim.retrieved_at,
            }
        )
    return {
        "app": research.app,
        "slug": slug,
        "category": research.category,
        "verdict": research.buildability_verdict,
        "access": research.access_model.value,
        "auth": research.auth_methods.value or [],
        "mcp": research.official_mcp_server.value,
        "composio": research.composio_toolkit_exists.value,
        "hasApi": research.has_public_api.value,
        "apiType": research.api_type.value,
        "blocker": research.main_blocker,
        "reason": research.verdict_reason,
        "outreach": research.needs_outreach,
        "outreachReason": research.outreach_reason,
        "openQuestions": research.open_questions,
        "claims": claims,
        "sources": research.audit.get("sources", []),
        "claimsSourced": sum(1 for f in CLAIM_FIELDS if research.claim(f).source_url),
        "officialSources": sum(1 for s in research.audit.get("sources", []) if s.get("official")),
    }


def load_sheet():
    snap = json.loads((BASE / "summaries" / "google-sheets-research-data.json").read_text())
    rows = []
    for line in snap["table"]:
        p = line.split("|", 10)
        if len(p) != 11:
            continue
        rest_raw, gql_raw = p[5].strip(), p[6].strip()
        rows.append(
            {
                "app": p[0].strip(),
                "category": p[1].strip(),
                "verdict": p[2].strip(),
                "access": p[3].strip(),
                "auth": [x for x in p[4].strip().split(",") if x],
                "rest": "yes" if rest_raw.lower().startswith("yes") else ("no" if rest_raw.lower().startswith("no") else "unknown"),
                "restRaw": rest_raw,
                "graphql": "yes" if gql_raw.lower().startswith("yes") else ("no" if gql_raw.lower().startswith("no") else "unknown"),
                "graphqlRaw": gql_raw,
                "mcp": p[7].strip(),
                "confidence": p[8].strip(),
                "status": p[9].strip(),
                "blocker": p[10].strip(),
            }
        )
    return rows, snap["stats"], snap["metadata"]


def verification_record(slug: str, name: str) -> dict:
    final_path = BASE / "results" / "final" / f"{slug}.json"
    log_path = BASE / "results" / "logs" / f"{slug}.json"
    final = json.loads(final_path.read_text()) if final_path.exists() else {}
    log = json.loads(log_path.read_text()) if log_path.exists() else {}
    rounds = log.get("rounds") or []
    first = rounds[0] if rounds else {}
    last = rounds[-1] if rounds else {}
    audit = (final.get("audit") or {}).get("verification") or {}
    return {
        "app": name,
        "slug": slug,
        "finalStatus": final.get("status") or audit.get("status") or "not recorded",
        "logStatus": log.get("final_status"),
        "rounds": log.get("rounds_used") or audit.get("rounds_used"),
        "mechanicalFirst": (first.get("mechanical") or {}).get("stats"),
        "mechanicalFinal": audit.get("final_mechanical_stats") or (last.get("mechanical") or {}).get("stats"),
        "llmFirst": (first.get("llm") or {}).get("stats"),
        "llmFinal": audit.get("final_llm_stats") or (last.get("llm") or {}).get("stats"),
        "disagreements": [
            d for r in rounds for d in ((r.get("llm") or {}).get("disagreements") or [])
        ],
        "issues": [
            i for r in rounds for i in ((r.get("llm") or {}).get("issues") or [])
        ],
        "mechanicalIssues": [
            i for r in rounds for i in ((r.get("mechanical") or {}).get("issues") or [])
        ],
        "openQuestions": final.get("open_questions") or [],
        "sourcePaths": [
            f"results/final/{slug}.json",
            f"results/logs/{slug}.json",
        ],
        "sourceInconsistency": (
            "final JSON says needs_human_review; the latest saved log says error because a later rerun was interrupted. "
            "Round data in this page comes from the saved log, status comes from the final JSON."
            if slug == "ahrefs"
            else None
        ),
    }


def build_data():
    pass1, raw = load_pass1()
    sheet, sheet_stats, sheet_meta = load_sheet()
    examples = {slug: example_record(slug, raw) for slug in EXAMPLES}
    app_verification = [
        verification_record("attio", "Attio"),
        verification_record("zendesk", "Zendesk"),
        verification_record("ahrefs", "Ahrefs"),
    ]

    run_path = BASE / "results" / "logs" / "run_20261123T161600Z.json"
    run = json.loads(run_path.read_text())
    started = datetime.fromisoformat(run["started_at"])
    finished = datetime.fromisoformat(run["finished_at"])

    mapping = {
        "easy": "Buildable",
        "moderate": "Buildable with limitations",
        "hard": "Difficult / gated",
        "blocked": "Insufficient information",
    }
    sheet_by_app = {r["app"]: r for r in sheet}
    differences = []
    agreement = 0
    for r in pass1:
        s = sheet_by_app.get(r["app"])
        if not s:
            continue
        if mapping.get(r["verdict"]) == s["verdict"]:
            agreement += 1
        else:
            differences.append(
                {
                    "app": r["app"],
                    "pass1Verdict": r["verdict"],
                    "sheetVerdict": s["verdict"],
                    "pass1Access": r["access"],
                    "sheetAccess": s["access"],
                }
            )

    category_order = []
    for path in sorted((BASE / "data").glob("*.json")):
        cat = json.loads(path.read_text()).get("category")
        if cat and cat not in category_order:
            category_order.append(cat)

    data = {
        "meta": {
            "generated": "2026-11-23",
            "categoryOrder": category_order,
            "repoUrl": "https://github.com/nikheeeeeel/composio-assignment",
            "runCommand": "python main.py --pass1-only --all --concurrency 8",
            "researcherModel": "opencode/deepseek-v4.1-flash",
            "verifierModel": "opencode/gemini-3.8-flash",
            "composioVersion": "composio 0.34.0",
            "mcpVersion": "mcp 2.3.0",
            "sheetId": sheet_meta["spreadsheet_id"],
            "sheetTab": sheet_meta["tab"],
            "sheetReadVia": sheet_meta["read_via"],
            "sheetColumns": 19,
            "sheetCompactColumns": 11,
            "pass1Files": 100,
            "categoryCount": 10,
            "fieldCount": len(CLAIM_FIELDS),
            "notMeasured": [
                "Agent-callable skill counts: the pass-1 schema has no separate skills field; it records API breadth, capabilities and MCP availability only.",
                "Claim-level manual accuracy: no completed gold CSV or claim-level manual audit exists in the project (eval/ is empty).",
                "Full 100-app verification: only 3 apps entered the mechanical + LLM verification loop.",
            ],
        },
        "pass1": pass1,
        "sheet": sheet,
        "sheetStats": sheet_stats,
        "examples": examples,
        "verification": {
            "agent": app_verification,
            "independent": {
                "records": len(sheet),
                "status": sheet_stats["overall"]["status"],
                "confidence": sheet_stats["overall"]["confidence"],
                "insufficient": sheet_stats["overall"]["verdict"].get("Insufficient information", 0),
                "lowConfidence": sheet_stats["overall"]["confidence"].get("Low", 0),
                "note": (
                    "Independent research pass in Google Sheets. Its status field is not a claim-by-claim audit "
                    "of the agent's pass1 output, so it cannot produce a manual accuracy rate."
                ),
            },
            "manual": {
                "available": False,
                "goldCsv": False,
                "evalFiles": [],
                "accuracy": None,
                "note": (
                    "No claim-level manual gold set is present. eval/ is empty; no eval/gold_template.csv or "
                    "score output exists. Manual accuracy is therefore not calculated."
                ),
            },
        },
        "crossSource": {
            "agreement": agreement,
            "mismatchCount": len(differences),
            "mappingNote": (
                "Navigational mapping only: easy→Buildable, moderate→Buildable with limitations, "
                "hard→Difficult / gated, blocked→Insufficient information. This is label agreement, not accuracy."
            ),
            "differences": differences,
        },
        "run": {
            "processed": run.get("processed"),
            "drafted": run.get("drafted"),
            "skipped": run.get("skipped"),
            "errors": run.get("errors"),
            "concurrency": run.get("concurrency"),
            "startedAt": run.get("started_at"),
            "finishedAt": run.get("finished_at"),
            "wallSeconds": round((finished - started).total_seconds(), 1),
            "sumAppMinutes": round(sum(a.get("duration_s", 0) for a in run.get("apps", [])) / 60, 1),
            "costUsd": round(sum(a.get("cost_usd", 0) or 0 for a in run.get("apps", [])), 4),
            "toolCalls": sum(a.get("tool_calls", 0) or 0 for a in run.get("apps", [])),
            "maxApp": max(((a.get("duration_s", 0), a.get("app")) for a in run.get("apps", [])), default=(0, "")),
        },
        "sources": {
            "input": "data/*.json (10 category files, 100 apps)",
            "pass1": "results/pass1/*.json (100 drafts, 17 claims each)",
            "logs": "results/logs/*.json (per-app tool/LLM logs)",
            "finals": "results/final/*.json (3 apps)",
            "sheet": "summaries/google-sheets-research-data.json (100-row Research tab snapshot)",
            "code": [
                "schema.py",
                "researcher.py",
                "composio_tools.py",
                "verify_mechanical.py",
                "verifier.py",
                "pipeline.py",
                "main.py",
            ],
        },
        "manualChecks": [
            {
                "app": "Attio",
                "observation": "Its buildability assessment appears to be correct.",
            },
            {
                "app": "DealCloud",
                "observation": "The difficult/gated assessment was broadly correct.",
            },
            {
                "app": "Sherlock",
                "observation": "It is a CLI tool, so it could potentially be made agent-callable as a skill. Treating the absence of a hosted API as a complete blocker misses this possibility.",
            },
            {
                "app": "Paygent Connect (NMI Powered)",
                "observation": "I could not find Paygent-specific information. Since it appears to be a payment gateway powered by NMI, it may be possible to build through the underlying NMI platform. This is an inference, not a confirmed finding.",
            },
        ],
        "caveats": [
            "Pass1 is agent-generated and unverified at scale: 100 drafts, 0 mechanical/LLM verifications for 97 of them.",
            "Google Sheets Research tab is a separate research pass with a different taxonomy; its 99 Verified / 1 Pilot status is not a claim-level audit of pass1.",
            "Ahrefs has a source inconsistency: results/final/ahrefs.json says needs_human_review; the latest log says error after an interrupted rerun.",
            "GoHighLevel's first pass was invalidated by a Stoplight docs-host search; the repaired pass1 is used, and the old draft is preserved at results/logs/gohighlevel.pass1-bad.json.",
            "The 100-run summary is results/logs/run_20261123T161600Z.json; results/logs/run_summary.json is from a later one-app run and is not used for scale numbers.",
            "No agent-callable skills count, no manual gold CSV, and no full 100-app verification exist in the project.",
        ],
    }
    return data


def main() -> None:
    data = build_data()
    template = TEMPLATE.read_text()
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    html = template.replace("__DATA_JSON__", payload)
    OUT.write_text(html, encoding="utf-8")
    print(f"wrote {OUT} ({len(html):,} bytes)")
    # Build-time consistency checks
    p = data["pass1"]
    print("pass1", len(p), "categories", len(set(x["category"] for x in p)))
    print("verdicts", Counter(x["verdict"] for x in p))
    print("sheet", len(data["sheet"]), "differences", len(data["crossSource"]["differences"]))
    print("verification", [(v["app"], v["finalStatus"]) for v in data["verification"]["agent"]])


if __name__ == "__main__":
    main()
