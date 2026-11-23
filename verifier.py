"""Layer B: adversarial LLM verification.

Two independent calls:
  1. claim judge: SUPPORTED / PARTIALLY / UNSUPPORTED / CONTRADICTED per claim
  2. independent re-derivation of the six highest-risk fields, without seeing the
     researcher's answers first

Weak/not_found claims trigger extra searches before both calls. Disagreements and
unsupported high-confidence claims become issues fed back to the researcher.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from urllib.parse import urlparse

from composio_tools import (
    ComposioRuntime,
    EvidenceDoc,
    ResearchTools,
    ToolBudget,
    ToolBudgetExceeded,
    ToolError,
)
from config import Settings
from evidence import app_domain, is_official_url
from llm import LLMClient, LLMError
from prompts import (
    CLAIM_JUDGE_SYSTEM,
    REDERIVE_SYSTEM,
    claim_judge_prompt,
    rederive_prompt,
    targeted_search_queries,
)
from researcher import ResearchRun
from schema import CLAIM_FIELDS, FIELD_DESCRIPTIONS, HIGH_RISK_FIELDS, AppResearch


def _norm_auth(values: Any) -> set[str]:
    if not values:
        return set()
    if isinstance(values, str):
        values = [values]
    out: set[str] = set()
    for raw in values:
        value = str(raw).lower().strip().replace("-", "_").replace(" ", "_")
        if value in ("oauth", "oauth2", "oauth_2", "oauth2.0", "oauth_2.0"):
            out.add("oauth2")
        elif value in ("api_key", "apikey", "api_token", "key"):
            out.add("api_key")
        elif value in ("bearer", "bearer_token", "token"):
            out.add("bearer_token")
        elif value in ("basic", "basic_auth", "username_password"):
            out.add("basic")
        elif value == "jwt":
            out.add("jwt")
        elif value:
            out.add(value)
    return out


_ACCESS_ALIASES = {
    "free": "self_serve_free",
    "open": "self_serve_free",
    "open_access": "self_serve_free",
    "public": "self_serve_free",
    "freemium": "self_serve_free",
    "free_tier": "self_serve_free",
    "self_serve": "self_serve_free",
    "selfservefree": "self_serve_free",
    "self_serve_free": "self_serve_free",
    "trial": "self_serve_trial",
    "self_serve_trial": "self_serve_trial",
    "paid": "paid_plan_required",
    "paid_plan": "paid_plan_required",
    "self_serve_paid": "paid_plan_required",
    "paid_plan_required": "paid_plan_required",
    "admin_approval": "admin_approval",
    "app_review": "app_review_required",
    "app_review_required": "app_review_required",
    "partner": "partner_only",
    "partner_only": "partner_only",
    "contact_sales": "contact_sales",
    "no_api": "no_api",
    "none": "no_api",
    "unknown": "unknown",
}


def _norm_access(value: Any) -> str:
    if not value:
        return "unknown"
    key = str(value).lower().strip().replace(" ", "_").replace("-", "_")
    return _ACCESS_ALIASES.get(key, key)


def _norm_url(value: Any) -> str | None:
    if not value or str(value).lower() in ("none", "null", "n/a"):
        return None
    text = str(value).strip().rstrip("/")
    parsed = urlparse(text)
    if not parsed.scheme:
        return text.lower()
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}{parsed.path.rstrip('/')}"


def _as_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in ("true", "yes", "1"):
        return True
    if text in ("false", "no", "0"):
        return False
    return None


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


class Verifier:
    def __init__(self, settings: Settings, runtime: ComposioRuntime, llm: LLMClient):
        self.settings = settings
        self.runtime = runtime
        self.llm = llm
        self._search_cache: dict[str, list[dict[str, Any]]] = {}

    async def verify(
        self,
        research: AppResearch,
        app: dict[str, Any],
        mech: dict[str, Any],
        run: ResearchRun,
        *,
        round_number: int,
    ) -> dict[str, Any]:
        vtools = ResearchTools(self.runtime, run.handle, self.settings)
        vtools.budget = ToolBudget(20)
        await vtools.start()
        calls: list[dict[str, Any]] = []
        searches: list[dict[str, Any]] = []
        try:
            weak = [
                field
                for field in CLAIM_FIELDS
                if field != "composio_toolkit_exists"
                and research.claim(field).confidence in ("low", "not_found")
            ]
            for field in weak[:5]:
                for query in targeted_search_queries(app["name"], field)[:2]:
                    try:
                        if query in self._search_cache:
                            hits = self._search_cache[query]
                        else:
                            hits = await vtools.search(query, purpose="verification")
                            self._search_cache[query] = hits
                        searches.append({"field": field, "query": query, "hits": [h["url"] for h in hits[:2]]})
                        known = {d.url for d in run.docs}
                        urls = [h["url"] for h in hits[:1] if h["url"] not in known]
                        if urls:
                            docs = await vtools.fetch(urls, purpose="verification")
                            for doc in docs:
                                self._add_doc(run, doc, app_domain(app.get("hint")))
                    except (ToolBudgetExceeded, ToolError) as exc:
                        run.gaps.append(f"Verifier re-search for {field} failed: {exc}")
                        break
            evidence = run.evidence_bundle(self.settings)

            # The claim judge and the independent re-derivation are independent: run them
            # concurrently. Re-derivation still never sees the researcher's answers.
            judge_result, rederive_result = await asyncio.gather(
                self._judge_claims(research, mech),
                self._rederive(app, evidence),
            )
            judgments, judge_calls = judge_result
            rederived, rederive_calls = rederive_result
            calls.extend(call.log() for call in judge_calls)
            calls.extend(call.log() for call in rederive_calls)
            per_field, disagreements, issues = self._compare(
                research=research,
                mech=mech,
                judgments=judgments,
                rederived=rederived,
            )
            issues = _dedupe_issues(issues)
            critical = [i for i in issues if i.get("severity") == "critical"]
            return {
                "passed": not critical,
                "per_field": per_field,
                "disagreements": disagreements,
                "issues": issues,
                "judgments": judgments,
                "rederived": rederived,
                "llm_calls": calls,
                "searches": searches,
                "stats": {
                    "verifier_model": self.settings.verifier_model,
                    "round": round_number,
                    "claims_judged": len(judgments),
                    "supported": sum(1 for j in judgments.values() if j.get("verdict") == "SUPPORTED"),
                    "partially": sum(1 for j in judgments.values() if j.get("verdict") == "PARTIALLY"),
                    "unsupported": sum(1 for j in judgments.values() if j.get("verdict") == "UNSUPPORTED"),
                    "contradicted": sum(1 for j in judgments.values() if j.get("verdict") == "CONTRADICTED"),
                    "disagreements": len(disagreements),
                    "critical_issues": len(critical),
                    "warnings": len(issues) - len(critical),
                    "verifier_searches": searches,
                    "verifier_tool_calls": len(vtools.calls),
                },
            }
        finally:
            await vtools.close()

    def _add_doc(self, run: ResearchRun, doc: EvidenceDoc, domain: str | None) -> None:
        doc.official = is_official_url(doc.url, domain, run.app.get("name"))
        existing = next((d for d in run.docs if d.url == doc.url), None)
        if existing is None:
            run.docs.append(doc)
        elif len(doc.text) > len(existing.text):
            run.docs[run.docs.index(existing)] = doc

    async def _judge_claims(
        self,
        research: AppResearch,
        mech: dict[str, Any],
    ) -> tuple[dict[str, dict[str, Any]], list[Any]]:
        claim_rows = []
        for field in CLAIM_FIELDS:
            claim = research.claim(field)
            m = mech.get("claims", {}).get(field, {})
            claim_rows.append(
                {
                    "field": field,
                    "description": FIELD_DESCRIPTIONS.get(field, ""),
                    "value": claim.value,
                    "source_url": claim.source_url,
                    "quote": claim.quote,
                    "confidence": claim.confidence,
                    "mechanical_quote_score": m.get("quote_score"),
                    "mechanical_quote_pass": m.get("quote_pass"),
                    "page_status": m.get("page_status"),
                    "page_title": m.get("page_title"),
                    "excerpt": m.get("excerpt") or "",
                }
            )
        user = claim_judge_prompt(research.app, claim_rows)
        data, responses = await self.llm.complete_json(
            system=CLAIM_JUDGE_SYSTEM,
            user=user,
            model=self.settings.verifier_model,
            purpose=f"verify-claims:{research.app}",
        )
        raw = data.get("judgments")
        if not isinstance(raw, list):
            raise LLMError("claim judge did not return a judgments list")
        out = {}
        for row in raw:
            if not isinstance(row, dict) or not row.get("field"):
                continue
            verdict = str(row.get("verdict") or "").upper().strip()
            if verdict not in ("SUPPORTED", "PARTIALLY", "UNSUPPORTED", "CONTRADICTED"):
                verdict = "UNSUPPORTED"
            out[row["field"]] = {"verdict": verdict, "reason": str(row.get("reason") or "")[:400]}
        for field in CLAIM_FIELDS:
            out.setdefault(field, {"verdict": "UNSUPPORTED", "reason": "verifier omitted this field"})
        return out, responses

    async def _rederive(
        self, app: dict[str, Any], evidence_bundle: str
    ) -> tuple[dict[str, Any], list[Any]]:
        data, responses = await self.llm.complete_json(
            system=REDERIVE_SYSTEM,
            user=rederive_prompt(app=app["name"], category=app["category"], evidence_bundle=evidence_bundle),
            model=self.settings.verifier_model,
            purpose=f"rederive:{app['name']}",
        )
        return (
            {
                "auth_methods": data.get("auth_methods"),
                "access_model": _norm_access(data.get("access_model")),
                "official_mcp_server": data.get("official_mcp_server"),
                "has_public_api": _as_bool(data.get("has_public_api")),
                "buildability_verdict": str(data.get("buildability_verdict") or "unknown").lower(),
                "needs_outreach": _as_bool(data.get("needs_outreach")),
                "reasoning": data.get("reasoning") or {},
            },
            responses,
        )

    def _compare(
        self,
        *,
        research: AppResearch,
        mech: dict[str, Any],
        judgments: dict[str, dict[str, Any]],
        rederived: dict[str, Any],
    ) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
        issues: list[dict[str, Any]] = list(mech.get("issues") or [])
        disagreements: list[dict[str, Any]] = []
        per_field: dict[str, Any] = {}

        for field in CLAIM_FIELDS:
            claim = research.claim(field)
            judgment = judgments.get(field, {"verdict": "UNSUPPORTED", "reason": ""})
            m = mech.get("claims", {}).get(field, {})
            per_field[field] = {
                "judgment": judgment["verdict"],
                "reason": judgment.get("reason", ""),
                "confidence": claim.confidence,
                "value": claim.value,
                "source_url": claim.source_url,
                "mechanical_quote_score": m.get("quote_score"),
                "mechanical_quote_pass": m.get("quote_pass"),
            }
            verdict = judgment["verdict"]
            if verdict == "CONTRADICTED":
                issues.append(
                    {
                        "field": field,
                        "problem": f"verifier found the evidence contradicts the value: {judgment.get('reason')}",
                        "severity": "critical",
                        "suggested_fix": "Correct the value from the cited evidence, or mark it not_found if the evidence is ambiguous.",
                    }
                )
            elif verdict == "UNSUPPORTED" and claim.confidence in ("high", "medium"):
                issues.append(
                    {
                        "field": field,
                        "problem": f"verifier could not support a {claim.confidence}-confidence value: {judgment.get('reason')}",
                        "severity": "critical",
                        "suggested_fix": "Cite a page/quote that directly entails the value, or downgrade confidence and add an open question.",
                    }
                )
            elif verdict == "PARTIALLY":
                issues.append(
                    {
                        "field": field,
                        "problem": f"verifier judged the claim only partially supported: {judgment.get('reason')}",
                        "severity": "warning",
                        "suggested_fix": "Tighten the value/quote so the excerpt fully entails it.",
                    }
                )

        # Independent re-derivation comparison.
        comparisons = {
            "auth_methods": (
                _norm_auth(research.claim("auth_methods").value),
                _norm_auth(rederived.get("auth_methods")),
            ),
            "access_model": (
                _norm_access(research.claim("access_model").value),
                _norm_access(rederived.get("access_model")),
            ),
            "official_mcp_server": (
                _norm_url(research.claim("official_mcp_server").value),
                _norm_url(rederived.get("official_mcp_server")),
            ),
            "has_public_api": (
                _as_bool(research.claim("has_public_api").value),
                _as_bool(rederived.get("has_public_api")),
            ),
            "buildability_verdict": (research.buildability_verdict, rederived.get("buildability_verdict")),
            "needs_outreach": (research.needs_outreach, rederived.get("needs_outreach")),
        }
        for field, (mine, theirs) in comparisons.items():
            equal = mine == theirs
            transport_only = False
            if field == "auth_methods" and isinstance(mine, set) and isinstance(theirs, set):
                transport = {"basic", "bearer_token"}
                mine_core, theirs_core = mine - transport, theirs - transport
                if mine_core or theirs_core:
                    equal = mine_core == theirs_core
                    transport_only = equal and mine != theirs
                else:
                    equal = mine == theirs
                if transport_only:
                    issues.append(
                        {
                            "field": field,
                            "problem": "auth transport schemes differ, but the credential methods (oauth2/api_key/jwt) agree",
                            "severity": "warning",
                            "suggested_fix": "Keep the credential methods; mention bearer/basic only if a source explicitly documents them as the request encoding.",
                        }
                    )
                    per_field.setdefault(field, {})["transport_note"] = {
                        "researcher": sorted(mine),
                        "verifier": sorted(theirs),
                    }
            if not equal:
                disagreement = {
                    "field": field,
                    "researcher": mine if not isinstance(mine, set) else sorted(mine),
                    "verifier": theirs if not isinstance(theirs, set) else sorted(theirs),
                    "reason": (rederived.get("reasoning") or {}).get(field, "")
                    if isinstance(rederived.get("reasoning"), dict)
                    else "",
                }
                disagreements.append(disagreement)
                per_field.setdefault(field, {})
                per_field[field]["independent_value"] = disagreement["verifier"]
                per_field[field]["disagreement"] = True
                issues.append(
                    {
                        "field": field,
                        "problem": f"independent re-derivation disagrees: researcher={disagreement['researcher']!r}, verifier={disagreement['verifier']!r}",
                        "severity": "critical",
                        "suggested_fix": "Resolve from the evidence; if genuinely ambiguous, set not_found and add an open question, or mark needs_human_review.",
                    }
                )

        # Unresolved high-risk claims.
        for field in HIGH_RISK_FIELDS:
            if field in ("buildability_verdict", "needs_outreach"):
                continue
            claim = research.claim(field)
            if claim.confidence in ("low", "not_found") or claim.value is None:
                issues.append(
                    {
                        "field": field,
                        "problem": f"high-risk field remains unresolved ({claim.confidence}, value={claim.value!r})",
                        "severity": "critical",
                        "suggested_fix": "Resolve it from a vendor source. If impossible, leave not_found, add a precise open_question, and expect human review.",
                    }
                )
        return per_field, disagreements, issues
