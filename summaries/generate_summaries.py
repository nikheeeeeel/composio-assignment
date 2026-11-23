"""Generate detailed plain-text category and overall summaries from results/pass1.

Run:  .venv/bin/python summaries/generate_summaries.py
"""

from __future__ import annotations

import collections
import glob
import json
import statistics
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from schema import CLAIM_FIELDS, AppResearch  # noqa: E402

OUT = BASE / "summaries"
PASS1 = BASE / "results" / "pass1"

CATEGORY_FILES = {
    "CRM and Sales": ("01", "crm-and-sales"),
    "Support and Helpdesk": ("02", "support-and-helpdesk"),
    "Communications and Messaging": ("03", "communications-and-messaging"),
    "Marketing, Ads, Email and Social": ("04", "marketing-ads-email-and-social"),
    "Ecommerce": ("05", "ecommerce"),
    "Data, SEO and Scraping": ("06", "data-seo-and-scraping"),
    "Developer, Infra and Data platforms": ("07", "developer-infra-and-data-platforms"),
    "Productivity and Project Management": ("08", "productivity-and-project-management"),
    "Finance and Fintech": ("09", "finance-and-fintech"),
    "AI, Research and Media-native": ("10", "ai-research-and-media-native"),
}

VERDICT_ORDER = {"easy": 0, "moderate": 1, "hard": 2, "blocked": 3}

ANALYST_READ = {
    "CRM and Sales": """\
CRM is the most uniformly ready category: all ten apps use OAuth2, nine have an official
MCP server, and nine match a Composio toolkit. The two non-easy apps are held back by
commercial/administrative conditions rather than by the API itself. Close requires an
existing paid subscription before API keys can be created, and DealCloud requires an
existing site with site-admin-enabled API capability. Salesforce is the clearest example
of the "free developer edition" pattern: production use is edition-gated, but the free
Developer Edition makes the first integration self-serve, which is why the first pass
lands on easy rather than moderate.""",
    "Support and Helpdesk": """\
Support is split exactly down the middle: five self-serve/trial apps and five paid or
admin-gated apps. The striking pattern is MCP saturation - all ten have an official MCP
server - so MCP presence says nothing about buildability in this category. What differs
is credentialing: Front, Gorgias and Help Scout require a paid plan, while Gladly and
Pylon require administrator cooperation. Composio covers eight of ten; Gladly and
LiveAgent are catalog gaps.""",
    "Communications and Messaging": """\
Every communications app is easy, but this is the widest auth surface in the portfolio:
OAuth2, API keys, Basic, JWT and "other" all appear. WhatsApp is technically easy because
the developer/test path is self-serve, yet production requires Meta App Review and
per-message billing; that policy risk should be treated as a product constraint, not an
API detail. Telegram and Discord use bot tokens rather than OAuth, so a generic OAuth-only
toolkit layer would miss them.""",
    "Marketing, Ads, Email and Social": """\
This category splits cleanly into ad platforms versus email/marketing tools. All four ad
platforms (Google, Meta, LinkedIn, Pinterest) are app_review_required and account for most
of the friction; Mailchimp, Klaviyo, SendGrid and systeme.io are self-serve. GoHighLevel is
the paid-plan outlier. Rate-limit evidence is the weakest of any category (only four of ten
have a sourced rate-limit claim), because ad-platform limits are buried in policy and
access-level documentation rather than a single API reference page.""",
    "Ecommerce": """\
Ecommerce has the largest Composio coverage gap: only three of ten apps match a toolkit.
The API surface is OAuth2-heavy (seven of ten), but the story is gating. Amazon SP-API needs
app review and role approval, Squarespace needs app review plus a Commerce Advanced plan,
Salesforce Commerce Cloud needs a paid B2C Commerce org, Ecwid needs a paid store, and
fanbasis keeps its documentation behind a password. Shopify, WooCommerce, BigCommerce and
Gumroad are the clean self-serve core.""",
    "Data, SEO and Scraping": """\
Data/SEO is API-key country: eight of ten use API keys, seven are self-serve free, and most
vendors are developer-first. The exceptions are commercial or administrative: Ahrefs needs
a paid plan, Waterfall.io manually approves API access, and Sherlock is a CLI with no hosted
API. Eight have an official MCP server and seven are in Composio. Evidence quality is
strong, with 13.4 of 17 claims sourced on average.""",
    "Developer, Infra and Data platforms": """\
Developer infrastructure is the most API-native category: all ten are REST, all ten have an
official MCP server, nine are easy and nine are in Composio. Auth is broad but conventional
(OAuth2, bearer, API key, JWT). The only moderate is Datadog, where the first pass could not
establish whether a free/trial credential path exists. Sentry is easy because token auth is
self-serve even though OAuth app registration requires partnership approval.""",
    "Productivity and Project Management": """\
Productivity is the best-covered category: all ten are in Composio, all ten use OAuth2, nine
are easy and nine have an official MCP server. The only gate is Smartsheet, which requires a
Business/Enterprise plan for API access. Harvest lacks an official MCP server but is still
easy. This is where existing Composio catalog depth and API uniformity reinforce each
other, making it the best reference implementation for OAuth2 toolkits.""",
    "Finance and Fintech": """\
Finance is bifurcated. Developer-first payments and accounting (Stripe, Plaid, QuickBooks,
Xero, Binance, iPayX) are easy. Brex and Ramp are moderate because credentials require an
existing customer/admin or sandbox approval. PitchBook is hard and sales-led. Paygent
Connect is the weakest evidence in the entire portfolio: one official source page and only
nine of seventeen claims sourced, with the access model unresolved. Rate-limit evidence is
thin here (seven of ten sourced), and versioning evidence is the weakest of any category
(five of ten).""",
    "AI, Research and Media-native": """\
AI/media-native is the most heterogeneous category: five easy native-AI tools, three
moderate enterprise/usage-gated products, one hard enterprise API, and one blocked CLI.
MCP is strong (eight of ten) but not universal; NotebookLM and Mermaid CLI are missing.
Auth is mixed (API key seven, OAuth2 six, bearer six), and scopes/sandbox documentation is
often absent. This is the category most likely to change quarter to quarter because
preview APIs, prepaid credits and enterprise gating move quickly.""",
}

