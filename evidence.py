"""Build compact, citation-friendly evidence bundles and detect official domains."""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlparse

from composio_tools import EvidenceDoc, SearchHit, normalize_text

KEYWORDS = {
    "api",
    "developer",
    "documentation",
    "docs",
    "reference",
    "endpoint",
    "authentication",
    "auth",
    "oauth",
    "oauth2",
    "api key",
    "apikey",
    "bearer",
    "token",
    "scope",
    "permission",
    "pricing",
    "price",
    "plan",
    "tier",
    "free",
    "trial",
    "enterprise",
    "contact sales",
    "sales",
    "partner",
    "approval",
    "review",
    "rate limit",
    "ratelimit",
    "throttle",
    "quota",
    "webhook",
    "event",
    "callback",
    "sandbox",
    "test environment",
    "staging",
    "sdk",
    "library",
    "client",
    "version",
    "deprecat",
    "sunset",
    "mcp",
    "model context protocol",
    "graphql",
    "rest",
    "soap",
    "public api",
    "private api",
    "access",
}

PURPOSE_ORDER = {
    "docs": 0,
    "auth": 1,
    "pricing": 2,
    "rate_limits": 3,
    "mcp": 4,
    "webhooks": 5,
    "sandbox": 6,
    "sdk": 7,
    "versioning": 8,
    "batch_docs": 9,
    "refine": 10,
    "verification": 11,
}


# Hosted API-docs platforms. A hint on one of these is a *docs host*, not the
# vendor's own domain; restricting searches to the platform root is wrong.
DOCS_PLATFORM_HOSTS = (
    "stoplight.io",
    "readme.io",
    "gitbook.io",
    "mintlify.app",
    "docusaurus.io",
    "docsify.js.org",
)


def _host_is_docs_platform(host: str) -> bool:
    return any(host == p or host.endswith("." + p) for p in DOCS_PLATFORM_HOSTS)


def app_domain(hint: str | None, fallback_url: str | None = None) -> str | None:
    candidates = [hint or "", fallback_url or ""]
    for candidate in candidates:
        candidate = candidate.strip()
        if not candidate:
            continue
        if not candidate.startswith(("http://", "https://")):
            if "." not in candidate:
                continue
            candidate = "https://" + candidate
        host = (urlparse(candidate).hostname or "").lower()
        if not host or _host_is_docs_platform(host):
            continue
        parts = host.split(".")
        if len(parts) >= 2:
            return ".".join(parts[-2:])
    return None


def _host_matches_app(host: str, app: str | None) -> bool:
    if not app:
        return False
    app_norm = normalize_text(app).replace(" ", "")
    for label in host.split("."):
        label = label.replace("-", "")
        if len(label) >= 4 and (label in app_norm or app_norm in label):
            return True
    return False


def is_official_url(url: str, domain: str | None, app: str | None = None) -> bool:
    host = (urlparse(url).hostname or "").lower()
    if not host:
        return False
    if domain and (host == domain or host.endswith("." + domain)):
        return True
    if _host_matches_app(host, app):
        return True
    if host in ("github.com", "raw.githubusercontent.com"):
        return True
    return False


def official_score(url: str, domain: str | None, app: str) -> int:
    host = (urlparse(url).hostname or "").lower()
    if domain and (host == domain or host.endswith("." + domain)):
        if host.startswith(("docs.", "developer.", "developers.", "api.")):
            return 100
        return 90
    if host in ("github.com", "raw.githubusercontent.com"):
        path = (urlparse(url).path or "").lower()
        app_tokens = [t for t in normalize_text(app).split() if len(t) > 2]
        if any(t in path for t in app_tokens):
            return 70
        return 40
    if _host_matches_app(host, app):
        return 80
    if host in (
        "docs.github.com",
        "learn.microsoft.com",
        "developers.google.com",
        "developer.mozilla.org",
    ):
        return 50
    return 0


def _paragraphs(text: str) -> list[str]:
    parts = re.split(r"\n\s*\n|(?<=[.!?])\s+(?=[A-Z0-9])", text)
    out = []
    for part in parts:
        part = " ".join(part.split())
        if len(part) >= 40:
            out.append(part)
    return out or [" ".join(text.split())]


def select_excerpt(text: str, max_chars: int) -> str:
    text = text.strip()
    if len(text) <= max_chars:
        return text
    paragraphs = _paragraphs(text)
    scored = []
    for index, para in enumerate(paragraphs):
        low = para.lower()
        score = sum(3 if " " in kw else 1 for kw in KEYWORDS if kw in low)
        if index < 2:
            score += 4
        if re.match(r"^#{1,3}\s", para):
            score += 2
        scored.append((score, index, para))
    chosen: list[tuple[int, str]] = []
    used = 0
    for score, index, para in sorted(scored, key=lambda x: (-x[0], x[1])):
        if used >= max_chars:
            break
        remaining = max_chars - used
        if len(para) > remaining:
            cut = para[:remaining]
            boundary = max(cut.rfind(". "), cut.rfind("\n"))
            if boundary > remaining * 0.5:
                cut = cut[: boundary + 1]
            para = cut
        if not para:
            continue
        chosen.append((index, para))
        used += len(para) + 1
    chosen.sort(key=lambda x: x[0])
    excerpt = "\n".join(para for _, para in chosen)
    return excerpt[:max_chars]


