"""Research -> mechanical verify -> LLM verify -> revise (max 2) -> final."""

from __future__ import annotations

import asyncio
import json
import re
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from composio_tools import ComposioRuntime, ResearchTools, ToolBudget
from config import Settings
from llm import LLMClient
from researcher import ResearchAgent, ResearchError, ResearchRun
from schema import AppResearch
from verifier import Verifier
from verify_mechanical import PageFetcher, verify_mechanical


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "app"


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _usage(llm_calls: list[dict[str, Any]]) -> dict[str, Any]:
    tokens = {"input": 0, "output": 0, "reasoning": 0}
    cost = 0.0
    for call in llm_calls:
        for key in tokens:
            tokens[key] += int((call.get("tokens") or {}).get(key) or 0)
        cost += float(call.get("cost_usd") or 0)
    return {"tokens": tokens, "cost_usd": round(cost, 8), "llm_calls": len(llm_calls)}


def _dedupe_issues(issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen = set()
    out = []
    for issue in issues:
        key = (issue.get("field"), issue.get("problem"))
        if key in seen:
            continue
        seen.add(key)
        out.append(issue)
    return out


@dataclass
class Outcome:
    app: str
    category: str
    slug: str
    status: str
    rounds: int = 0
    duration_s: float = 0
    tool_calls: int = 0
    cost_usd: float = 0
    error: str | None = None
    skipped: bool = False


@dataclass
class Pipeline:
    settings: Settings
    runtime: ComposioRuntime
    force: bool = False
    pass1_only: bool = False
    only_slugs: set[str] | None = None
    outcomes: list[Outcome] = field(default_factory=list)
    started_at: str = field(default_factory=utcnow)

    async def run(self, apps: list[dict[str, Any]]) -> list[Outcome]:
        semaphore = asyncio.Semaphore(max(1, self.settings.concurrency))
        total = len(apps)
        completed = 0
        lock = asyncio.Lock()

        async def wrapped(app: dict[str, Any], index: int) -> Outcome:
            nonlocal completed
            async with semaphore:
                outcome = await self.process_app(app)
            async with lock:
                completed += 1
                icon = "SKIP" if outcome.skipped else outcome.status.upper()
                print(
                    f"[{completed}/{total}] {outcome.app}: {icon} "
                    f"rounds={outcome.rounds} tools={outcome.tool_calls} "
                    f"{outcome.duration_s:.0f}s ${outcome.cost_usd:.4f}",
                    flush=True,
                )
                self.outcomes.append(outcome)
            return outcome

        await asyncio.gather(*(wrapped(app, i) for i, app in enumerate(apps, start=1)))
        return sorted(self.outcomes, key=lambda o: o.app.lower())

    async def process_app(self, app: dict[str, Any]) -> Outcome:
        started = time.monotonic()
        slug = slugify(app["name"])
        pass1_path = self.settings.pass1_dir / f"{slug}.json"
        final_path = self.settings.final_dir / f"{slug}.json"
        log_path = self.settings.logs_dir / f"{slug}.json"

        skip_path = pass1_path if self.pass1_only else final_path
        if skip_path.exists() and not self.force:
            skipped_status = _status_of(skip_path) or ("drafted" if self.pass1_only else "unknown")
            return Outcome(
                app=app["name"], category=app["category"], slug=slug,
                status=skipped_status,
                skipped=True,
            )

        log: dict[str, Any] = {
            "app": app["name"],
            "category": app["category"],
            "slug": slug,
            "started_at": utcnow(),
            "researcher_model": self.settings.researcher_model,
            "verifier_model": self.settings.verifier_model,
            "rounds": [],
        }
        run: ResearchRun | None = None
        mech_tools: ResearchTools | None = None
        research: AppResearch | None = None
        try:
            researcher_llm = LLMClient(self.settings)
            verifier_llm = LLMClient(self.settings)
            researcher = ResearchAgent(self.settings, self.runtime, researcher_llm)
            verifier = Verifier(self.settings, self.runtime, verifier_llm)

            run = await researcher.start_run(app)
            log["mcp_used"] = run.tools.mcp_active
            log["mcp_error"] = run.tools.mcp_error
            research = await researcher.research(run)
            research.status = "drafted"

            if pass1_path.exists():
                log["pass1_existing"] = True
            else:
                write_json(pass1_path, research.model_dump(mode="json"))
                log["pass1_written"] = True

            if self.pass1_only:
                usage = _usage(run.llm_calls)
                log.update(
                    {
                        "finished_at": utcnow(),
                        "duration_s": round(time.monotonic() - started, 2),
                        "final_status": "drafted",
                        "pass1_only": True,
                        "rounds_used": 0,
                        "pass1_path": str(pass1_path.relative_to(self.settings.base_dir)),
                        "tool_calls": run.tools.tool_log(),
                        "tool_call_count": len(run.tools.calls),
                        "gaps": run.gaps,
                        "sources": [d.log() for d in run.docs],
                        "llm_calls": run.llm_calls,
                        "usage": usage,
                    }
                )
                write_json(log_path, log)
                return Outcome(
                    app=app["name"],
                    category=app["category"],
                    slug=slug,
                    status="drafted",
                    rounds=0,
                    duration_s=log["duration_s"],
                    tool_calls=len(run.tools.calls),
                    cost_usd=usage["cost_usd"],
                )

            round_number = 1
            needs_human_reason: str | None = None
            # One page cache + one Exa fallback transport for all rounds of this app.
            mech_tools = ResearchTools(self.runtime, run.handle, self.settings)
            mech_tools.budget = ToolBudget(20)
            await mech_tools.start()
            page_fetcher = PageFetcher(self.settings, mech_tools)
            while True:
                mech = await verify_mechanical(
                    research,
                    app,
                    self.settings,
                    self.runtime,
                    tools=None,
                    page_fetcher=page_fetcher,
                )

                try:
                    llm_report = await verifier.verify(
                        research, app, mech, run, round_number=round_number
                    )
                except Exception as exc:
                    llm_report = {
                        "passed": False,
                        "per_field": {},
                        "disagreements": [],
                        "issues": [
                            {
                                "field": "verification",
                                "problem": f"LLM verifier failed: {type(exc).__name__}: {exc}",
                                "severity": "critical",
                                "suggested_fix": "Inspect the verifier error and re-run; do not mark verified.",
                            }
                        ],
                        "stats": {},
                        "llm_calls": [],
                    }
                    log["verifier_error"] = f"{type(exc).__name__}: {exc}"

                all_issues = _dedupe_issues((mech.get("issues") or []) + (llm_report.get("issues") or []))
                passed = bool(mech.get("passed")) and bool(llm_report.get("passed"))
                log["rounds"].append(
                    {
                        "round": round_number,
                        "mechanical": {
                            "passed": mech.get("passed"),
                            "stats": mech.get("stats"),
                            "issues": mech.get("issues"),
                            "claims": mech.get("claims"),
                            "pages": mech.get("pages"),
                            "github": mech.get("github"),
                        },
                        "llm": {
                            "passed": llm_report.get("passed"),
                            "stats": llm_report.get("stats"),
                            "issues": llm_report.get("issues"),
                            "disagreements": llm_report.get("disagreements"),
                            "per_field": llm_report.get("per_field"),
                            "judgments": llm_report.get("judgments"),
                            "rederived": llm_report.get("rederived"),
                            "llm_calls": llm_report.get("llm_calls"),
                        },
                        "passed": passed,
                        "issue_count": len(all_issues),
                    }
                )
                if passed:
                    research.status = "verified"
                    break

                critical = [i for i in all_issues if i.get("severity") == "critical"]
                if round_number > self.settings.max_revision_rounds:
                    research.status = "needs_human_review"
                    needs_human_reason = (
                        f"Still failing after {round_number} verification rounds; "
                        f"{len(critical)} critical issue(s) remain."
                    )
                    break
                research = await researcher.revise(
                    run, research, all_issues[:15], round_number=round_number
                )
                research.status = "drafted"
                round_number += 1

            research.status = research.status or "needs_human_review"
            if research.status == "needs_human_review":
                remaining = _dedupe_issues((log["rounds"][-1]["mechanical"].get("issues") or []) + (log["rounds"][-1]["llm"].get("issues") or []))
                for issue in remaining[:8]:
                    text = f"UNRESOLVED [{issue.get('field')}]: {issue.get('problem')}"
                    if text not in research.open_questions:
                        research.open_questions.append(text)
                research.audit["needs_human_review_reason"] = needs_human_reason

            research.audit["verification"] = {
                "status": research.status,
                "rounds_used": len(log["rounds"]),
                "final_mechanical_stats": log["rounds"][-1]["mechanical"].get("stats"),
                "final_llm_stats": log["rounds"][-1]["llm"].get("stats"),
                "open_disagreements": log["rounds"][-1]["llm"].get("disagreements"),
            }
            write_json(final_path, research.model_dump(mode="json"))

            all_llm_calls = (run.llm_calls or []) + [
                c for r in log["rounds"] for c in (r["llm"].get("llm_calls") or [])
            ]
            usage = _usage(all_llm_calls)
            log.update(
                {
                    "finished_at": utcnow(),
                    "duration_s": round(time.monotonic() - started, 2),
                    "final_status": research.status,
                    "rounds_used": len(log["rounds"]),
                    "pass1_path": str(pass1_path.relative_to(self.settings.base_dir)),
                    "final_path": str(final_path.relative_to(self.settings.base_dir)),
                    "needs_human_review_reason": needs_human_reason,
                    "tool_calls": run.tools.tool_log(),
                    "tool_call_count": len(run.tools.calls),
                    "gaps": run.gaps,
                    "sources": [d.log() for d in run.docs],
                    "llm_calls": run.llm_calls,
                    "usage": usage,
                }
            )
            write_json(log_path, log)
            return Outcome(
                app=app["name"],
                category=app["category"],
                slug=slug,
                status=research.status,
                rounds=len(log["rounds"]),
                duration_s=log["duration_s"],
                tool_calls=len(run.tools.calls),
                cost_usd=usage["cost_usd"],
            )
        except Exception as exc:
            log.update(
                {
                    "finished_at": utcnow(),
                    "duration_s": round(time.monotonic() - started, 2),
                    "final_status": "error",
                    "error": f"{type(exc).__name__}: {exc}",
                    "traceback": traceback.format_exc()[-6000:],
                }
            )
            if run is not None:
                log["tool_calls"] = run.tools.tool_log()
                log["tool_call_count"] = len(run.tools.calls)
                log["gaps"] = run.gaps
                log["sources"] = [d.log() for d in run.docs]
                log["llm_calls"] = run.llm_calls
            write_json(log_path, log)
            return Outcome(
                app=app["name"], category=app["category"], slug=slug, status="error",
                duration_s=log["duration_s"], error=f"{type(exc).__name__}: {exc}",
            )
        finally:
            if mech_tools is not None:
                await mech_tools.close()
            if run is not None:
                await run.close()

    def summary(self) -> dict[str, Any]:
        finished = utcnow()
        verified = sum(1 for o in self.outcomes if o.status == "verified" and not o.skipped)
        human = sum(1 for o in self.outcomes if o.status == "needs_human_review" and not o.skipped)
        drafted = sum(1 for o in self.outcomes if o.status == "drafted" and not o.skipped)
        errors = sum(1 for o in self.outcomes if o.status == "error")
        skipped = sum(1 for o in self.outcomes if o.skipped)
        return {
            "started_at": self.started_at,
            "finished_at": finished,
            "processed": len(self.outcomes),
            "verified": verified,
            "needs_human_review": human,
            "drafted": drafted,
            "errors": errors,
            "skipped": skipped,
            "concurrency": self.settings.concurrency,
            "apps": [
                {
                    "app": o.app,
                    "category": o.category,
                    "slug": o.slug,
                    "status": o.status,
                    "skipped": o.skipped,
                    "rounds": o.rounds,
                    "duration_s": o.duration_s,
                    "tool_calls": o.tool_calls,
                    "cost_usd": o.cost_usd,
                    "error": o.error,
                }
                for o in self.outcomes
            ],
        }


def _status_of(path: Path) -> str | None:
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("status")
    except Exception:
        return None