IMPLICATIONS = {
    "CRM and Sales": [
        "Prioritize the eight easy apps; they share OAuth2 and broad read-write CRM object coverage.",
        "DealCloud needs human/partner outreach and a custom toolkit; it has no Composio match.",
        "Close can be built, but onboarding must assume an existing paid customer, not a free developer.",
        "MCP is not a differentiator here (9/10); invest in object/field coverage and write safety instead.",
    ],
    "Support and Helpdesk": [
        "Start with Zendesk, Intercom, Freshdesk, Plain and LiveAgent.",
        "Treat Front, Gorgias and Help Scout as paid-customer integrations; there is no free build path.",
        "Gladly and Pylon need administrator cooperation; they are outreach/partner motions.",
        "Close the Composio gaps for Gladly and LiveAgent.",
    ],
    "Communications and Messaging": [
        "Build Slack, Twilio, Telegram and Discord first; all are self-serve.",
        "WhatsApp needs policy/compliance work (Meta App Review, billing) despite an easy first-pass verdict.",
        "Support bot-token auth as a first-class pattern; do not assume OAuth2 everywhere.",
        "MCP gaps (Aircall, Discord, Telegram) matter less than correct auth adapters.",
    ],
    "Marketing, Ads, Email and Social": [
        "Ship email/marketing toolkits first: Mailchimp, Klaviyo, SendGrid and systeme.io.",
        "Ad platforms need a compliance track (app review, business verification, use-case vetting).",
        "Collect rate-limit evidence before promising throughput; it is the weakest field in this category.",
        "Close the Composio gaps for GoHighLevel and systeme.io.",
    ],
    "Ecommerce": [
        "Shopify and WooCommerce first: both self-serve and high value.",
        "Amazon SP-API is a compliance project, not a quick toolkit.",
        "Close the seven-app Composio gap; ecommerce is the clearest catalog expansion opportunity.",
        "Budget human review for Salesforce Commerce Cloud, Squarespace and fanbasis.",
    ],
    "Data, SEO and Scraping": [
        "High-confidence build targets: DataForSEO, Apify, Firecrawl, Bright Data, Clay, MrScraper and SE Ranking.",
        "Ahrefs is paid-plan gated; Waterfall.io needs outreach; Sherlock is not a hosted API.",
        "API-key onboarding is the norm, so a shared key-connection flow pays off across the category.",
        "Close the Composio gaps for Waterfall.io, Sherlock (N/A) and SE Ranking.",
    ],
    "Developer, Infra and Data platforms": [
        "This is the best category for immediate toolkit expansion.",
        "Verify Datadog's free/trial path before committing; its access model is the only unknown here.",
        "Add MongoDB Atlas, the one Composio gap in an otherwise near-complete category.",
        "Reuse OAuth2 and service-token patterns across Cloudflare, Vercel, Supabase, Sentry and friends.",
    ],
    "Productivity and Project Management": [
        "Fastest wins: Linear, Notion, Asana, Airtable, Jira, Monday.com, ClickUp, Coda and Harvest.",
        "Smartsheet needs a paid-plan assumption; treat it as a later tier.",
        "Use this category as the reference implementation for OAuth2 toolkits.",
        "All ten already have Composio toolkits, so effort should go into depth and write actions, not discovery.",
    ],
    "Finance and Fintech": [
        "Build Stripe, Plaid, QuickBooks and Xero first; all are self-serve and well documented.",
        "Brex and Ramp require customer/partner context; do not promise self-serve onboarding.",
        "Paygent Connect needs human research; its moderate verdict is not reliable yet.",
        "PitchBook is a sales-led data contract, not a toolkit candidate in the near term.",
    ],
    "AI, Research and Media-native": [
        "Prioritize Consensus, Fathom, Grain, Reducto and YouTube Transcript.",
        "Devin and higgsfield are paid/prepaid; NotebookLM is enterprise plus preview API.",
        "Otter is sales-led; Mermaid CLI is not an API toolkit at all.",
        "Re-verify preview/enterprise terms before building; this category changes quickly.",
    ],
}


