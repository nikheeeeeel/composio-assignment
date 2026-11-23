"""Agent 1: researcher.

The researcher is an agentic gather -> synthesize -> refine loop. Tool use is capped and
logged. Synthesis happens in the tool-less `json-only` model agent, so the model cannot
quietly browse around the evidence bundle or execute commands.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from pydantic import ValidationError

from composio_tools import (
    ComposioRuntime,
    EvidenceDoc,
    ResearchTools,
    SearchHit,
    SessionHandle,
    ToolBudgetExceeded,
    ToolError,
    normalize_text,
)
from config import Settings
from evidence import app_domain, build_evidence_bundle, is_official_url, official_score
from llm import LLMClient, LLMError
from prompts import (
    RESEARCHER_SYSTEM,
    researcher_revision_prompt,
    researcher_synthesis_prompt,
    targeted_search_queries,
)
from schema import CLAIM_FIELDS, HIGH_RISK_FIELDS, AppResearch


class ResearchError(RuntimeError):
    pass


@dataclass
class ResearchRun:
    app: dict[str, Any]
    handle: SessionHandle
    tools: ResearchTools
    retrieved_at: str
    docs: list[EvidenceDoc] = field(default_factory=list)
    hits: list[SearchHit] = field(default_factory=list)
    catalog: dict[str, Any] | None = None
    github: list[dict[str, Any]] = field(default_factory=list)
    registry: list[dict[str, Any]] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    query_log: list[dict[str, Any]] = field(default_factory=list)
    llm_calls: list[dict[str, Any]] = field(default_factory=list)
    research: AppResearch | None = None
    refinement: dict[str, Any] = field(default_factory=dict)

    def evidence_bundle(self, settings: Settings) -> str:
        return build_evidence_bundle(
            app=self.app["name"],
            category=self.app["category"],
            hint=self.app.get("hint"),
            description=self.app.get("description"),
            retrieved_at=self.retrieved_at,
            docs=self.docs,
            hits=self.hits,
            catalog=self.catalog,
            github=self.github,
            registry=self.registry,
            gaps=self.gaps,
            query_log=self.query_log,
            budget=settings.evidence_char_budget,
        )

    async def close(self) -> None:
        await self.tools.close()
        await self.handle.close()


def _hint_url(hint: str | None) -> str | None:
    if not hint:
        return None
    hint = hint.strip()
    if not hint:
        return None
    if hint.startswith(("http://", "https://")):
        return hint
    if "." in hint and " " not in hint:
        return "https://" + hint
    return None


def _add_doc(run: ResearchRun, doc: EvidenceDoc, domain: str | None) -> None:
    doc.official = is_official_url(doc.url, domain, run.app.get("name"))
    existing = next((d for d in run.docs if d.url == doc.url), None)
    if existing is None:
        run.docs.append(doc)
    elif len(doc.text) > len(existing.text):
        run.docs[run.docs.index(existing)] = doc


class ResearchAgent:
    def __init__(self, settings: Settings, runtime: ComposioRuntime, llm: LLMClient):
        self.settings = settings
        self.runtime = runtime
        self.llm = llm

    async def start_run(self, app: dict[str, Any]) -> ResearchRun:
        handle = await self.runtime.create_session()
        tools = ResearchTools(self.runtime, handle, self.settings)
        await tools.start()
        return ResearchRun(
            app=app,
            handle=handle,
            tools=tools,
            retrieved_at=date.today().isoformat(),
            hits=tools.hits,  # keep the run's view in sync with the tool layer
        )

    async def research(self, run: ResearchRun) -> AppResearch:
        started = time.monotonic()
        await self._gather(run)
        bundle = run.evidence_bundle(self.settings)
        research = await self._synthesize(run, bundle, refinement_note="")
        await self._refine_if_needed(run, research)
        assert run.research is not None
        run.research.audit.update(
            {
                "input": {
                    "name": run.app["name"],
                    "category": run.app["category"],
                    "hint": run.app.get("hint"),
                    "description": run.app.get("description"),
                    "source_file": run.app.get("source_file"),
                },
                "retrieved_at": run.retrieved_at,
                "researcher_model": self.settings.researcher_model,
                "mcp_used": run.tools.mcp_active,
                "mcp_error": run.tools.mcp_error,
                "tool_call_count": len(run.tools.calls),
                "tool_call_cap": self.settings.max_tool_calls_per_app,
                "sources": [d.log() for d in run.docs],
                "search_hits_count": len(run.hits),
                "gaps": run.gaps,
                "refinement": run.refinement,
                "research_seconds": round(time.monotonic() - started, 2),
            }
        )
        return run.research

    async def _gather(self, run: ResearchRun) -> None:
        app = run.app
        name = app["name"]
        hint = app.get("hint")
        domain = app_domain(hint)
        hint_url = _hint_url(hint)

        required_queries: list[tuple[str, str]] = [
            ("docs", f'"{name}" official API documentation developer reference'),
            ("auth", f'"{name}" API authentication OAuth API key documentation'),
            ("pricing", f'"{name}" pricing plans API access enterprise developer'),
            ("mcp", f'"{name}" MCP server official Model Context Protocol'),
            ("rate_limits", f'"{name}" API rate limits throttling quota documentation'),
            ("webhooks", f'"{name}" API webhooks events callbacks documentation'),
            ("sandbox", f'"{name}" API sandbox test environment staging developer'),
            ("sdk", f'"{name}" official SDK client libraries developer'),
            ("versioning", f'"{name}" API versioning deprecation sunset policy'),
        ]
        if domain and domain != "github.com":
            required_queries = [(purpose, f"{query} site:{domain}") for purpose, query in required_queries]

        sem = asyncio.Semaphore(max(1, self.settings.tool_concurrency))

        async def run_query(purpose: str, query: str):
            async with sem:
                try:
                    hits = await run.tools.search(query, purpose=purpose)
                    return purpose, query, hits, None
                except Exception as exc:
                    return purpose, query, None, exc

        search_results = await asyncio.gather(*(run_query(p, q) for p, q in required_queries))
        budget_hit = False
        for purpose, query, hits, error in search_results:
            if error is None:
                run.query_log.append(
                    {
                        "purpose": purpose,
                        "query": query,
                        "result_count": len(hits or []),
                        "top_urls": [h["url"] for h in (hits or [])[:3]],
                    }
                )
                continue
            if isinstance(error, ToolBudgetExceeded):
                if not budget_hit:
                    run.gaps.append(
                        f"Tool-call cap ({self.settings.max_tool_calls_per_app}) reached during required searches; stopped early."
                    )
                    budget_hit = True
            else:
                run.gaps.append(f"{purpose} search failed: {error}")
            run.query_log.append(
                {"purpose": purpose, "query": query, "result_count": 0, "error": str(error)[:300]}
            )

        # Pick pages: required evidence first (docs/auth/pricing/limits/MCP), then the
        # explicit hint, then the remaining purposes. A hint pointing directly at a docs
        # path is promoted to the front.
        selected: list[str] = []
        hint_is_docs = bool(hint_url and any(
            marker in hint_url.lower() for marker in ("/docs", "/api", "developer", "reference")
        ))
        if hint_url and hint_is_docs:
            selected.append(hint_url)
        for purpose in ("docs", "auth", "pricing", "rate_limits", "mcp", "webhooks", "sandbox", "sdk", "versioning"):
            purpose_hits = [h for h in run.hits if h.purpose == purpose]
            purpose_hits.sort(key=lambda h: official_score(h.url, domain, name), reverse=True)
            take = 2 if purpose in ("docs", "auth", "pricing", "mcp") else 1
            for hit in purpose_hits[:take]:
                if hit.url not in selected:
                    selected.append(hit.url)
        if hint_url and hint_url not in selected:
            selected.append(hint_url)
        if domain and domain != "github.com":
            for guess in (f"https://{domain}/pricing", f"https://{domain}/docs"):
                if guess not in selected:
                    selected.append(guess)
        selected = selected[: self.settings.max_pages_per_app]

        async def fetch_batch(batch: list[str]):
            async with sem:
                try:
                    docs = await run.tools.fetch(batch, purpose="batch_docs")
                    for doc in docs:
                        _add_doc(run, doc, domain)
                except ToolBudgetExceeded:
                    run.gaps.append("Tool-call cap reached during page fetches; some pages were not fetched.")
                except ToolError as exc:
                    run.gaps.append(f"Batch page fetch failed ({len(batch)} URLs): {exc}")
                    if batch:
                        doc = await run.tools.direct_fetch(batch[0], purpose="batch_docs")
                        if doc:
                            _add_doc(run, doc, domain)

        # Vendor GitHub repos / MCP candidates.
        github_urls: list[str] = []
        for hit in run.hits:
            if "github.com" in hit.url and hit.url not in github_urls:
                github_urls.append(hit.url)
        explicit = _github_from_hint(hint)
        if explicit and explicit not in github_urls:
            github_urls.insert(0, explicit)

        async def github_one(url: str):
            async with sem:
                try:
                    run.github.append(await run.tools.github(url))
                except ToolError as exc:
                    run.gaps.append(f"GitHub lookup failed for {url}: {exc}")

        async def registry_one():
            async with sem:
                try:
                    run.registry = await run.tools.registry(f"{name} MCP")
                except ToolBudgetExceeded:
                    run.gaps.append("Tool-call cap reached before the MCP registry check.")
                except ToolError as exc:
                    run.gaps.append(f"MCP registry check failed: {exc}")

        async def catalog_one():
            async with sem:
                try:
                    run.catalog = await run.tools.catalog(name, hint)
                except ToolBudgetExceeded:
                    run.gaps.append("Tool-call cap reached before the Composio catalog check.")
                except Exception as exc:
                    run.gaps.append(f"Composio catalog check failed: {exc}")

        tasks = [fetch_batch(selected[i : i + 5]) for i in range(0, len(selected), 5)]
        tasks.extend(github_one(url) for url in github_urls[:3])
        tasks.append(registry_one())
        tasks.append(catalog_one())
        await asyncio.gather(*tasks)

        # If the explicitly supplied docs hint produced no text, try a direct HTML fetch.
        if hint_url and not any(d.url.rstrip("/") == hint_url.rstrip("/") for d in run.docs):
            doc = await run.tools.direct_fetch(hint_url, purpose="docs")
            if doc:
                _add_doc(run, doc, domain)
                run.gaps.append(f"Composio page fetch returned no text for the input hint; used direct HTML fetch: {hint_url}")

        # Gap accounting.
        hit_purposes = {h.purpose for h in run.hits}
        if not any(d.official for d in run.docs):
            run.gaps.append("No official vendor page was fetched; evidence may be third-party only.")
        for purpose, label in (
            ("docs", "developer documentation"),
            ("auth", "authentication documentation"),
            ("pricing", "pricing/API-tier page"),
            ("rate_limits", "rate-limit documentation"),
            ("mcp", "MCP server evidence"),
        ):
            if purpose not in hit_purposes:
                run.gaps.append(f"No {label} found in search results.")
            elif not any(d.purpose in ("batch_docs", "refine", purpose) for d in run.docs):
                run.gaps.append(f"{label.capitalize()} had search results but no page text was captured.")
        if run.tools.mcp_error:
            run.gaps.append(f"MCP transport issue (SDK fallback used): {run.tools.mcp_error}")
        if run.tools.budget.used >= self.settings.max_tool_calls_per_app:
            run.gaps.append("Tool-call cap reached; remaining unknowns are reported rather than guessed.")

    async def _synthesize(
        self, run: ResearchRun, bundle: str, *, refinement_note: str
    ) -> AppResearch:
        user = researcher_synthesis_prompt(
            app=run.app["name"],
            category=run.app["category"],
            hint=run.app.get("hint"),
            description=run.app.get("description"),
            retrieved_at=run.retrieved_at,
            evidence_bundle=bundle,
            search_log=run.query_log,
        )
        if refinement_note:
            user += f"\n\nADDITIONAL INSTRUCTION:\n{refinement_note}\n"
        data, calls = await self.llm.complete_json(
            system=RESEARCHER_SYSTEM,
            user=user,
            model=self.settings.researcher_model,
            purpose=f"research:{run.app['name']}",
        )
        run.llm_calls.extend(call.log() for call in calls)
        return await self._validate_with_retry(run, data, bundle, refinement_note=refinement_note)

    async def _validate_with_retry(
        self, run: ResearchRun, data: dict[str, Any], bundle: str, *, refinement_note: str
    ) -> AppResearch:
        try:
            return AppResearch.model_validate(data)
        except ValidationError as first_error:
            retry_user = researcher_synthesis_prompt(
                app=run.app["name"],
                category=run.app["category"],
                hint=run.app.get("hint"),
                description=run.app.get("description"),
                retrieved_at=run.retrieved_at,
                evidence_bundle=bundle,
                search_log=run.query_log,
            )
            if refinement_note:
                retry_user += f"\n\nADDITIONAL INSTRUCTION:\n{refinement_note}\n"
            retry_user += (
                "\n\nYOUR PREVIOUS JSON FAILED SCHEMA VALIDATION. Fix exactly these problems and return "
                f"the complete corrected JSON object:\n{first_error}\n"
            )
            data2, calls = await self.llm.complete_json(
                system=RESEARCHER_SYSTEM,
                user=retry_user,
                model=self.settings.researcher_model,
                purpose=f"research:{run.app['name']}:schema-retry",
            )
            run.llm_calls.extend(call.log() for call in calls)
            try:
                return AppResearch.model_validate(data2)
            except ValidationError as second_error:
                raise ResearchError(f"schema validation failed twice: {second_error}") from second_error

    async def _refine_if_needed(self, run: ResearchRun, research: AppResearch) -> None:
        weak: list[str] = []
        for field in HIGH_RISK_FIELDS:
            if field in ("buildability_verdict", "needs_outreach"):
                continue
            claim = research.claim(field)
            if claim.confidence in ("low", "not_found") or claim.value is None:
                weak.append(field)
        if not weak:
            run.research = research
            return
        if run.tools.budget.used + 3 > self.settings.max_tool_calls_per_app:
            run.gaps.append(
                "High-risk fields still weak but the tool-call cap leaves no room for a refinement pass: "
                + ", ".join(weak)
            )
            run.research = research
            return

        added_queries: list[str] = []
        added_urls: list[str] = []
        for field in weak[:2]:
            for query in targeted_search_queries(run.app["name"], field)[:2]:
                try:
                    hits = await run.tools.search(query, purpose="refine")
                    added_queries.append(query)
                    run.query_log.append(
                        {
                            "purpose": "refine",
                            "field": field,
                            "query": query,
                            "result_count": len(hits),
                            "top_urls": [h["url"] for h in hits[:3]],
                        }
                    )
                    for hit in hits[:1]:
                        if hit["url"] not in added_urls:
                            added_urls.append(hit["url"])
                except (ToolBudgetExceeded, ToolError) as exc:
                    run.gaps.append(f"Refinement search for {field} failed: {exc}")
                    break
        if not added_urls:
            run.research = research
            return
        domain = app_domain(run.app.get("hint"))
        for start in range(0, len(added_urls), 5):
            try:
                docs = await run.tools.fetch(added_urls[start : start + 5], purpose="refine")
                for doc in docs:
                    _add_doc(run, doc, domain)
            except (ToolBudgetExceeded, ToolError) as exc:
                run.gaps.append(f"Refinement fetch failed: {exc}")

        bundle = run.evidence_bundle(self.settings)
        note = (
            "This is a refinement pass. The first draft had low/not_found values for: "
            + ", ".join(weak)
            + ". Extra sources were fetched. Re-check those fields and any dependent verdict. "
            "Do not invent evidence."
        )
        try:
            refined = await self._synthesize(run, bundle, refinement_note=note)
        except (ResearchError, LLMError) as exc:
            run.gaps.append(f"Refinement synthesis failed; keeping first draft: {exc}")
            run.research = research
            return
        run.refinement = {
            "weak_fields": weak,
            "queries": added_queries,
            "extra_urls": added_urls,
            "tool_calls_after": len(run.tools.calls),
        }
        run.research = refined

    async def revise(
        self,
        run: ResearchRun,
        original: AppResearch,
        issues: list[dict[str, Any]],
        *,
        round_number: int,
    ) -> AppResearch:
        # Fetch a little extra evidence for the specific failed fields, if budget allows.
        fields = []
        for issue in issues:
            field = issue.get("field")
            if field and field not in fields and field not in ("buildability_verdict", "needs_outreach"):
                fields.append(field)
        if run.tools.budget.used + 2 <= self.settings.max_tool_calls_per_app:
            domain = app_domain(run.app.get("hint"))
            for field in fields[:2]:
                for query in targeted_search_queries(run.app["name"], field)[:1]:
                    try:
                        hits = await run.tools.search(query, purpose="refine")
                        run.query_log.append(
                            {
                                "purpose": "revision",
                                "field": field,
                                "query": query,
                                "result_count": len(hits),
                                "top_urls": [h["url"] for h in hits[:2]],
                            }
                        )
                        urls = [h["url"] for h in hits[:1]]
                        if urls:
                            docs = await run.tools.fetch(urls, purpose="refine")
                            for doc in docs:
                                _add_doc(run, doc, domain)
                    except (ToolBudgetExceeded, ToolError) as exc:
                        run.gaps.append(f"Revision search for {field} failed: {exc}")
                        break

        bundle = run.evidence_bundle(self.settings)
        user = researcher_revision_prompt(
            app=run.app["name"],
            category=run.app["category"],
            retrieved_at=run.retrieved_at,
            original_json=original.model_dump(mode="json"),
            issues=issues,
            evidence_bundle=bundle,
            extra_note=f"\nThis is revision round {round_number} of at most {self.settings.max_revision_rounds}.",
        )
        data, calls = await self.llm.complete_json(
            system=RESEARCHER_SYSTEM,
            user=user,
            model=self.settings.researcher_model,
            purpose=f"revise:{run.app['name']}:r{round_number}",
        )
        run.llm_calls.extend(call.log() for call in calls)
        merged = self._merge_revision_patch(original, data)
        try:
            revised = AppResearch.model_validate(merged)
        except ValidationError as patch_error:
            # Last resort: ask for the complete object rather than a patch.
            full_user = (
                user
                + f"\n\nYour patch failed schema validation: {patch_error}\n"
                + "Return the COMPLETE revised AppResearch JSON object now (not a patch)."
            )
            data2, calls2 = await self.llm.complete_json(
                system=RESEARCHER_SYSTEM,
                user=full_user,
                model=self.settings.researcher_model,
                purpose=f"revise:{run.app['name']}:r{round_number}:full-fallback",
            )
            run.llm_calls.extend(call.log() for call in calls2)
            revised = await self._validate_with_retry(
                run, data2, bundle, refinement_note=f"revision round {round_number}"
            )
        revised.audit = dict(original.audit)
        revised.audit["revision_round"] = round_number
        revised.audit["revision_issues"] = issues
        revised.audit["revision_model"] = self.settings.researcher_model
        return revised

    def _merge_revision_patch(self, original: AppResearch, data: dict[str, Any]) -> dict[str, Any]:
        """Accept either a full AppResearch object or a patch with an `updates` map."""
        if "updates" not in data and "app" in data and any(field in data for field in CLAIM_FIELDS):
            return data
        base = original.model_dump(mode="json")
        updates = data.get("updates")
        if not isinstance(updates, dict):
            updates = {}
        for field in CLAIM_FIELDS:
            if field in updates:
                base[field] = updates[field]
        for key in (
            "buildability_verdict",
            "verdict_reason",
            "main_blocker",
            "needs_outreach",
            "outreach_reason",
            "open_questions",
        ):
            if key in data:
                base[key] = data[key]
            elif key in updates:
                base[key] = updates[key]
        base["app"] = original.app
        base["category"] = original.category
        base["status"] = "drafted"
        return base


def _github_from_hint(hint: str | None) -> str | None:
    if not hint:
        return None
    hint = hint.strip()
    if hint.startswith("https://github.com/") or hint.startswith("http://github.com/"):
        return hint
    if hint.startswith("github.com/"):
        return "https://" + hint
    return None
