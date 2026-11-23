"""Layer A: mechanical verification. No LLM.

Re-fetches every source URL, checks quote presence with fuzzy scoring, audits domains,
checks GitHub/MCP URLs, validates schema consistency, and returns an evidence map that
the adversarial LLM verifier consumes.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup
from rapidfuzz import fuzz

from composio_tools import ComposioRuntime, ResearchTools
from config import Settings
from evidence import app_domain, select_excerpt
from schema import CLAIM_FIELDS, AppResearch, Claim


@dataclass
class PageFetch:
    url: str
    status_code: int | None
    final_url: str | None
    title: str
    text: str
    ok: bool
    bad_reason: str | None
    via: str
    body_chars: int = 0

    def log(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "status_code": self.status_code,
            "final_url": self.final_url,
            "title": self.title[:200],
            "ok": self.ok,
            "bad_reason": self.bad_reason,
            "via": self.via,
            "body_chars": self.body_chars,
        }


LOGIN_PATTERNS = (
    "sign in to continue",
    "log in to continue",
    "please log in",
    "please sign in",
    "login required",
    "sign in required",
    "you must be logged in",
)
JS_PATTERNS = (
    "enable javascript",
    "javascript is required",
    "javascript must be enabled",
    "turn on javascript",
)
BLOCK_PATTERNS = (
    "access denied",
    "captcha",
    "are you a robot",
    "request blocked",
    "cloudflare",
)
PARKED_PATTERNS = ("domain is for sale", "buy this domain", "this domain may be for sale")

OFFICIAL_FIELDS = {
    "has_public_api",
    "api_type",
    "api_breadth",
    "docs_url",
    "auth_methods",
    "oauth_scopes_documented",
    "access_model",
    "access_notes",
    "official_mcp_server",
    "rate_limits",
    "webhooks",
    "sandbox_or_test_env",
    "sdk_languages",
    "api_versioning_or_deprecation_notes",
}

_VENDOR_STOP = {"the", "inc", "llc", "ltd", "com", "www", "app", "api", "crm", "cloud", "io", "ai", "business"}


def normalize_for_match(text: str) -> str:
    text = text.lower()
    text = text.replace("\u2019", "'").replace("\u2018", "'").replace("\u201c", '"').replace("\u201d", '"')
    text = text.replace("\u2013", "-").replace("\u2014", "-").replace("\u00a0", " ")
    text = re.sub(r"[`*_>#|]", " ", text)
    return " ".join(text.split())


def quote_score(quote: str | None, page_text: str) -> float:
    if not quote or not page_text:
        return 0.0
    q = normalize_for_match(quote)
    t = normalize_for_match(page_text)
    if not q or not t:
        return 0.0
    if q in t:
        return 100.0
    return float(fuzz.partial_ratio(q, t))


def excerpt_around(page_text: str, quote: str | None, max_chars: int = 1800) -> str:
    if not page_text:
        return ""
    if quote:
        alignment = fuzz.partial_ratio_alignment(quote, page_text)
        if alignment is not None and alignment.dest_end > alignment.dest_start:
            start = max(0, alignment.dest_start - 650)
            end = min(len(page_text), alignment.dest_end + 650)
            return page_text[start:end][:max_chars]
    return select_excerpt(page_text, max_chars)


def _extract_html(html: str) -> tuple[str, str]:
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    for tag in soup(["script", "style", "noscript", "svg", "nav", "footer", "header", "form"]):
        tag.decompose()
    text = soup.get_text("\n", strip=True)
    lines = [line.strip() for line in text.splitlines() if len(line.strip()) > 2]
    return title, "\n".join(lines)


def detect_bad_page(status: int, title: str, text: str, content_type: str) -> str | None:
    if status != 200:
        return f"http_{status}"
    if content_type and not any(k in content_type for k in ("html", "text", "json")):
        return f"content_type:{content_type.split(';')[0]}"
    low = f"{title}\n{text}".lower()
    if len(text) < 200:
        return "thin_page"
    if any(p in low for p in PARKED_PATTERNS):
        return "parked_domain"
    if any(p in low for p in JS_PATTERNS) and len(text) < 1500:
        return "js_shell"
    if any(p in low for p in LOGIN_PATTERNS) and len(text) < 3000:
        return "login_page"
    if any(p in low for p in BLOCK_PATTERNS) and len(text) < 1500:
        return "blocked_page"
    if re.search(r"\b404\b", title.lower()) or ("page not found" in low and len(text) < 1500):
        return "not_found_page"
    return None


async def probe_mcp_endpoint(url: str, settings: Settings) -> PageFetch:
    """Confirm an MCP URL exists even when plain GET is rejected (405/406/401).

    Streamable HTTP MCP endpoints often answer GET with 405 and require a POSTed
    JSON-RPC initialize. A JSON-RPC response or mcp-session-id header is positive
    evidence that the endpoint exists.
    """
    headers = {"User-Agent": "composio-research/1.0", "Accept": "application/json, text/event-stream"}
    try:
        async with httpx.AsyncClient(timeout=settings.http_timeout_seconds, follow_redirects=True) as client:
            get = await client.get(url, headers=headers)
            if get.status_code == 200 and len(get.text) > 200:
                title, text = _extract_html(get.text)
                return PageFetch(
                    url=url, status_code=200, final_url=str(get.url), title=title, text=text,
                    ok=True, bad_reason=None, via="httpx", body_chars=len(text),
                )
            payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "composio-research-probe", "version": "1.0"},
                },
            }
            post = await client.post(
                url,
                headers={**headers, "Content-Type": "application/json"},
                json=payload,
            )
            body = post.text[:4000]
            session_header = post.headers.get("mcp-session-id")
            looks_mcp = bool(
                session_header
                or '"jsonrpc"' in body
                or "event: message" in body.lower()
                or post.status_code in (200, 400, 401, 403, 405, 406)
            )
            return PageFetch(
                url=url,
                status_code=post.status_code,
                final_url=str(post.url),
                title="MCP endpoint probe",
                text=body,
                ok=looks_mcp,
                bad_reason=None if looks_mcp else f"http_{post.status_code}",
                via="mcp_probe",
                body_chars=len(body),
            )
    except Exception as exc:
        return PageFetch(
            url=url, status_code=None, final_url=None, title="", text="", ok=False,
            bad_reason=f"{type(exc).__name__}: {exc}"[:200], via="mcp_probe", body_chars=0,
        )


class PageFetcher:
    def __init__(self, settings: Settings, tools: ResearchTools | None = None):
        self.settings = settings
        self.tools = tools
        self.cache: dict[str, PageFetch] = {}

    async def fetch(self, url: str) -> PageFetch:
        if url in self.cache:
            return self.cache[url]
        page = await self._fetch_httpx(url)
        if (not page.ok or page.bad_reason) and self.tools is not None:
            try:
                docs = await self.tools.fetch([url], purpose="verification")
                if docs:
                    doc = docs[0]
                    page = PageFetch(
                        url=url,
                        status_code=200,
                        final_url=doc.url,
                        title=doc.title,
                        text=doc.text,
                        ok=True,
                        bad_reason=None,
                        via="composio_exa",
                        body_chars=len(doc.text),
                    )
            except Exception:
                pass
        self.cache[url] = page
        return page

    async def _fetch_httpx(self, url: str) -> PageFetch:
        headers = {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/125.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
        }
        try:
            async with httpx.AsyncClient(
                timeout=self.settings.http_timeout_seconds,
                follow_redirects=True,
                headers=headers,
            ) as client:
                response = await client.get(url)
            content_type = response.headers.get("content-type", "")
            title, text = _extract_html(response.text)
            if "json" in content_type:
                text = response.text
                title = "JSON response"
            bad = detect_bad_page(response.status_code, title, text, content_type)
            return PageFetch(
                url=url,
                status_code=response.status_code,
                final_url=str(response.url),
                title=title,
                text=text,
                ok=bad is None,
                bad_reason=bad,
                via="httpx",
                body_chars=len(text),
            )
        except Exception as exc:
            return PageFetch(
                url=url,
                status_code=None,
                final_url=None,
                title="",
                text="",
                ok=False,
                bad_reason=f"{type(exc).__name__}: {exc}"[:200],
                via="httpx",
                body_chars=0,
            )


def _vendor_tokens(app: str, hint: str | None, domain: str | None) -> set[str]:
    tokens = {t for t in re.split(r"[^a-z0-9]+", app.lower()) if len(t) > 2 and t not in _VENDOR_STOP}
    if domain:
        tokens.add(domain.split(".")[0])
    if hint and "github.com/" in hint:
        owner = hint.split("github.com/", 1)[1].split("/", 1)[0]
        tokens.add(owner.lower())
    return tokens


def _owner_is_vendor(owner: str | None, vendor_tokens: set[str]) -> bool:
    if not owner:
        return False
    owner_norm = re.sub(r"[^a-z0-9]+", "", owner.lower())
    for token in vendor_tokens:
        token_norm = re.sub(r"[^a-z0-9]+", "", token.lower())
        if token_norm and (token_norm in owner_norm or owner_norm in token_norm):
            return True
    return False


def _claim_value(research: AppResearch, field: str) -> Any:
    if field in ("buildability_verdict", "needs_outreach"):
        return getattr(research, field)
    return research.claim(field).value


def _add_issue(
    issues: list[dict[str, Any]], field: str, problem: str, severity: str, suggested_fix: str
) -> None:
    issues.append(
        {"field": field, "problem": problem, "severity": severity, "suggested_fix": suggested_fix}
    )


async def verify_mechanical(
    research: AppResearch,
    app: dict[str, Any],
    settings: Settings,
    runtime: ComposioRuntime,
    tools: ResearchTools | None = None,
    page_fetcher: PageFetcher | None = None,
) -> dict[str, Any]:
    domain = app_domain(app.get("hint"))
    vendor_tokens = _vendor_tokens(app["name"], app.get("hint"), domain)
    fetcher = page_fetcher or PageFetcher(settings, tools)
    issues: list[dict[str, Any]] = []
    claims_report: dict[str, Any] = {}
    github_report: dict[str, Any] = {}

    # Identity and completeness
    if research.app.strip().casefold() != app["name"].strip().casefold():
        _add_issue(
            issues, "app", f"researched app is {research.app!r}, expected {app['name']!r}",
            "critical", "Re-research the correct app; do not substitute a same-named product.",
        )
    if research.category.strip().casefold() != app["category"].strip().casefold():
        _add_issue(
            issues, "category", f"category is {research.category!r}, expected {app['category']!r}",
            "warning", "Use the supplied category verbatim.",
        )
    if len(research.verdict_reason.strip()) < 20:
        _add_issue(
            issues, "verdict_reason", "verdict_reason is too short to be auditable",
            "warning", "Cite the decisive evidence in at least one full sentence.",
        )
    if not isinstance(research.open_questions, list):
        _add_issue(
            issues, "open_questions", "open_questions must be a list",
            "critical", "Return a list of unresolved items (possibly empty).",
        )

    # Fetch every distinct source URL once.
    source_urls = []
    for field in CLAIM_FIELDS:
        url = research.claim(field).source_url
        if url and url not in source_urls and field != "composio_toolkit_exists":
            source_urls.append(url)
    pages = await asyncio.gather(*(fetcher.fetch(url) for url in source_urls))
    page_map = {page.url: page for page in pages}

    # Composio catalog special-case (API evidence, not a web page).
    catalog: dict[str, Any] | None = None
    try:
        catalog = await runtime.catalog_matches(app["name"], app.get("hint"))
    except Exception as exc:
        _add_issue(
            issues, "composio_toolkit_exists", f"catalog re-check failed: {exc}",
            "warning", "Re-run the Composio catalog check.",
        )

    quote_checked = 0
    quote_passed = 0
    high_medium_checked = 0
    high_medium_passed = 0

    for field in CLAIM_FIELDS:
        claim: Claim = research.claim(field)
        field_report: dict[str, Any] = {
            "confidence": claim.confidence,
            "source_url": claim.source_url,
            "value": claim.value,
            "quote": claim.quote,
        }

        if field == "composio_toolkit_exists":
            catalog_json = json.dumps(catalog or {}, ensure_ascii=False, default=str)
            score = quote_score(claim.quote, catalog_json) if claim.quote else 0.0
            field_report.update(
                {
                    "page_status": 200 if catalog is not None else None,
                    "page_title": "Composio toolkit catalog",
                    "fetched_via": "composio_catalog",
                    "quote_score": round(score, 2),
                    "quote_pass": score >= settings.quote_fuzzy_threshold * 100,
                    "excerpt": (catalog or {}).get("evidence_snippet", ""),
                    "catalog_exists": bool(catalog and catalog.get("exists")),
                }
            )
            if catalog is not None:
                if claim.value is True and not catalog.get("exists"):
                    _add_issue(
                        issues, field, "claim says a Composio toolkit exists, but the catalog has no match",
                        "critical", "Set the field false or cite the exact matching toolkit slug.",
                    )
                    field_report["contradicted"] = True
                elif claim.value is False and catalog.get("exists"):
                    _add_issue(
                        issues, field,
                        f"claim says no Composio toolkit exists, but catalog matches {catalog.get('matches')}",
                        "critical", "Correct the value and cite the matching toolkit.",
                    )
                    field_report["contradicted"] = True
                else:
                    field_report["verified_by_catalog"] = True
            claims_report[field] = field_report
            continue

        if not claim.source_url:
            field_report.update(
                {
                    "page_status": None,
                    "page_title": "",
                    "fetched_via": None,
                    "quote_score": 0.0,
                    "quote_pass": False,
                    "excerpt": "",
                }
            )
            if claim.confidence in ("high", "medium"):
                _add_issue(
                    issues, field, "high/medium confidence without a source URL",
                    "critical", "Add a real source+quote or downgrade to low/not_found.",
                )
            claims_report[field] = field_report
            continue

        page = page_map[claim.source_url]
        score = quote_score(claim.quote, page.text)
        passed = score >= settings.quote_fuzzy_threshold * 100
        field_report.update(
            {
                "page_status": page.status_code,
                "page_title": page.title,
                "final_url": page.final_url,
                "fetched_via": page.via,
                "page_ok": page.ok,
                "bad_reason": page.bad_reason,
                "quote_score": round(score, 2),
                "quote_pass": passed,
                "excerpt": excerpt_around(page.text, claim.quote),
                "body_chars": page.body_chars,
            }
        )

        if claim.source_url:
            quote_checked += 1
            if passed:
                quote_passed += 1
            if claim.confidence in ("high", "medium"):
                high_medium_checked += 1
                if passed:
                    high_medium_passed += 1

        if not page.ok:
            severity = "critical" if claim.confidence in ("high", "medium") else "warning"
            _add_issue(
                issues, field,
                f"source page is not usable ({page.bad_reason}); HTTP {page.status_code}",
                severity,
                "Find a fetchable official page for this claim or mark it not_found with an open question.",
            )
        if not passed:
            severity = "critical" if claim.confidence in ("high", "medium") else "warning"
            _add_issue(
                issues, field,
                f"quote not found in source (score {score:.1f} < {settings.quote_fuzzy_threshold * 100:.0f})",
                severity,
                "Replace the quote with a verbatim snippet from the cited page, or lower confidence.",
            )

        host = (urlparse(claim.source_url).hostname or "").lower()
        vendor_owned = bool(domain and (host == domain or host.endswith("." + domain)))
        github_owner = None
        github_vendor = False
        if host == "github.com":
            parts = [p for p in urlparse(claim.source_url).path.strip("/").split("/") if p]
            github_owner = parts[0] if parts else None
            github_vendor = _owner_is_vendor(github_owner, vendor_tokens)
        official_source = vendor_owned or github_vendor
        field_report["source_domain"] = host
        field_report["official_source"] = official_source
        if claim.confidence in ("high", "medium") and field in OFFICIAL_FIELDS and not official_source:
            _add_issue(
                issues, field,
                f"official claim cites a third-party source ({host})",
                "critical",
                "Use the vendor's own docs/pricing page for this claim, or lower confidence and note the third-party source.",
            )
        elif field in OFFICIAL_FIELDS and not official_source:
            _add_issue(
                issues, field,
                f"third-party source for an official field ({host})",
                "warning",
                "Prefer a vendor page; keep the third-party note in access_notes/open_questions.",
            )
        claims_report[field] = field_report

    # GitHub / MCP URL checks.
    for field in ("official_mcp_server", "community_mcp_servers"):
        claim = research.claim(field)
        if not claim.value:
            continue
        urls = claim.value if isinstance(claim.value, list) else [claim.value]
        for url in urls[:5]:
            if not isinstance(url, str) or not url.startswith(("http://", "https://")):
                _add_issue(
                    issues, field, f"invalid MCP URL: {url!r}", "warning",
                    "Use an absolute http(s) URL or null.",
                )
                continue
            host = (urlparse(url).hostname or "").lower()
            if host == "github.com":
                info = await runtime.github_repo(url)
                owner_vendor = _owner_is_vendor(info.get("owner"), vendor_tokens)
                github_report[url] = {**info, "owner_is_vendor": owner_vendor}
                if not info.get("exists"):
                    _add_issue(
                        issues, field, f"GitHub repo does not exist: {url}", "critical",
                        "Replace with an existing repo URL or remove it.",
                    )
                elif field == "official_mcp_server" and not owner_vendor:
                    _add_issue(
                        issues, field,
                        f"official MCP server is a GitHub repo owned by {info.get('owner')}, not the vendor",
                        "critical",
                        "Move community repos to community_mcp_servers; cite a vendor-operated server.",
                    )
            else:
                page = await probe_mcp_endpoint(url, settings)
                github_report[url] = {"exists": page.ok, "status": page.status_code, "via": page.via}
                if not page.ok:
                    _add_issue(
                        issues, field, f"MCP URL is not fetchable ({page.bad_reason})", 
                        "critical" if field == "official_mcp_server" else "warning",
                        "Replace with a live URL or mark the field not_found.",
                    )
                elif field == "official_mcp_server" and "mcp" not in page.text.lower():
                    _add_issue(
                        issues, field, "official MCP URL page does not mention MCP",
                        "warning", "Cite the vendor page that actually documents the MCP server.",
                    )

    # Internal consistency.
    def val(field: str) -> Any:
        return _claim_value(research, field)

    has_api = val("has_public_api")
    auth = val("auth_methods")
    access = val("access_model")
    verdict = research.buildability_verdict
    if has_api is False and auth:
        _add_issue(
            issues, "auth_methods", "has_public_api=false but auth_methods is populated",
            "critical", "Either the API exists (fix has_public_api) or auth_methods must be null.",
        )
    if has_api is False and verdict != "blocked":
        _add_issue(
            issues, "buildability_verdict", "has_public_api=false but verdict is not blocked",
            "critical", "A no-API app must be blocked.",
        )
    if has_api is True and access == "no_api":
        _add_issue(
            issues, "access_model", "has_public_api=true but access_model=no_api",
            "critical", "Pick the real access model.",
        )
    if access in ("partner_only", "contact_sales") and verdict in ("easy", "moderate"):
        _add_issue(
            issues, "buildability_verdict",
            f"access_model={access} but verdict={verdict}",
            "critical", "Partner/contact-sales access must be hard unless documented self-serve access exists.",
        )
    if access in ("partner_only", "contact_sales") and research.needs_outreach is False:
        _add_issue(
            issues, "needs_outreach", f"access_model={access} but needs_outreach=false",
            "critical", "Set needs_outreach=true and explain the contact step.",
        )
    if access in ("paid_plan_required", "admin_approval", "app_review_required") and verdict == "easy":
        _add_issue(
            issues, "buildability_verdict", f"access_model={access} but verdict=easy",
            "critical", "Gated access must be at least moderate.",
        )
    if access == "self_serve_free" and verdict == "blocked":
        _add_issue(
            issues, "buildability_verdict", "access_model=self_serve_free but verdict=blocked",
            "critical", "Self-serve API access contradicts a blocked verdict.",
        )
    if val("oauth_scopes_documented") is True and not (auth and "oauth2" in auth):
        _add_issue(
            issues, "oauth_scopes_documented", "scopes documented=true but auth_methods has no oauth2",
            "warning", "Only claim documented OAuth scopes if OAuth2 is an auth method.",
        )
    if val("sdk_languages") == []:
        _add_issue(
            issues, "sdk_languages", "empty list instead of null/not_found",
            "warning", "Use null with confidence=not_found when no official SDK is found.",
        )
    if research.needs_outreach and not research.outreach_reason:
        _add_issue(
            issues, "outreach_reason", "needs_outreach=true without a reason",
            "critical", "Explain the required human contact step.",
        )

    critical = [i for i in issues if i["severity"] == "critical"]
    warnings = [i for i in issues if i["severity"] == "warning"]
    quote_pass_pct = (quote_passed / quote_checked * 100.0) if quote_checked else 100.0
    high_medium_pct = (high_medium_passed / high_medium_checked * 100.0) if high_medium_checked else 100.0
    passed = not critical

    return {
        "passed": passed,
        "claims": claims_report,
        "pages": {url: page.log() for url, page in page_map.items()},
        "github": github_report,
        "issues": issues,
        "stats": {
            "claims_total": len(CLAIM_FIELDS),
            "claims_with_source": sum(1 for f in CLAIM_FIELDS if research.claim(f).source_url),
            "quote_checked": quote_checked,
            "quote_passed": quote_passed,
            "quote_pass_pct": round(quote_pass_pct, 2),
            "high_medium_checked": high_medium_checked,
            "high_medium_passed": high_medium_passed,
            "high_medium_quote_pass_pct": round(high_medium_pct, 2),
            "pages_fetched": len(page_map),
            "bad_pages": sum(1 for p in page_map.values() if not p.ok),
            "third_party_sources": sum(1 for r in claims_report.values() if r.get("source_domain") and not r.get("official_source")),
            "critical_issues": len(critical),
            "warnings": len(warnings),
        },
    }
