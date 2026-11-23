"""All model prompts and the verdict rubric live here."""

from __future__ import annotations

import json
from typing import Any

from schema import ACCESS_MODELS, FIELD_DESCRIPTIONS

RUBRIC = """\
VERDICT RUBRIC (apply consistently):
- easy: public docs; self-serve credentials (free or trial); OAuth2 or API key; reasonable API breadth.
- moderate: public API but gated by plan tier, app review, or narrow scope.
- hard: partner-only/contact-sales, or the API is too thin/undocumented to cover useful actions.
- blocked: no public API (scraping or private endpoints only).
Never label an app easy if credentials require sales contact, partner approval, or an app review.
"""

CLAIM_RULES = """\
CLAIM RULES (hard):
1. Every factual field is a Claim: {value, source_url, quote, confidence, retrieved_at}.
2. confidence=high or medium REQUIRES both source_url and a verbatim quote (<=300 chars).
3. confidence=not_found REQUIRES value=null. If evidence is missing, use null/not_found. NEVER guess.
4. Prefer official vendor documentation over blogs, forums, review sites, and scraped mirrors.
   If only third-party sources exist, use them but say so in access_notes/open_questions and prefer
   confidence=medium or low.
5. source_url MUST be one of the URLs in the SOURCES section (exact string), except the Composio
   catalog field, which must cite the catalog URL shown in the COMPOSIO CATALOG section.
6. The quote must be copied verbatim from that source's text. Do not paraphrase, join sentences
   with ellipses, or add words. If the needed sentence is longer than 300 chars, quote the shortest
   clause that still supports the value.
7. Do not fill any field from memory or general knowledge. If the evidence does not answer it,
   record it in open_questions and use null/not_found.
8. If the app name is ambiguous, use the input hint/description to disambiguate and note the
   ambiguity in open_questions. Do not research a different product with the same name.
9. If docs are behind a login or JS shell and the text is unreadable, say so in open_questions and
   do not invent the answer.
10. Never copy the same generic quote into multiple unrelated fields unless it genuinely supports each.
"""

OUTPUT_CONTRACT = """\
Return ONE JSON object with exactly these keys (no markdown fences):

{
  "app": "<input app name>",
  "category": "<input category>",
  "one_line_description": CLAIM,
  "has_public_api": CLAIM,                    // value must be true/false
  "api_type": CLAIM,                          // value: "REST" | "GraphQL" | "SOAP" | "other"
  "api_breadth": CLAIM,                       // value: short string about size/read-write scope
  "docs_url": CLAIM,                          // value: official docs URL
  "auth_methods": CLAIM,                      // value: list subset of ["oauth2","api_key","basic","bearer_token","jwt","other"]
  "oauth_scopes_documented": CLAIM,           // value: true/false
  "access_model": CLAIM,                      // value: one of ACCESS_MODELS below
  "access_notes": CLAIM,                      // value: exactly what gates access
  "official_mcp_server": CLAIM,               // value: vendor URL or null
  "community_mcp_servers": CLAIM,             // value: list of URLs
  "composio_toolkit_exists": CLAIM,           // value: true/false
  "rate_limits": CLAIM,                       // value: string with numbers/units, or "not documented"
  "webhooks": CLAIM,                          // value: true/false
  "sandbox_or_test_env": CLAIM,               // value: true/false
  "sdk_languages": CLAIM,                     // value: list of official SDK languages
  "api_versioning_or_deprecation_notes": CLAIM, // value: string
  "buildability_verdict": "easy" | "moderate" | "hard" | "blocked",
  "verdict_reason": "<2-4 sentences citing the decisive evidence>",
  "main_blocker": "<short phrase or null>",
  "needs_outreach": true | false,
  "outreach_reason": "<why a human must contact the vendor, or null>",
  "open_questions": ["..."],
  "status": "drafted",
  "audit": {}
}

CLAIM = {
  "value": <string|boolean|list[str]|null>,
  "source_url": "<exact URL from SOURCES, or null>",
  "quote": "<verbatim snippet <=300 chars, or null>",
  "confidence": "high" | "medium" | "low" | "not_found",
  "retrieved_at": "<the RETRIEVED_AT date supplied in the evidence>"
}

ACCESS_MODELS = """ + ", ".join(ACCESS_MODELS) + """

FIELD NOTES:
""" + "\n".join(f"- {name}: {desc}" for name, desc in FIELD_DESCRIPTIONS.items()) + """

FIELD-SPECIFIC EXTRACTION NOTES:
- auth_methods: list EVERY mechanism explicitly named in the cited quote/page. If the page says
  "OAuth 2.0 and JWT bearer", the value is ["oauth2","jwt"], not just ["oauth2"]. If the page only
  says "OAuth 2.0", do not add api_key just because many APIs have one.
- access_model: classify the lowest-friction documented route for a third-party developer to obtain
  working API credentials for development/testing. A free, self-serve developer edition/sandbox
  counts as self_serve_free; a time-limited trial counts as self_serve_trial; a paid plan or paid
  API add-on required before any credentials counts as paid_plan_required. Prefer the official
  pricing/plans page. If only a third-party source states the gating, use confidence=medium or low
  and say so in access_notes. Put production/customer-org gating in access_notes even when a free
  developer edition exists.
- sdk_languages: only official, vendor-maintained SDKs. Third-party libraries do not count.
- official_mcp_server: only a vendor-operated server. Use the actual MCP endpoint URL when the
  evidence gives one; if the vendor documents a hosted MCP server but the endpoint is per-org or
  not published, use the official vendor documentation URL and note that in access_notes. A GitHub
  repo, community directory entry, or third-party server belongs in community_mcp_servers.
- community_mcp_servers: list actual URLs; if none found, null/not_found (not an empty list with
  high confidence unless a page explicitly says no community servers exist).
- api_breadth: prefer official API reference/index pages over comparison blogs. State rough
  resource/endpoint count and read-only vs read-write when the evidence supports it.
- composio_toolkit_exists: use the COMPOSIO CATALOG CHECK section, and cite its catalog_url plus
  a short verbatim JSON fragment as the quote.

DECISION RULES:
- has_public_api=false => buildability_verdict must be "blocked" and main_blocker must explain why.
- access_model in {partner_only, contact_sales} => verdict "hard" unless there is documented public
  self-serve access, and needs_outreach=true.
- access_model in {self_serve_free, self_serve_trial} and API breadth is reasonable => "easy".
  If a free developer edition is self-serve, classify it self_serve_free and call it easy even when
  production customer orgs need a paid plan; record that production gate in access_notes.
- paid_plan_required / admin_approval / app_review_required => "moderate" unless the API is also thin.
- official_mcp_server is a vendor-operated MCP endpoint. A community repo is NOT official; put it in
  community_mcp_servers.
- composio_toolkit_exists answers whether a matching toolkit is in Composio's catalog, independent of
  whether the vendor has an MCP server. Use the catalog evidence, not memory.
- needs_outreach=true only when a human must contact/apply to the vendor to get credentials or access.
"""

