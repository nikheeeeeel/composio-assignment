"""Generate a detailed summary of the Google Sheets 'Research' tab.

Input: summaries/google-sheets-research-data.json (snapshot read through Composio MCP).
Output: summaries/google-sheets-research-summary.txt
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

BASE = Path(__file__).resolve().parent
DATA = BASE / "google-sheets-research-data.json"
OUT = BASE / "google-sheets-research-summary.txt"

CATEGORY_ORDER = [
    "CRM and Sales",
    "Support and Helpdesk",
    "Communications and Messaging",
    "Marketing, Ads, Email and Social",
    "Ecommerce",
    "Data, SEO and Scraping",
    "Developer, Infra and Data platforms",
    "Productivity and Project Management",
    "Finance and Fintech",
    "AI, Research and Media-native",
]

READS = {
    "CRM and Sales": """\
CRM is the most mature block in the tab: all ten apps are REST, all ten have an official
MCP server, OAuth2 appears on every app, and eight are self-serve. Seven are unconditionally
buildable. The friction is tenant administration rather than API absence: Salesforce is
limited because every customer org needs an admin-created External Client App and
Enterprise+ for production API access, while Copper is effectively a paid-seat product.
DealCloud is the one difficult app: enterprise-only, admin-enabled capability, and a
schema that differs per tenant.""",
    "Support and Helpdesk": """\
Support is one of the most consistently buildable categories: nine of ten are buildable,
nine have official MCP, and eight are self-serve. Seven are self-serve trials, so the common
qualifier is commercial rather than technical. Gladly is the sole difficult app because
onboarding is enterprise/account-team-led with no public self-serve credentials. OAuth2 and
API keys are equally common (nine each), making this a dual-auth category.""",
    "Communications and Messaging": """\
Eight of ten are buildable and two are buildable with limitations. Nine are self-serve, and
MCP is split between eight official and two community servers. Auth is the most mixed of any
category: OAuth2 (7), API keys (4), Basic (2), Bearer (2), JWT (1) and other (1). WhatsApp
Business is limited by business verification, template approval and the 24-hour messaging
window; Aircall is limited by a paid plan plus beta/read-only MCP. There are no difficult or
insufficient apps here.""",
    "Marketing, Ads, Email and Social": """\
Six are buildable, three are buildable with limitations, and one is difficult. The split is
ad platforms versus email/marketing tools: five apps are self-serve, but four require admin
approval. Google Ads, Meta Ads and Pinterest are limited by developer-token/app review,
access tiers and per-account permissions; LinkedIn Ads is difficult because the Marketing
Developer Platform requires vetting and has frequent API version sunsets. Email/marketing
tools are the clean buildable core. MCP is seven official and three community.""",
    "Ecommerce": """\
Six are buildable, three are limited and one is difficult. Seven are self-serve and nine have
official MCP; OAuth2 appears in nine of ten. Amazon Selling Partner is limited by developer
registration, app review and SigV4 signing; Squarespace is limited by a paid commerce plan
and thin MCP coverage; fanbasis is limited by a rebrand, thin docs and low vendor maturity.
Salesforce Commerce Cloud is difficult because of enterprise licensing, per-instance client
setup and MCP tooling still in preview/pilot. GraphQL appears in three ecommerce platforms.""",
    "Data, SEO and Scraping": """\
Six are buildable, three are limited and one is difficult. Only five are self-serve; four are
paid-plan and one is sales-gated. API keys dominate (eight of ten). Ahrefs is limited by an
enterprise plan and per-row unit costs; Clay is limited because the HTTP API is Enterprise
and MCP is workspace-scoped; Sherlock is limited because there is no hosted API and a toolkit
must wrap the CLI. Waterfall.io is difficult because access is sales-gated. MCP is eight
official, one community and one none.""",
    "Developer, Infra and Data platforms": """\
This is the cleanest category in the tab: ten of ten are buildable, ten are self-serve, and
ten have official MCP. OAuth2 (7) and Bearer (8) dominate; API keys (4) and JWT (1) are
secondary. REST is universal and GraphQL appears in three apps. There are no limitations, no
gated apps and no low-confidence entries. This is the best reference set for a reusable
toolkit pattern.""",
    "Productivity and Project Management": """\