def load_rows() -> list[AppResearch]:
    rows = []
    for path in sorted(glob.glob(str(PASS1 / "*.json"))):
        rows.append(AppResearch.model_validate(json.loads(Path(path).read_text())))
    return rows


def names(rows, predicate) -> list[str]:
    return sorted(r.app for r in rows if predicate(r))


def bar(count: int, total: int, width: int = 20) -> str:
    n = round(width * count / total) if total else 0
    return "#" * n


def confidence_summary(rows) -> dict[str, int]:
    counts = collections.Counter()
    for r in rows:
        for field in CLAIM_FIELDS:
            counts[r.claim(field).confidence] += 1
    return dict(counts)


def evidence_stats(rows) -> dict:
    sourced = [sum(1 for f in CLAIM_FIELDS if r.claim(f).source_url) for r in rows]
    official = [sum(1 for s in r.audit.get("sources", []) if s.get("official")) for r in rows]
    return {
        "avg_sourced": round(statistics.mean(sourced), 1) if sourced else 0,
        "avg_official": round(statistics.mean(official), 1) if official else 0,
        "min_sourced": min(sourced) if sourced else 0,
        "min_official": min(official) if official else 0,
        "avg_openq": round(statistics.mean(len(r.open_questions) for r in rows), 1) if rows else 0,
        "max_openq": max((len(r.open_questions), r.app) for r in rows) if rows else (0, ""),
    }


def app_table(rows) -> str:
    header = (
        f"{'APP':<25} {'VERDICT':<9} {'ACCESS':<21} {'AUTH':<30} "
        f"{'MCP':<4} {'COMP':<5} {'OUT':<4} {'EVIDENCE':<9}"
    )
    lines = [header, "-" * len(header)]
    for r in sorted(rows, key=lambda x: (VERDICT_ORDER.get(x.buildability_verdict, 9), x.app.lower())):
        auth = ",".join(r.auth_methods.value or []) or "-"
        if len(auth) > 29:
            auth = auth[:26] + "..."
        comp = "Y" if r.composio_toolkit_exists.value else ("N" if r.composio_toolkit_exists.value is False else "?")
        mcp = "Y" if r.official_mcp_server.value else "-"
        out = "Y" if r.needs_outreach else "-"
        src = sum(1 for f in CLAIM_FIELDS if r.claim(f).source_url)
        off = sum(1 for s in r.audit.get("sources", []) if s.get("official"))
        access = str(r.access_model.value or "null")
        lines.append(
            f"{r.app:<25} {r.buildability_verdict:<9} {access:<21} {auth:<30} "
            f"{mcp:<4} {comp:<5} {out:<4} {f'{src}/{off}':<9}"
        )
    lines.append("")
    lines.append("Legend: MCP = vendor official MCP server; COMP = matching Composio toolkit;")
    lines.append("OUT = human outreach required; EVIDENCE = sourced claims / official pages fetched.")
    return "\n".join(lines)