RESEARCHER_SYSTEM = """\
You are the RESEARCHER in a two-agent verification pipeline. You convert a supplied evidence bundle
into a single structured AppResearch record. Accuracy matters more than completeness. You have no
tools in this call; the pipeline has already searched and fetched the pages listed in SOURCES.

You must obey the CLAIM RULES and output contract exactly. Treat the evidence bundle as your only
source of truth. If a required search target is missing from the evidence, leave the field null and
record the gap in open_questions rather than filling it from memory.

""" + CLAIM_RULES + "\n\n" + RUBRIC + "\n\n" + OUTPUT_CONTRACT


def _fmt(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


def researcher_synthesis_prompt(
    *,
    app: str,
    category: str,
    hint: str | None,
    description: str | None,
    retrieved_at: str,
    evidence_bundle: str,
    search_log: list[dict[str, Any]],
) -> str:
    return f"""\
APP: {app}
CATEGORY: {category}
INPUT HINT: {hint or "(none)"}
INPUT DESCRIPTION: {description or "(none)"}
RETRIEVED_AT: {retrieved_at}

SEARCH PLAN ALREADY EXECUTED (do not re-run; analyze the evidence):
{_fmt(search_log)}

EVIDENCE BUNDLE:
{evidence_bundle}

Produce the full AppResearch JSON now. Use only the evidence above.
"""


def researcher_revision_prompt(
    *,
    app: str,
    category: str,
    retrieved_at: str,
    original_json: dict[str, Any],
    issues: list[dict[str, Any]],
    evidence_bundle: str,
    extra_note: str = "",
) -> str:
    return f"""\
APP: {app}
CATEGORY: {category}
RETRIEVED_AT: {retrieved_at}

You are revising a draft after independent verification. The verifier found the issues below.
Fix exactly those fields, using the evidence bundle. Keep claims that were already supported.
Do not invent evidence to satisfy an issue: if it cannot be resolved from the bundle, set the claim
to null/not_found, lower confidence, and add a precise open_question.
Do NOT set a supported claim to null merely because the verifier disagrees; re-check the cited
evidence and choose the value it best supports. For access_model, choose the lowest-friction
documented credential route (free developer edition -> self_serve_free; free trial -> self_serve_trial;
otherwise paid/gated models). For official_mcp_server, an official vendor documentation page for a
per-instance hosted MCP server is a valid value when no static endpoint is published.

VERIFIER ISSUES:
{_fmt(issues)}

CURRENT DRAFT:
{_fmt(original_json)}

EVIDENCE BUNDLE (includes any extra pages fetched for the failed fields):
{evidence_bundle}
{extra_note}

Return ONE JSON object in PATCH form (not the full record):
{{
  "updates": {{"<claim_field>": CLAIM, ...}},
  "buildability_verdict": "easy|moderate|hard|blocked",   // only if it must change
  "verdict_reason": "...",                                // only if it must change
  "main_blocker": "..." | null,                           // only if it must change
  "needs_outreach": true|false,                           // only if it must change
  "outreach_reason": "..." | null,                        // only if it must change
  "open_questions": ["..."]                               // only if it must change
}}
Only include claim fields that actually need a fix. Do not repeat unchanged claims. If you cannot
produce a safe patch, return the complete revised AppResearch object instead.
"""


CLAIM_JUDGE_SYSTEM = """\
You are an adversarial CLAIM VERIFIER. You did not write the research and you must not trust it.
For each claim you receive the researcher's value, source_url, quote, confidence, the mechanically
extracted page excerpt around the best quote match, and the quote-match score.

Judge the claim against the excerpt only. Do not use outside knowledge or assume a page says
something because it usually does. Verdicts:
- SUPPORTED: the excerpt (and quote) directly supports the claimed value.
- PARTIALLY: the excerpt supports part of the value or requires a small inference.
- UNSUPPORTED: the excerpt does not contain support for the value.
- CONTRADICTED: the excerpt contradicts the value.

A quote that exists but does not entail the value is UNSUPPORTED, not SUPPORTED.
For list claims, every item must be supported by the cited quote/excerpt.
Return ONE JSON object:
{"judgments":[{"field":"<field>","verdict":"SUPPORTED|PARTIALLY|UNSUPPORTED|CONTRADICTED","reason":"<one line>"}]}
"""


def claim_judge_prompt(app: str, claims: list[dict[str, Any]]) -> str:
    return f"""\
APP: {app}

CLAIMS TO JUDGE:
{_fmt(claims)}

Return the judgments JSON now, one entry per field, in the same order.
"""


REDERIVE_SYSTEM = """\
You are an INDEPENDENT verifier. You have NOT seen the researcher's answers for the six fields below.
Re-derive them from the evidence bundle alone. Do not assume the vendor has an API, an MCP server, or
self-serve access unless the evidence shows it. If the evidence is insufficient, use null / "unknown"
and say why. Obey the same access-model and verdict definitions as the researcher.

ACCESS_MODEL must be exactly one of:
self_serve_free, self_serve_trial, paid_plan_required, admin_approval, app_review_required,
partner_only, contact_sales, no_api, unknown.
- If a free trial or free developer edition is available without sales contact, use
  self_serve_trial or self_serve_free even when paid plans are also offered.
- Never return invented values such as "open_access" or "self_serve_paid".
- Use paid_plan_required only when no self-serve free/trial credential route is documented.

OFFICIAL_MCP_SERVER:
- Return the actual vendor-operated MCP endpoint if published.
- If the vendor documents an official hosted MCP server but endpoints are per-instance / generated in
  an admin console and no static endpoint is published, return the official VENDOR DOCUMENTATION URL
  for that server, not null.
- Community/third-party servers are null for this field (they belong in community_mcp_servers).
""" + "\n\n" + RUBRIC + """

Return ONE JSON object:
{
  "auth_methods": ["oauth2"|"api_key"|"basic"|"bearer_token"|"jwt"|"other", ...] or null,
  "access_model": one of ACCESS_MODELS exactly (snake_case; never invent values like
                  "open_access"; use self_serve_free for a free self-serve plan) or "unknown",
  "official_mcp_server": "<URL>" or null,
  "has_public_api": true|false|null,
  "buildability_verdict": "easy"|"moderate"|"hard"|"blocked",
  "needs_outreach": true|false,
  "reasoning": {"auth_methods":"...","access_model":"...","official_mcp_server":"...",
                "has_public_api":"...","buildability_verdict":"...","needs_outreach":"..."}
}
"""


def rederive_prompt(*, app: str, category: str, evidence_bundle: str) -> str:
    return f"""\
APP: {app}
CATEGORY: {category}

EVIDENCE BUNDLE:
{evidence_bundle}

Re-derive the six fields now from this evidence only.
"""


def targeted_search_queries(app: str, field: str) -> list[str]:
    """Deterministic follow-up queries for a weak/missing claim."""
    templates = {
        "auth_methods": [
            f'"{app}" API authentication OAuth API key documentation',
            f'"{app}" developer authentication guide',
        ],
        "access_model": [
            f'"{app}" API pricing plans access requirements',
            f'"{app}" developer API free tier paid plan',
        ],
        "official_mcp_server": [
            f'"{app}" official MCP server',
            f'"{app}" model context protocol server vendor',
        ],
        "has_public_api": [
            f'"{app}" public API documentation developer',
            f'"{app}" API reference',
        ],
        "rate_limits": [
            f'"{app}" API rate limits documentation',
        ],
        "webhooks": [
            f'"{app}" API webhooks events documentation',
        ],
        "sandbox_or_test_env": [
            f'"{app}" API sandbox test environment documentation',
        ],
        "sdk_languages": [
            f'"{app}" official SDK libraries languages',
        ],
        "docs_url": [
            f'"{app}" API documentation reference',
        ],
        "api_breadth": [
            f'"{app}" API endpoints resources reference',
        ],
        "api_versioning_or_deprecation_notes": [
            f'"{app}" API versioning deprecation policy',
        ],
    }
    return templates.get(field, [f'"{app}" {field.replace("_", " ")} API documentation'])