Ten of ten are buildable and ten are self-serve; nine have official MCP and one has a
community server. OAuth2 appears in nine, API keys in six and Bearer in six. REST is present
in eight; the two "REST: No" entries (Linear and Monday.com) are GraphQL-first, not API-less.
That is an adapter-design issue, not a blocker. There are no limited, difficult or
insufficient apps in this category.""",
    "Finance and Fintech": """\
Five are buildable, two are limited, one is difficult and two are insufficient; only five are
self-serve. Auth is mixed (OAuth2 5, API key 3, other 3), REST is present in seven, and three
have no official MCP. Plaid is limited by production approval and per-item costs; Brex is
limited by customer-only access. PitchBook is difficult because it is an enterprise data
contract. Paygent Connect and iPayX are insufficient because public documentation is absent
or dead. This is the lowest-confidence category (three low-confidence apps).""",
    "AI, Research and Media-native": """\
Four are buildable, five are limited, one is insufficient, and none are difficult. Six are
self-serve; eight have official MCP. Auth is mixed (API key 5, Bearer 5, OAuth2 3). The
limited cluster is heterogeneous: Otter is Enterprise-gated, Fathom is MCP-first with no
public REST reference, Consensus has commercial access terms, Devin wraps an expensive agent
runtime, and higgsfield has marketing-level docs with costly generation credits. NotebookLM
is insufficient because no public API is documented. This is the category most likely to
change as preview APIs and prepaid models evolve.""",
}


def yn(value: str) -> str:
    low = (value or "").strip().lower()
    if low.startswith("yes"):
        return "Y"
    if low.startswith("no"):
        return "N"
    return "?"


def verdict_abbr(value: str) -> str:
    return {
        "Buildable": "B",
        "Buildable with limitations": "BL",
        "Difficult / gated": "DG",
        "Insufficient information": "II",
    }.get(value, "?")


def access_abbr(value: str) -> str:
    return {
        "Self-serve free": "Free",
        "Self-serve trial": "Trial",
        "Paid plan required": "Paid",
        "Admin approval required": "Admin",
        "Partnership / contact-sales required": "Sales",
        "Unknown": "Unknown",
    }.get(value, value or "?")


def mcp_abbr(value: str) -> str:
    return {"Official": "Off", "Community": "Com", "None": "None"}.get(value, value or "?")


def conf_abbr(value: str) -> str:
    return {"High": "H", "Medium": "M", "Low": "L"}.get(value, value or "?")


def load():
    data = json.loads(DATA.read_text(encoding="utf-8"))
    rows = []
    for line in data["table"]:
        parts = line.split("|", 10)
        if len(parts) != 11:
            continue
        rows.append(
            {
                "app": parts[0].strip(),
                "category": parts[1].strip(),
                "verdict": parts[2].strip(),
                "access": parts[3].strip(),
                "auth": parts[4].strip(),
                "rest_raw": parts[5].strip(),
                "graphql_raw": parts[6].strip(),
                "mcp": parts[7].strip(),
                "confidence": parts[8].strip(),
                "status": parts[9].strip(),
                "blocker": parts[10].strip(),
            }
        )
    return data["metadata"], data["stats"], rows


def cross_table(stats) -> list[str]:
    lines = [
        f"{'CATEGORY':<43} {'B':>3} {'BL':>3} {'DG':>3} {'II':>3} {'SELF':>5} {'MCP':>4} {'REST':>5} {'GQL':>4} {'HIGH':>5}",
        "-" * 92,
    ]
    for cat in CATEGORY_ORDER:
        c = stats["categories"][cat]
        lines.append(
            f"{cat:<43} "
            f"{c['buildable']:>3} {c['limited']:>3} {c['difficult']:>3} {c['insufficient']:>3} "
            f"{c['self_serve']:>5} {c['official_mcp']:>4} {c['rest'].get('yes',0):>5} "
            f"{c['graphql'].get('yes',0):>4} {c['confidence'].get('High',0):>5}"
        )
    return lines


def app_table(rows: list[dict]) -> list[str]:
    lines = [
        f"{'APP':<24} {'V':<3} {'ACCESS':<7} {'AUTH':<27} {'R':<1} {'G':<1} {'MCP':<4} {'C':<1}  BLOCKER",
        "-" * 118,
    ]
    rows_sorted = sorted(rows, key=lambda r: (CATEGORY_ORDER.index(r["category"]) if r["category"] in CATEGORY_ORDER else 99, r["app"].lower()))
    for r in rows_sorted:
        auth = r["auth"] or "-"
        if len(auth) > 26:
            auth = auth[:23] + "..."
        blocker = r["blocker"].replace("\n", " ")[:72]
        lines.append(
            f"{r['app']:<24} {verdict_abbr(r['verdict']):<3} {access_abbr(r['access']):<7} "
            f"{auth:<27} {yn(r['rest_raw']):<1} {yn(r['graphql_raw']):<1} "
            f"{mcp_abbr(r['mcp']):<4} {conf_abbr(r['confidence']):<1}  {blocker}"
        )
    return lines


def write_summary() -> None:
    meta, stats, rows = load()
    overall = stats["overall"]
    v = overall["verdict"]
    a = overall["access"]
    m = overall["mcp"]
    conf = overall["confidence"]
    total = len(rows)
    difficult = [r for r in rows if r["verdict"] == "Difficult / gated"]
    insufficient = [r for r in rows if r["verdict"] == "Insufficient information"]
    low_conf = [r for r in rows if r["confidence"] == "Low"]
    limited = [r for r in rows if r["verdict"] == "Buildable with limitations"]
    pilot = [r for r in rows if r["status"] != "Verified"]
    rest_no = [r for r in rows if yn(r["rest_raw"]) == "N"]
    rest_unknown = [r for r in rows if yn(r["rest_raw"]) == "?"]
    gql_yes = [r for r in rows if yn(r["graphql_raw"]) == "Y"]
    mcp_none = [r for r in rows if r["mcp"] == "None"]

    lines: list[str] = [
        "=" * 110,
        "GOOGLE SHEETS - 'composio-research' / 'Research' TAB",
        "SUMMARY OF THE 100-APP RESEARCH RESULTS",
        "=" * 110,
        f"Spreadsheet: {meta['spreadsheet']} ({meta['spreadsheet_id']})",
        f"Tab: {meta['tab']} | Apps: {meta['app_rows']} | Columns: {meta['columns']} | Rows incl. header: {meta['rows']}",
        f"Read via: {meta['read_via']}",
        f"Generated: {meta['generated']}",
        "Research status: 99 Verified, 1 Pilot - verified (Salesforce).",
        "",
        "IMPORTANT",
        "  This is a summary of the Research tab as it exists in Google Sheets. It is a",
        "  separate research pass from results/pass1 (different taxonomy and evidence cutoffs).",
        "  The tab's labels are Buildable / Buildable with limitations / Difficult-gated /",
        "  Insufficient information, not easy/moderate/hard/blocked.",
        "",
        "1. EXECUTIVE SNAPSHOT",
        f"   {v.get('Buildable',0)} Buildable | {v.get('Buildable with limitations',0)} Buildable with limitations | "
        f"{v.get('Difficult / gated',0)} Difficult / gated | {v.get('Insufficient information',0)} Insufficient information.",
        f"   73/100 have a self-serve free or trial route; 14 are paid-plan, 6 admin-approval, 5 partnership/contact-sales, 2 unknown.",
        f"   84/100 have an official MCP server; 10 more have community MCP; only 6 have none.",
        f"   REST is present in 90/100; GraphQL in 13/100; 6 report no REST, 4 unknown.",
        f"   Confidence: {conf.get('High',0)} High, {conf.get('Medium',0)} Medium, {conf.get('Low',0)} Low.",
        "",
        "2. VERDICT AND ACCESS MIX",
        "   Verdict",
        f"     Buildable                    {v.get('Buildable',0):>3}",
        f"     Buildable with limitations   {v.get('Buildable with limitations',0):>3}",
        f"     Difficult / gated            {v.get('Difficult / gated',0):>3}",
        f"     Insufficient information     {v.get('Insufficient information',0):>3}",
        "",
        "   Credential access",
        f"     Self-serve free              {a.get('Self-serve free',0):>3}",
        f"     Self-serve trial             {a.get('Self-serve trial',0):>3}",
        f"     Paid plan required           {a.get('Paid plan required',0):>3}",
        f"     Admin approval required      {a.get('Admin approval required',0):>3}",
        f"     Partnership / contact sales  {a.get('Partnership / contact-sales required',0):>3}",
        f"     Unknown                      {a.get('Unknown',0):>3}",
        "",
        "3. AUTH, API SHAPE AND MCP",
        "   Auth methods (apps can use several):",
        f"     OAuth2 {overall['auth'].get('oauth2',0)} | API key {overall['auth'].get('api_key',0)} | "
        f"Bearer {overall['auth'].get('bearer_token',0)} | Basic {overall['auth'].get('basic',0)} | "
        f"JWT {overall['auth'].get('jwt',0)} | other {overall['auth'].get('other',0)}",
        "   OAuth2 + API key is the dominant pairing. Bearer is often the transport for either.",
        "",
        "   API shape:",
        f"     REST: yes {overall['rest'].get('yes',0)}, no {overall['rest'].get('no',0)}, unknown {overall['rest'].get('unknown',0)}.",
        f"     GraphQL: yes {overall['graphql'].get('yes',0)}, no {overall['graphql'].get('no',0)}, unknown {overall['graphql'].get('unknown',0)}.",
        "   GraphQL-only apps (Plain, Linear, Monday.com) mean a REST-only toolkit layer would miss",
        "   them. CLI-first apps (Sherlock, Mermaid CLI) and MCP-first apps (Fathom) are also present.",
        "",
        "   MCP:",
        f"     Official {m.get('Official',0)} | Community {m.get('Community',0)} | None {m.get('None',0)}.",
        "   84/100 official means MCP is table stakes. The 10 community-only cases are vendor gaps,",
        "   not proof that the vendor has no MCP strategy.",
        "",
        "4. CONFIDENCE AND EVIDENCE",
        f"   High {conf.get('High',0)} | Medium {conf.get('Medium',0)} | Low {conf.get('Low',0)}.",
        f"   99 Verified + 1 Pilot - verified ({pilot[0]['app'] if pilot else 'none'}).",
        "   Low-confidence apps: " + (", ".join(f"{r['app']} ({r['category']})" for r in low_conf) or "none") + ".",
        "   The strongest evidence is in CRM, Support, Developer Infra and Productivity; the weakest",
        "   is Finance, where three apps are Low and two are Insufficient.",
        "",
        "5. CATEGORY-BY-CATEGORY",
        "",
    ]

    for idx, cat in enumerate(CATEGORY_ORDER, start=1):
        c = stats["categories"][cat]
        lines.extend(
            [
                "-" * 108,
                f"CATEGORY {idx:02d} - {cat}",
                "-" * 108,
                f"  Snapshot: {c['buildable']} Buildable | {c['limited']} Buildable with limitations | "
                f"{c['difficult']} Difficult / gated | {c['insufficient']} Insufficient.",
                f"  Access: {c['self_serve']}/10 self-serve free or trial; "
                f"official MCP {c['official_mcp']}/10; REST yes {c['rest'].get('yes',0)}/10; "
                f"GraphQL yes {c['graphql'].get('yes',0)}/10.",
                f"  Auth: " + ", ".join(f"{k} {v}" for k, v in c["auth"].items()) + ".",
                f"  Confidence: High {c['confidence'].get('High',0)}, Medium {c['confidence'].get('Medium',0)}, Low {c['confidence'].get('Low',0)}.",
                "",
            ]
        )
        for paragraph in READS[cat].splitlines():
            lines.append(f"  {paragraph}")
        lines.append("")
        cat_limited = [r for r in limited if r["category"] == cat]
        cat_difficult = [r for r in difficult if r["category"] == cat]
        cat_insufficient = [r for r in insufficient if r["category"] == cat]
        if cat_limited:
            lines.append("  Limited: " + "; ".join(f"{r['app']} ({r['access']}, {r['confidence']})" for r in cat_limited) + ".")
        if cat_difficult:
            lines.append("  Difficult: " + "; ".join(f"{r['app']} ({r['access']})" for r in cat_difficult) + ".")
        if cat_insufficient:
            lines.append("  Insufficient: " + "; ".join(f"{r['app']} ({r['access']}, {r['confidence']})" for r in cat_insufficient) + ".")
        lines.extend(["", ""])

    lines.extend(
        [
            "6. GATED, INSUFFICIENT AND LOW-CONFIDENCE APP DETAIL",
            "",
            "   Difficult / gated:",
        ]
    )
    for r in difficult:
        lines.append(f"     - {r['app']} [{r['category']} | {r['access']} | {r['confidence']}]: {r['blocker'][:240]}")
    lines.append("")
    lines.append("   Insufficient information:")
    for r in insufficient:
        lines.append(f"     - {r['app']} [{r['category']} | {r['access']} | {r['confidence']}]: {r['blocker'][:240]}")
    lines.append("")
    lines.append("   Low confidence (treat verdict as provisional):")
    for r in low_conf:
        lines.append(f"     - {r['app']} [{r['category']} | {r['verdict']} | {r['access']}]: {r['blocker'][:200]}")
    lines.extend(
        [
            "",
            "7. BLOCKER THEMES",
            "   Primary-blocker buckets (heuristic keyword classification):",
        ]
    )
    for key, count in sorted(stats["blocker_buckets"].items(), key=lambda x: -x[1]):
        lines.append(f"     {key:<24} {count:>3}")
    lines.extend(
        [
            "",
            "   Read: plan/payment (26) and review/approval (17) are the dominant frictions, followed",
            "   by admin/role (11) and sandbox/credentials (14). Only one blocker is explicitly about",
            "   rate limits; five blocker texts fall into the 'no public API' bucket, and some of those",
            "   still have GraphQL or CLI surfaces. The portfolio's problem is commercial/administrative",
            "   access, not the absence of APIs.",
            "",
            "8. NOTABLE API-SHAPE EXCEPTIONS",
            "   REST not present / unknown:",
        ]
    )
    for r in rest_no + rest_unknown:
        lines.append(f"     - {r['app']:<24} REST={r['rest_raw']:<40} GraphQL={r['graphql_raw']:<28} verdict={r['verdict']}")
    lines.append("")
    lines.append("   GraphQL-positive apps:")
    lines.append("     " + ", ".join(r["app"] for r in gql_yes) + ".")
    lines.append("")
    lines.append("   No MCP at all:")
    lines.append("     " + ", ".join(f"{r['app']} ({r['verdict']})" for r in mcp_none) + ".")
    lines.extend(
        [
            "",
            "9. CROSS-CHECK AGAINST results/pass1",
            "   Pass1 used easy/moderate/hard/blocked: 69 / 27 / 2 / 2.",
            "   Research tab uses Buildable/limitations/gated/insufficient: 71 / 20 / 6 / 3.",
            "   Both agree on the big picture: self-serve dominates (70 vs 73), official MCP is near-",
            "   universal (85 vs 84), and only a small minority are truly blocked or insufficient.",
            "   The tab is more conservative and more explicit about limitations. App-level differences",
            "   are expected: Salesforce is limited here vs easy in pass1; Mermaid CLI is buildable here",
            "   (CLI wrapper) vs blocked in pass1; Paygent, iPayX and NotebookLM are insufficient here",
            "   vs moderate/easy/moderate in pass1; Sherlock is limited here vs blocked in pass1.",
            "",
            "10. CAVEATS",
            "   - Auth is free text in the sheet; the OAuth2/API-key/Bearer counts are keyword-normalized.",
            "   - 'Buildable' is not the same as 'hosted REST API exists': Mermaid CLI and Sherlock are",
            "     CLI/library wrappers, and Plain/Linear/Monday.com are GraphQL-first.",
            "   - Primary-blocker buckets are heuristic and can overlap (e.g., paid plan + admin role).",
            "   - Low-confidence and insufficient apps should be re-checked before build commitments.",
            "   - The tab reports 99 Verified + 1 Pilot; verification depth is not uniform across rows.",
            "",
            "11. APPENDIX - FULL 100-APP TABLE (from the Research tab)",
            "   Abbreviations: V=B buildable, BL=buildable with limitations, DG=difficult/gated,",
            "   II=insufficient. R/G = REST/GraphQL yes/no/unknown. MCP Off/Com/None. C=confidence H/M/L.",
            "",
        ]
    )
    lines.extend(app_table(rows))
    lines.extend(
        [
            "",
            "END OF GOOGLE SHEETS RESEARCH SUMMARY",
            "",
        ]
    )
    OUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {OUT} ({len(lines)} lines)")


if __name__ == "__main__":
    write_summary()