def gated_details(rows) -> str:
    lines = []
    for r in sorted(rows, key=lambda x: (VERDICT_ORDER.get(x.buildability_verdict, 9), x.app.lower())):
        if r.buildability_verdict in ("moderate", "hard", "blocked") or r.needs_outreach:
            lines.append(
                f"- {r.app} [{r.buildability_verdict}, access={r.access_model.value or 'null'}, "
                f"outreach={'yes' if r.needs_outreach else 'no'}]: {r.main_blocker or 'no explicit blocker'}"
            )
    return "\n".join(lines) if lines else "- None in this category."


def write_category(cat: str, rows: list[AppResearch]) -> None:
    num, slug = CATEGORY_FILES[cat]
    easy = sum(1 for r in rows if r.buildability_verdict == "easy")
    moderate = sum(1 for r in rows if r.buildability_verdict == "moderate")
    hard = sum(1 for r in rows if r.buildability_verdict == "hard")
    blocked = sum(1 for r in rows if r.buildability_verdict == "blocked")
    access = collections.Counter(str(r.access_model.value or "null") for r in rows)
    auth = collections.Counter(v for r in rows for v in (r.auth_methods.value or []))
    combos = collections.Counter(tuple(sorted(r.auth_methods.value or [])) for r in rows)
    api_types = collections.Counter(str(r.api_type.value or "null") for r in rows)
    mcp_yes = names(rows, lambda r: bool(r.official_mcp_server.value))
    mcp_no = names(rows, lambda r: not r.official_mcp_server.value)
    comp_yes = names(rows, lambda r: r.composio_toolkit_exists.value is True)
    comp_no = names(rows, lambda r: r.composio_toolkit_exists.value is False)
    outreach = names(rows, lambda r: r.needs_outreach)
    conf = confidence_summary(rows)
    total_conf = sum(conf.values())
    ev = evidence_stats(rows)
    low_ev = [
        (r.app, sum(1 for f in CLAIM_FIELDS if r.claim(f).source_url),
         sum(1 for s in r.audit.get("sources", []) if s.get("official")))
        for r in rows
        if sum(1 for f in CLAIM_FIELDS if r.claim(f).source_url) < 10
        or sum(1 for s in r.audit.get("sources", []) if s.get("official")) < 3
    ]

    lines = [
        "=" * 100,
        f"CATEGORY {num} - {cat}",
        "=" * 100,
        f"Apps: {len(rows)} | Stage: first-pass draft (unverified) | Source: results/pass1/*.json",
        "Generated: 2026-11-23",
        "",
        "1. HEADLINE",
        f"   {easy} easy, {moderate} moderate, {hard} hard, {blocked} blocked.",
        f"   Easy: {', '.join(names(rows, lambda r: r.buildability_verdict == 'easy')) or 'none'}.",
        f"   Gated / blocked: {', '.join(names(rows, lambda r: r.buildability_verdict != 'easy')) or 'none'}.",
        "",
        "2. PATTERNS IN THE DATA",
        "   Verdict mix:",
        f"     easy      {easy:>2}  {bar(easy, len(rows))}",
        f"     moderate  {moderate:>2}  {bar(moderate, len(rows))}",
        f"     hard      {hard:>2}  {bar(hard, len(rows))}",
        f"     blocked   {blocked:>2}  {bar(blocked, len(rows))}",
        "",
        "   Access model mix:",
    ]
    for key, count in access.most_common():
        lines.append(f"     {key:<22} {count:>2}  {bar(count, len(rows))}")
    lines.extend(
        [
            "",
            f"   Auth methods (apps can have several): "
            + ", ".join(f"{k} {v}" for k, v in auth.most_common()),
            f"   Most common auth combination: "
            + (
                ", ".join(f"{'+'.join(k) or 'none'} x{v}" for k, v in combos.most_common(3))
                or "none"
            ),
            f"   API shape: " + ", ".join(f"{k} {v}" for k, v in api_types.most_common()),
            "",
            f"   Official MCP server: {len(mcp_yes)}/{len(rows)} "
            f"({', '.join(mcp_yes) or 'none'}).",
            f"   Missing official MCP: {', '.join(mcp_no) or 'none'}.",
            f"   Composio toolkit match: {len(comp_yes)}/{len(rows)} "
            f"({', '.join(comp_yes) or 'none'}).",
            f"   Composio gaps: {', '.join(comp_no) or 'none'}.",
            f"   Human outreach required: {len(outreach)}/{len(rows)} "
            f"({', '.join(outreach) or 'none'}).",
            "",
            f"   Evidence: {ev['avg_sourced']}/17 claims sourced on average "
            f"(min {ev['min_sourced']}); {ev['avg_official']} official pages/app "
            f"(min {ev['min_official']}); {ev['avg_openq']} open questions/app "
            f"(max {ev['max_openq'][0]} for {ev['max_openq'][1]}).",
            f"   Confidence mix: high {conf.get('high',0)} ({conf.get('high',0)/total_conf*100:.0f}%), "
            f"medium {conf.get('medium',0)} ({conf.get('medium',0)/total_conf*100:.0f}%), "
            f"low {conf.get('low',0)} ({conf.get('low',0)/total_conf*100:.0f}%), "
            f"not_found {conf.get('not_found',0)} ({conf.get('not_found',0)/total_conf*100:.0f}%).",
            "",
            "3. ANALYST READ",
        ]
    )
    for paragraph in ANALYST_READ[cat].split("\n\n"):
        lines.extend(f"   {text}" for text in paragraph.splitlines())
        lines.append("")
    lines.append("4. IMPLICATIONS FOR TOOLKIT BUILD")
    for item in IMPLICATIONS[cat]:
        lines.append(f"   - {item}")
    lines.extend(["", "5. APP-LEVEL TABLE", app_table(rows), "", "6. GATED / BLOCKED DETAIL"])
    lines.append(gated_details(rows))
    lines.extend(["", "7. WATCHLIST / CAVEATS"])
    if low_ev:
        for app, src, off in low_ev:
            page_word = "page" if off == 1 else "pages"
            lines.append(f"   - {app}: low evidence ({src}/17 claims sourced, {off} official {page_word}). Re-check before trusting the verdict.")
    else:
        lines.append("   - No app falls below the low-evidence threshold (10 sourced claims / 3 official pages).")
    unknown = names(rows, lambda r: r.access_model.value in (None, "unknown"))
    if unknown:
        lines.append(f"   - Unresolved access model: {', '.join(unknown)}.")
    if mcp_no:
        lines.append(f"   - No official MCP found: {', '.join(mcp_no)}. This is a coverage opening, not automatically a build blocker.")
    lines.append("   - First-pass only: quotes, gating and verdicts have not yet been mechanically/LLM verified.")
    lines.append("")
    (OUT / f"{num}-{slug}.txt").write_text("\n".join(lines), encoding="utf-8")