def _fmt_json(value: Any, limit: int = 6000) -> str:
    text = json.dumps(value, ensure_ascii=False, indent=1, default=str)
    return text[:limit]


def build_evidence_bundle(
    *,
    app: str,
    category: str,
    hint: str | None,
    description: str | None,
    retrieved_at: str,
    docs: list[EvidenceDoc],
    hits: list[SearchHit],
    catalog: dict[str, Any] | None,
    github: list[dict[str, Any]],
    registry: list[dict[str, Any]],
    gaps: list[str],
    query_log: list[dict[str, Any]],
    budget: int,
) -> str:
    domain = app_domain(hint)
    ordered_docs = sorted(
        docs,
        key=lambda d: (
            0 if d.official else 1,
            PURPOSE_ORDER.get(d.purpose, 50),
            0 if d.url.startswith("https://") else 1,
        ),
    )
    by_url: dict[str, EvidenceDoc] = {}
    for doc in ordered_docs:
        if doc.url not in by_url or len(doc.text) > len(by_url[doc.url].text):
            by_url[doc.url] = doc
    ordered_docs = list(by_url.values())[:10]

    head: list[str] = [
        f"APP: {app}",
        f"CATEGORY: {category}",
        f"INPUT HINT: {hint or '(none)'}",
        f"INPUT DESCRIPTION: {description or '(none)'}",
        f"RETRIEVED_AT: {retrieved_at}",
        "",
        "SEARCH INDEX (queries executed; use SOURCES text for quotes):",
    ]
    for row in query_log:
        head.append(
            f"- purpose={row.get('purpose')} query={row.get('query')!r} "
            f"results={row.get('result_count')} top={row.get('top_urls')}"
        )

    head.extend(["", "SEARCH SNIPPETS (context only; NOT valid for quotes):"])
    seen_snips = set()
    for hit in hits:
        if hit.url in seen_snips:
            continue
        seen_snips.add(hit.url)
        head.append(f"- [{hit.purpose}] {hit.title} | {hit.url} | {hit.snippet[:200]}")
        if len(seen_snips) >= 20:
            break

    # Catalog/GitHub/registry/gaps are deliberately BEFORE the page text so they can
    # never be truncated away by long source excerpts.
    head.extend(["", "COMPOSIO CATALOG CHECK (authoritative for composio_toolkit_exists):"])
    if catalog:
        head.append(f"catalog_url: {catalog.get('catalog_url')}")
        head.append(f"toolkit_exists: {catalog.get('exists')}")
        head.append(f"evidence_snippet: {catalog.get('evidence_snippet')}")
        head.append(_fmt_json(catalog.get("matches") or [], 4500))
    else:
        head.append("(catalog check unavailable)")

    head.extend(["", "GITHUB REPOSITORIES CHECKED:"])
    head.append(_fmt_json(github, 3500) if github else "(none)")
    head.extend(["", "MCP REGISTRY RESULTS:"])
    head.append(_fmt_json(registry[:6], 3000) if registry else "(none)")
    head.extend(["", "KNOWN GAPS / SEARCH PROBLEMS:"])
    head.extend(f"- {gap}" for gap in gaps) if gaps else head.append("- none recorded")
    head_text = "\n".join(head)

    source_budget = max(6000, budget - len(head_text) - 800)
    per_doc = max(1000, min(6000, source_budget // max(1, len(ordered_docs))))

    def source_lines(width: int) -> list[str]:
        out = ["", "SOURCES (cite source_url exactly as shown):"]
        for index, doc in enumerate(ordered_docs, start=1):
            out.extend(["", f"[S{index}] URL: {doc.url}"])
            out.append(f"TITLE: {doc.title}")
            out.append(
                f"PURPOSE: {doc.purpose} | OFFICIAL: {'yes' if doc.official else 'no'} | VIA: {doc.fetched_via}"
            )
            if doc.published_date:
                out.append(f"PUBLISHED: {doc.published_date}")
            out.extend(["TEXT:", select_excerpt(doc.text, width)])
        return out

    bundle = head_text + "\n" + "\n".join(source_lines(per_doc))
    if len(bundle) > budget:
        over = len(bundle) - budget
        per_doc = max(700, per_doc - over // max(1, len(ordered_docs)) - 50)
        bundle = head_text + "\n" + "\n".join(source_lines(per_doc))
    if len(bundle) > budget:
        bundle = bundle[:budget] + "\n\n[BUNDLE TRUNCATED AT BUDGET]"
    return bundle