def write_overall(rows: list[AppResearch], categories: dict[str, list[AppResearch]]) -> None:
    easy = sum(1 for r in rows if r.buildability_verdict == "easy")
    moderate = sum(1 for r in rows if r.buildability_verdict == "moderate")
    hard = sum(1 for r in rows if r.buildability_verdict == "hard")
    blocked = sum(1 for r in rows if r.buildability_verdict == "blocked")
    access = collections.Counter(str(r.access_model.value or "null") for r in rows)
    auth = collections.Counter(v for r in rows for v in (r.auth_methods.value or []))
    api = collections.Counter(str(r.has_public_api.value) for r in rows)
    conf = confidence_summary(rows)
    total_conf = sum(conf.values())
    ev = evidence_stats(rows)
    mcp_yes = names(rows, lambda r: bool(r.official_mcp_server.value))
    comp_yes = names(rows, lambda r: r.composio_toolkit_exists.value is True)
    comp_no = names(rows, lambda r: r.composio_toolkit_exists.value is False)
    outreach = [(r.app, r.access_model.value, r.outreach_reason) for r in rows if r.needs_outreach]
    blocked_apps = names(rows, lambda r: r.buildability_verdict == "blocked")
    hard_apps = [(r.app, r.main_blocker) for r in rows if r.buildability_verdict == "hard"]
    unknown_access = names(rows, lambda r: r.access_model.value in (None, "unknown"))
    low_ev = [
        (r.app, sum(1 for f in CLAIM_FIELDS if r.claim(f).source_url),
         sum(1 for s in r.audit.get("sources", []) if s.get("official")))
        for r in rows
        if sum(1 for f in CLAIM_FIELDS if r.claim(f).source_url) < 10
        or sum(1 for s in r.audit.get("sources", []) if s.get("official")) < 3
    ]

    lines = [
        "=" * 100,
        "OVERALL TRENDS - 100 third-party apps (first-pass draft)",
        "=" * 100,
        "Source: results/pass1/*.json | Stage: first-pass, unverified | Generated: 2026-11-23",
        "",
        "1. HEADLINE",
        f"   {easy} easy, {moderate} moderate, {hard} hard, {blocked} blocked.",
        f"   {access.get('self_serve_free',0)+access.get('self_serve_trial',0)}/100 have a self-serve free or trial route.",
        f"   {len(mcp_yes)}/100 have an official vendor MCP server; {len(comp_yes)}/100 match a Composio toolkit.",
        f"   {len(outreach)}/100 need human outreach before credentials can be obtained.",
        "",
        "2. VERDICT & ACCESS MIX",
        f"   easy      {easy:>2}  {bar(easy, 100)}",
        f"   moderate  {moderate:>2}  {bar(moderate, 100)}",
        f"   hard      {hard:>2}  {bar(hard, 100)}",
        f"   blocked   {blocked:>2}  {bar(blocked, 100)}",
        "",
    ]
    for key, count in access.most_common():
        lines.append(f"   {key:<22} {count:>3}  {bar(count, 100)}")
    lines.extend(
        [
            "",
            "3. AUTH PATTERNS",
            "   " + ", ".join(f"{k} {v}" for k, v in auth.most_common()),
            f"   OAuth2 appears in {auth.get('oauth2',0)}/100 apps; API keys in {auth.get('api_key',0)}/100.",
            "   Most apps support more than one method; bearer/basic are often transport encodings of",
            "   the same API key or OAuth token rather than separate credential systems.",
            "",
            "4. MCP & COMPOSIO COVERAGE",
            f"   Official MCP: {len(mcp_yes)}/100.",
            f"   Composio toolkit: {len(comp_yes)}/100; gaps: {len(comp_no)}.",
            "   Category coverage (official MCP / Composio):",
        ]
    )
    for cat in CATEGORY_FILES:
        rs = categories[cat]
        m = sum(1 for r in rs if r.official_mcp_server.value)
        c = sum(1 for r in rs if r.composio_toolkit_exists.value is True)
        lines.append(f"     {cat:<45} MCP {m:>2}/10   Composio {c:>2}/10")
    lines.extend(
        [
            "",
            "5. FRICTION & OUTREACH",
            f"   No public API: {', '.join(blocked_apps) or 'none'}.",
            f"   Hard/sales-led: " + ", ".join(f"{a} ({b})" for a, b in hard_apps) + ".",
            f"   Outreach required ({len(outreach)}):",
        ]
    )
    for app, model, reason in outreach:
        lines.append(f"     - {app} [{model}]: {(reason or '')[:220]}")
    lines.extend(
        [
            "",
            "6. EVIDENCE QUALITY",
            f"   Claims: {len(rows)*len(CLAIM_FIELDS)} total; "
            f"high {conf.get('high',0)} ({conf.get('high',0)/total_conf*100:.0f}%), "
            f"medium {conf.get('medium',0)} ({conf.get('medium',0)/total_conf*100:.0f}%), "
            f"low {conf.get('low',0)} ({conf.get('low',0)/total_conf*100:.0f}%), "
            f"not_found {conf.get('not_found',0)} ({conf.get('not_found',0)/total_conf*100:.0f}%).",
            f"   Average sourced claims: {ev['avg_sourced']}/17; average official pages: {ev['avg_official']}/app.",
            f"   Rate limits sourced: {sum(1 for r in rows if r.claim('rate_limits').source_url)}/100; "
            f"versioning: {sum(1 for r in rows if r.claim('api_versioning_or_deprecation_notes').source_url)}/100; "
            f"webhooks true: {sum(1 for r in rows if r.webhooks.value is True)}/100; "
            f"sandbox true: {sum(1 for r in rows if r.sandbox_or_test_env.value is True)}/100; "
            f"OAuth scopes documented true: {sum(1 for r in rows if r.oauth_scopes_documented.value is True)}/100.",
            "",
            "7. CROSS-CATEGORY TABLE",
            f"{'CATEGORY':<45} {'EASY':>4} {'MOD':>4} {'HARD':>4} {'BLOCK':>5} {'MCP':>4} {'COMP':>4} {'OUT':>4}",
            "-" * 85,
        ]
    )
    for cat in CATEGORY_FILES:
        rs = categories[cat]
        lines.append(
            f"{cat:<45} "
            f"{sum(1 for r in rs if r.buildability_verdict=='easy'):>4} "
            f"{sum(1 for r in rs if r.buildability_verdict=='moderate'):>4} "
            f"{sum(1 for r in rs if r.buildability_verdict=='hard'):>4} "
            f"{sum(1 for r in rs if r.buildability_verdict=='blocked'):>5} "
            f"{sum(1 for r in rs if r.official_mcp_server.value):>4} "
            f"{sum(1 for r in rs if r.composio_toolkit_exists.value is True):>4} "
            f"{sum(1 for r in rs if r.needs_outreach):>4}"
        )
    lines.extend(
        [
            "",
            "8. SEVEN PATTERNS THAT MATTER",
            "   1) Self-serve is the default. 70/100 apps have a free or trial credential route. The",
            "      median app is not a sales problem; it is an OAuth/API-key onboarding problem.",
            "   2) Gating is commercial, not technical. 27 moderate apps are mostly held back by paid",
            "      plans, app review, or admin roles. Only 2 apps have no public API at all.",
            "   3) MCP is table stakes. 85/100 have an official MCP server, including 10/10 in support",
            "      and developer infra. MCP presence no longer differentiates; auth, breadth and writes do.",
            "   4) Composio coverage is uneven. 77/100 match a toolkit, but ecommerce is only 3/10 and",
            "      data/finance are 7/10. The gap is a catalog roadmap, not a market signal.",
            "   5) Auth is converging on OAuth2 + API key. OAuth2 72/100, API key 60/100. Bearer/basic",
            "      are mostly encodings; JWT is niche. A two-path auth layer covers most of the market.",
            "   6) Evidence gaps cluster in operational fields. Sandbox (38/100), OAuth scopes (59/100),",
            "      rate limits (75/100) and versioning (84/100) are the hardest claims to source.",
            "   7) First-pass verdicts are optimistic. 17% of claims are not_found and 3% are low;",
            "      the verifier should be expected to move some easy apps to moderate or human review.",
            "",
            "9. PRIORITIZATION",
            "   Tier 1 - build now (self-serve, broad, strong evidence):",
            "     CRM (8 easy), Communications (10), Developer infra (9), Productivity (9),",
            "     Data/SEO (7), Ecommerce self-serve core (5), Marketing email tools (5),",
            "     Finance developer-first (6), AI native tools (5), Support self-serve/trial (5).",
            "",
            "   Tier 2 - gated but viable (paid plan, app review, admin approval):",
            "     27 moderate apps; plan commercial/partner work rather than only engineering.",
            "",
            "   Tier 3 - outreach / deprioritize:",
            "     9 outreach apps (Amazon SP-API, Salesforce Commerce Cloud, Squarespace, Waterfall,",
            "     PitchBook, Pinterest, Otter, Ramp, LinkedIn Ads); hard: PitchBook, Otter;",
            "     blocked/no API: Mermaid CLI, Sherlock.",
            "",
            "10. CAVEATS",
            "   - This is pass 1 only. No mechanical quote/URL checks or adversarial LLM verification ran.",
            "   - GoHighLevel was re-researched after a docs-platform host (Stoplight) polluted the first",
            "     attempt; the bad draft is preserved at results/logs/gohighlevel.pass1-bad.json.",
            f"   - Low-evidence apps: {', '.join(a for a,_,_ in low_ev) or 'none'}.",
            f"   - Unresolved access model: {', '.join(unknown_access) or 'none'}.",
            "   - Treat 'easy' as an upper bound until verification; treat 'hard/blocked' as more stable",
            "     because they are usually backed by explicit sales/CLI evidence.",
            "",
        ]
    )
    (OUT / "00-overall-trends.txt").write_text("\n".join(lines), encoding="utf-8")


def _app_index(rows: list[AppResearch]) -> str:
    lines = [
        f"{'APP':<27} {'CATEGORY':<42} {'VERDICT':<9} {'ACCESS':<22} {'COMPOSIO':<9}",
        "-" * 112,
    ]
    for r in sorted(rows, key=lambda x: x.app.lower()):
        comp = "yes" if r.composio_toolkit_exists.value else ("no" if r.composio_toolkit_exists.value is False else "unknown")
        lines.append(
            f"{r.app:<27} {r.category:<42} {r.buildability_verdict:<9} "
            f"{str(r.access_model.value or 'null'):<22} {comp:<9}"
        )
    return "\n".join(lines)


def _strip_inner_banner(text: str) -> str:
    lines = text.rstrip().splitlines()
    if len(lines) >= 3 and set(lines[0].strip()) == {"="} and set(lines[2].strip()) == {"="}:
        lines = lines[3:]
    return "\n".join(lines).lstrip()


def write_combined(rows: list[AppResearch], categories: dict[str, list[AppResearch]]) -> None:
    overall = _strip_inner_banner((OUT / "00-overall-trends.txt").read_text(encoding="utf-8"))
    parts = [
        "=" * 110,
        "COMPLETE ANALYSIS - 100 THIRD-PARTY APPS FOR AGENT TOOLKITS",
        "=" * 110,
        "Generated: 2026-11-23",
        "Source: results/pass1/*.json (first-pass drafts, unverified)",
        "",
        "HOW TO READ THIS FILE",
        "  Part I is the portfolio-level view: verdict mix, access/auth patterns, MCP and",
        "  Composio coverage, evidence quality, cross-category table, seven key patterns,",
        "  prioritization and caveats.",
        "  Part II contains one section per category. Every category has the same structure:",
        "  headline, computed patterns, analyst read, build implications, app-level table,",
        "  gated/blocked detail, and watchlist/caveats.",
        "  The appendix is an alphabetical master index of all 100 apps.",
        "",
        "IMPORTANT: these are first-pass research drafts. They have not been mechanically",
        "quote-checked or adversarially verified. Treat 'easy' as an upper bound.",
        "",
        "CONTENTS",
        "  PART I    Overall trends and patterns",
    ]
    for cat, (num, slug) in CATEGORY_FILES.items():
        parts.append(f"  PART II  {num}  {cat}")
    parts.extend(
        [
            "  APPENDIX  Master app index (A-Z)",
            "",
            "",
            "#" * 110,
            "#  PART I - OVERALL TRENDS AND PATTERNS",
            "#" * 110,
            "",
            overall,
            "",
            "",
            "#" * 110,
            "#  PART II - CATEGORY DEEP DIVES",
            "#" * 110,
            "",
        ]
    )
    for cat, (num, slug) in CATEGORY_FILES.items():
        text = _strip_inner_banner((OUT / f"{num}-{slug}.txt").read_text(encoding="utf-8"))
        parts.extend(
            [
                "",
                "",
                "=" * 110,
                f"CATEGORY {num} OF 10 - {cat.upper()}",
                "=" * 110,
                "",
                text,
                "",
            ]
        )
    parts.extend(
        [
            "",
            "",
            "#" * 110,
            "#  APPENDIX - MASTER APP INDEX (A-Z)",
            "#" * 110,
            "",
            "All 100 apps in alphabetical order. COMPOSIO = matching toolkit in Composio's catalog.",
            "",
            _app_index(rows),
            "",
            "END OF COMPLETE ANALYSIS",
            "",
        ]
    )
    (OUT / "COMPLETE-ANALYSIS.txt").write_text("\n".join(parts), encoding="utf-8")


def write_index() -> None:
    lines = [
        "SUMMARIES INDEX",
        "================",
        "First-pass category analysis for the 100-app research set.",
        "Each category file contains: headline, computed patterns, analyst read, implications,",
        "app-level table, gated/blocked detail, and watchlist/caveats.",
        "",
        "00-overall-trends.txt",
        "COMPLETE-ANALYSIS.txt  (single combined file: Part I overall + Part II categories + appendix)",
    ]
    for cat, (num, slug) in CATEGORY_FILES.items():
        lines.append(f"{num}-{slug}.txt  ({cat})")
    lines.extend(
        [
            "",
            "GOOGLE SHEETS RESEARCH TAB (separate research pass)",
            "google-sheets-research-summary.txt  (detailed summary of the 'Research' tab in composio-research)",
            "google-sheets-research-data.json  (local snapshot of the 100 rows used for that summary)",
            "",
            "Regenerate with: .venv/bin/python summaries/generate_summaries.py",
            "Google Sheets summary: .venv/bin/python summaries/generate_google_sheets_summary.py",
            "Data: results/pass1/*.json for Part I/II; Google Sheets Research tab for the Sheets summary.",
            "",
        ]
    )
    (OUT / "README.txt").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    rows = load_rows()
    if len(rows) != 100:
        raise SystemExit(f"expected 100 pass1 files, found {len(rows)}")
    categories = collections.defaultdict(list)
    for r in rows:
        categories[r.category].append(r)
    missing = set(CATEGORY_FILES) - set(categories)
    if missing:
        raise SystemExit(f"missing categories: {missing}")
    for cat in CATEGORY_FILES:
        write_category(cat, categories[cat])
    write_overall(rows, categories)
    write_combined(rows, categories)
    write_index()
    print(f"wrote {len(CATEGORY_FILES)+3} files to {OUT}")


if __name__ == "__main__":
    main()
