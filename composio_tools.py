"""Composio SDK + MCP tool layer.

Primary transport for app research is the session MCP URL (direct-tools preset), which
exposes exactly EXA_SEARCH and EXA_GET_CONTENTS_ACTION. Every call automatically falls
back to the SDK session execution path if MCP is unavailable, and the fallback is logged.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote_plus, urlparse

import httpx
from bs4 import BeautifulSoup
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_attempt, wait_exponential

from config import Settings


class ToolError(RuntimeError):
    pass


class ToolBudgetExceeded(RuntimeError):
    pass


class TransientToolError(ToolError):
    pass


@dataclass(slots=True)
class ToolBudget:
    limit: int
    used: int = 0

    def spend(self, tool: str) -> None:
        if self.used >= self.limit:
            raise ToolBudgetExceeded(f"tool-call cap reached ({self.limit}); stopping with gaps")
        self.used += 1


@dataclass(slots=True)
class ToolCall:
    seq: int
    tool: str
    source: str
    purpose: str
    arguments: dict[str, Any]
    ok: bool
    duration_s: float
    result_count: int | None = None
    error: str | None = None

    def log(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "tool": self.tool,
            "source": self.source,
            "purpose": self.purpose,
            "arguments": self.arguments,
            "ok": self.ok,
            "duration_s": round(self.duration_s, 3),
            "result_count": self.result_count,
            "error": self.error,
        }


@dataclass(slots=True)
class EvidenceDoc:
    url: str
    title: str
    text: str
    purpose: str
    fetched_via: str
    official: bool = False
    published_date: str | None = None

    def log(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "title": self.title,
            "purpose": self.purpose,
            "fetched_via": self.fetched_via,
            "official": self.official,
            "published_date": self.published_date,
            "chars": len(self.text),
        }


@dataclass(slots=True)
class SearchHit:
    title: str
    url: str
    snippet: str
    purpose: str
    rank: int

    def log(self) -> dict[str, Any]:
        return {"title": self.title, "url": self.url, "purpose": self.purpose, "rank": self.rank}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_text(value: str) -> str:
    value = value.lower().replace("&", " and ")
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return " ".join(value.split())


_STOP = {"the", "and", "inc", "llc", "ltd", "com", "www", "app", "api", "crm", "cloud", "io", "ai"}


def _tokens(value: str) -> set[str]:
    return {t for t in normalize_text(value).split() if len(t) > 1 and t not in _STOP}


def _clean_app_name(app: str) -> str:
    app = re.sub(r"\(.*?\)", " ", app)
    return " ".join(app.split())


def _unwrap_payload(payload: Any) -> dict[str, Any]:
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ToolError(f"tool returned non-JSON text: {payload[:300]}") from exc
    if not isinstance(payload, dict):
        raise ToolError(f"unexpected tool payload type: {type(payload).__name__}")
    if payload.get("successful") is False or payload.get("error"):
        raise ToolError(str(payload.get("error") or "tool reported failure")[:800])
    data = payload.get("data", payload)
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError:
            return {"text": data}
    return data if isinstance(data, dict) else {"data": data}


def _retryable(message: str) -> bool:
    lowered = message.lower()
    return any(marker in lowered for marker in ("429", "rate limit", "timeout", "timed out", "500", "502", "503", "504", "temporarily"))


async def _run_with_retries(fn, *, attempts: int = 4):
    async for attempt in AsyncRetrying(
        stop=stop_after_attempt(attempts),
        wait=wait_exponential(multiplier=1.5, min=2, max=25),
        retry=retry_if_exception_type(TransientToolError),
        reraise=True,
    ):
        with attempt:
            return await fn()
    raise ToolError("unreachable")


class ComposioRuntime:
    """Shared Composio connection/auth/catalog state for the whole run."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._client = None
        self.auth_config_id: str | None = None
        self.connected_account_id: str | None = None
        self._catalog: list[dict[str, Any]] | None = None
        self._catalog_lock = asyncio.Lock()
        self._github_cache: dict[str, dict[str, Any]] = {}

    @property
    def client(self):
        if self._client is None:
            from composio import Composio

            self._client = Composio(api_key=self.settings.composio_api_key)
        return self._client

    async def start(self) -> None:
        await asyncio.to_thread(self._ensure_exa_connection)

    def _ensure_exa_connection(self) -> None:
        from composio.types import auth_scheme

        if not self.settings.exa_api_key:
            raise ToolError("EXA_API_KEY is not set")
        configs = self.client.auth_configs.list(search="exa", show_disabled=True)
        config = next((c for c in configs.items if c.toolkit.slug == "exa"), None)
        if config is None:
            config = self.client.auth_configs.create(
                toolkit="exa",
                options={
                    "type": "use_custom_auth",
                    "auth_scheme": "API_KEY",
                    "name": "Exa Research Key",
                    "credentials": {},
                },
            )
        self.auth_config_id = config.id
        accounts = self.client.connected_accounts.list(
            toolkit_slugs=["exa"], user_ids=[self.settings.user_id]
        )
        active = [a for a in accounts.items if str(a.status) == "ACTIVE"]
        if active:
            self.connected_account_id = active[0].id
            return
        connection = self.client.connected_accounts.initiate(
            user_id=self.settings.user_id,
            auth_config_id=config.id,
            config=auth_scheme.api_key({"api_key": self.settings.exa_api_key}),
        )
        self.connected_account_id = connection.id

    async def create_session(self) -> "SessionHandle":
        session = await asyncio.to_thread(self._create_session_sync)
        return SessionHandle(self, session)

    def _create_session_sync(self):
        from composio import SESSION_PRESET_DIRECT_TOOLS

        if not self.auth_config_id:
            raise ToolError("Composio runtime not started")
        return self.client.sessions.create(
            user_id=self.settings.user_id,
            toolkits=["exa"],
            auth_configs={"exa": self.auth_config_id},
            tools={"exa": {"enable": ["EXA_SEARCH", "EXA_GET_CONTENTS_ACTION"]}},
            session_preset=SESSION_PRESET_DIRECT_TOOLS,
            manage_connections=False,
            mcp=True,
        )

    async def catalog(self) -> list[dict[str, Any]]:
        async with self._catalog_lock:
            if self._catalog is not None:
                return self._catalog
            self._catalog = await asyncio.to_thread(self._fetch_catalog)
            return self._catalog

    def _fetch_catalog(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        cursor = None
        while True:
            query: dict[str, Any] = {"limit": 1000, "include_deprecated": True}
            if cursor:
                query["cursor"] = cursor
            page = self.client.client.get("/api/v3.1/toolkits", cast_to=object, query=query)
            items.extend(page.get("items") or [])
            cursor = page.get("next_cursor")
            if not cursor:
                break
        return items

    async def catalog_matches(self, app: str, hint: str | None) -> dict[str, Any]:
        catalog = await self.catalog()
        cleaned = normalize_text(_clean_app_name(app))
        app_tokens = _tokens(app)
        hint_tokens = _tokens(hint or "")
        matches: list[dict[str, Any]] = []
        for item in catalog:
            name = item.get("name") or ""
            slug = item.get("slug") or ""
            app_name = item.get("app") or ""
            hay = " ".join((name, slug, app_name))
            norm = normalize_text(hay)
            score = 0.0
            reason = ""
            if cleaned and cleaned == normalize_text(name):
                score, reason = 1.0, "exact name"
            elif cleaned and cleaned == normalize_text(app_name):
                score, reason = 0.99, "exact app name"
            elif cleaned and cleaned.replace(" ", "") == normalize_text(slug).replace(" ", ""):
                score, reason = 0.98, "exact slug"
            elif cleaned and (cleaned in norm or norm in cleaned) and len(cleaned) >= 4:
                score, reason = 0.85, "substring"
            elif app_tokens and app_tokens.issubset(_tokens(hay)):
                score, reason = 0.8, "all app tokens"
            elif hint_tokens and hint_tokens.issubset(_tokens(hay)) and len(hint_tokens) >= 2:
                score, reason = 0.6, "all hint tokens"
            if score >= 0.6:
                matches.append(
                    {
                        "slug": slug,
                        "name": name,
                        "app": app_name,
                        "tools_count": (item.get("meta") or {}).get("tools_count"),
                        "auth_schemes": item.get("auth_schemes") or [],
                        "instant": item.get("instant") or {},
                        "match_reason": reason,
                        "score": score,
                    }
                )
        matches.sort(key=lambda m: (-m["score"], -(m.get("tools_count") or 0)))
        matches = matches[:8]
        best_slug = matches[0]["slug"] if matches else None
        catalog_url = (
            f"https://backend.composio.dev/api/v3.1/toolkits?search={quote_plus(best_slug)}"
            if best_slug
            else f"https://backend.composio.dev/api/v3.1/toolkits?search={quote_plus(app)}"
        )
        if matches:
            snippet = {
                "items": [
                    {
                        "slug": m["slug"],
                        "name": m["name"],
                        "app": m["app"],
                        "tools_count": m["tools_count"],
                    }
                    for m in matches[:3]
                ],
                "total_items": len(matches),
            }
        else:
            snippet = {"items": [], "total_items": 0, "query": app}
        return {
            "exists": bool(matches),
            "matches": matches,
            "catalog_url": catalog_url,
            "evidence_snippet": json.dumps(snippet, ensure_ascii=False)[:290],
        }

    async def github_repo(self, url: str) -> dict[str, Any]:
        parsed = _parse_github(url)
        if not parsed:
            return {"exists": False, "url": url, "error": "not a github.com repo URL"}
        owner, repo = parsed
        key = f"{owner}/{repo}".lower()
        if key in self._github_cache:
            return self._github_cache[key]
        headers = {"Accept": "application/vnd.github+json", "User-Agent": "composio-research/1.0"}
        if self.settings.github_token:
            headers["Authorization"] = f"Bearer {self.settings.github_token}"
        data: dict[str, Any] = {"exists": False, "url": f"https://github.com/{owner}/{repo}", "owner": owner, "repo": repo}
        async with httpx.AsyncClient(timeout=self.settings.http_timeout_seconds, headers=headers) as client:
            repo_resp = await client.get(f"https://api.github.com/repos/{owner}/{repo}")
            if repo_resp.status_code == 200:
                info = repo_resp.json()
                data.update(
                    {
                        "exists": True,
                        "full_name": info.get("full_name"),
                        "stars": info.get("stargazers_count"),
                        "pushed_at": info.get("pushed_at"),
                        "archived": info.get("archived"),
                        "description": info.get("description"),
                        "owner": (info.get("owner") or {}).get("login") or owner,
                        "html_url": info.get("html_url"),
                    }
                )
                commits = await client.get(
                    f"https://api.github.com/repos/{owner}/{repo}/commits", params={"per_page": 1}
                )
                if commits.status_code == 200 and commits.json():
                    data["last_commit_at"] = commits.json()[0]["commit"]["committer"]["date"]
            elif repo_resp.status_code == 404:
                data["error"] = "404 repository not found"
            else:
                data["error"] = f"HTTP {repo_resp.status_code}"
        self._github_cache[key] = data
        return data

    async def mcp_registry(self, query: str) -> list[dict[str, Any]]:
        async with httpx.AsyncClient(timeout=self.settings.http_timeout_seconds) as client:
            response = await client.get(
                "https://registry.modelcontextprotocol.io/v0/servers",
                params={"search": query, "limit": 10},
            )
        if response.status_code != 200:
            raise ToolError(f"MCP registry HTTP {response.status_code}: {response.text[:300]}")
        out = []
        for row in response.json().get("servers") or []:
            server = row.get("server") or row
            out.append(
                {
                    "name": server.get("name"),
                    "description": server.get("description"),
                    "version": server.get("version"),
                    "websiteUrl": server.get("websiteUrl"),
                    "repository": server.get("repository"),
                    "remotes": server.get("remotes"),
                }
            )
        return out


def _parse_github(url: str) -> tuple[str, str] | None:
    parsed = urlparse(url)
    if "github.com" not in parsed.netloc.lower():
        return None
    parts = [p for p in parsed.path.strip("/").split("/") if p]
    if len(parts) < 2:
        return None
    repo = parts[1].removesuffix(".git")
    return parts[0], repo


def _extract_html(html: str) -> tuple[str, str]:
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    for tag in soup(["script", "style", "noscript", "svg", "nav", "footer", "header", "form"]):
        tag.decompose()
    text = soup.get_text("\n", strip=True)
    lines = [line.strip() for line in text.splitlines() if len(line.strip()) > 2]
    return title, "\n".join(lines)


class SessionHandle:
    def __init__(self, runtime: ComposioRuntime, session):
        self.runtime = runtime
        self.session = session
        self.session_id = session.session_id
        self.mcp_url = session.mcp.url
        self.mcp_headers = dict(session.mcp.headers or {})

    async def execute(self, slug: str, arguments: dict[str, Any]) -> dict[str, Any]:
        result = await asyncio.to_thread(self.session.execute, slug, arguments=arguments)
        payload = result.model_dump(mode="json")
        if payload.get("result_type") != "completed":
            raise ToolError(str(payload.get("error") or f"{slug} result_type={payload.get('result_type')}"))
        return _unwrap_payload(payload)

    async def close(self) -> None:
        try:
            await asyncio.to_thread(self.session.delete)
        except Exception:
            pass


class MCPToolClient:
    def __init__(self, handle: SessionHandle):
        self.handle = handle
        self._stack = None
        self.session = None
        self.connected = False
        self.error: str | None = None

    async def connect(self) -> bool:
        try:
            from contextlib import AsyncExitStack

            from mcp import ClientSession
            from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

            self._stack = AsyncExitStack()
            http = create_mcp_http_client(headers=self.handle.mcp_headers)
            await self._stack.enter_async_context(http)
            streams = await self._stack.enter_async_context(
                streamable_http_client(self.handle.mcp_url, http_client=http)
            )
            read, write = streams
            self.session = await self._stack.enter_async_context(ClientSession(read, write))
            await self.session.initialize()
            self.connected = True
            return True
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            await self.close()
            return False

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if not self.connected or self.session is None:
            raise ToolError(f"MCP not connected: {self.error}")
        result = await self.session.call_tool(name, arguments)
        if getattr(result, "is_error", False):
            raise ToolError(f"MCP tool {name} returned is_error")
        texts = [getattr(block, "text", None) for block in result.content]
        text = next((t for t in texts if t), None)
        if not text:
            raise ToolError(f"MCP tool {name} returned no text content")
        return _unwrap_payload(text)

    async def close(self) -> None:
        if self._stack is not None:
            try:
                await self._stack.aclose()
            except Exception:
                pass
        self._stack = None
        self.session = None
        self.connected = False


class ResearchTools:
    """Per-app tool facade: budgeted, MCP-first, SDK fallback, fully logged."""

    def __init__(self, runtime: ComposioRuntime, handle: SessionHandle, settings: Settings):
        self.runtime = runtime
        self.handle = handle
        self.settings = settings
        self.mcp = MCPToolClient(handle)
        self.budget = ToolBudget(settings.max_tool_calls_per_app)
        self.calls: list[ToolCall] = []
        self.mcp_active = False
        self.mcp_error: str | None = None
        self.docs: dict[str, EvidenceDoc] = {}
        self.hits: list[SearchHit] = []

    async def start(self) -> None:
        self.mcp_active = await self.mcp.connect()
        if not self.mcp_active:
            self.mcp_error = self.mcp.error
            self._log(
                ToolCall(
                    seq=0,
                    tool="MCP_CONNECT",
                    source="mcp",
                    purpose="transport",
                    arguments={"url_host": urlparse(self.handle.mcp_url).netloc},
                    ok=False,
                    duration_s=0,
                    error=self.mcp_error,
                )
            )

    def _log(self, call: ToolCall) -> None:
        self.calls.append(call)

    async def _call(
        self,
        *,
        slug: str,
        arguments: dict[str, Any],
        purpose: str,
        log_args: dict[str, Any],
    ) -> dict[str, Any]:
        self.budget.spend(slug)
        started = time.monotonic()
        source = "mcp" if self.mcp_active else "sdk"
        error: str | None = None
        try:
            if self.mcp_active:
                try:
                    payload = await self.mcp.call_tool(slug, arguments)
                except Exception as exc:
                    self.mcp_active = False
                    self.mcp_error = f"MCP call failed, falling back to SDK: {type(exc).__name__}: {exc}"
                    source = "sdk"
                    payload = await self._execute_sdk_with_retries(slug, arguments)
            else:
                payload = await self._execute_sdk_with_retries(slug, arguments)
            count = len(payload.get("results") or []) if isinstance(payload, dict) else None
            self._log(
                ToolCall(
                    seq=len(self.calls) + 1,
                    tool=slug,
                    source=source,
                    purpose=purpose,
                    arguments=log_args,
                    ok=True,
                    duration_s=time.monotonic() - started,
                    result_count=count,
                )
            )
            return payload
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            self._log(
                ToolCall(
                    seq=len(self.calls) + 1,
                    tool=slug,
                    source=source,
                    purpose=purpose,
                    arguments=log_args,
                    ok=False,
                    duration_s=time.monotonic() - started,
                    error=error[:800],
                )
            )
            raise ToolError(error) from exc

    async def _execute_sdk_with_retries(self, slug: str, arguments: dict[str, Any]) -> dict[str, Any]:
        async def attempt():
            try:
                return await self.handle.execute(slug, arguments)
            except Exception as exc:
                message = f"{type(exc).__name__}: {exc}"
                if _retryable(message):
                    raise TransientToolError(message) from exc
                raise
        return await _run_with_retries(attempt)

    async def search(self, query: str, *, purpose: str, num_results: int | None = None) -> list[dict[str, Any]]:
        payload = await self._call(
            slug="EXA_SEARCH",
            arguments={
                "query": query,
                "numResults": num_results or self.settings.max_search_results,
                "contents": {"text": False, "highlights": True},
            },
            purpose=purpose,
            log_args={"query": query, "numResults": num_results or self.settings.max_search_results},
        )
        results = payload.get("results") or []
        hits: list[dict[str, Any]] = []
        for rank, row in enumerate(results, start=1):
            url = row.get("url")
            if not url:
                continue
            hit = SearchHit(
                title=(row.get("title") or "").strip(),
                url=url,
                snippet=(row.get("summary") or row.get("text") or " ".join(row.get("highlights") or []))[:1200],
                purpose=purpose,
                rank=rank,
            )
            self.hits.append(hit)
            hits.append({"title": hit.title, "url": hit.url, "snippet": hit.snippet, "rank": rank})
        return hits

    async def fetch(self, urls: list[str], *, purpose: str, max_chars: int | None = None) -> list[EvidenceDoc]:
        unique = list(dict.fromkeys(u for u in urls if u.startswith(("http://", "https://"))))
        if not unique:
            return []
        payload = await self._call(
            slug="EXA_GET_CONTENTS_ACTION",
            arguments={
                "urls": unique[:5],
                "text": {"maxCharacters": max_chars or self.settings.page_char_budget, "verbosity": "standard"},
                "maxAgeHours": self.settings.exa_max_age_hours,
            },
            purpose=purpose,
            log_args={"urls": unique[:5], "max_chars": max_chars or self.settings.page_char_budget},
        )
        docs: list[EvidenceDoc] = []
        for row in payload.get("results") or []:
            url = row.get("url")
            text = row.get("text") or ""
            if not url or not text:
                continue
            doc = EvidenceDoc(
                url=url,
                title=(row.get("title") or "").strip(),
                text=text,
                purpose=purpose,
                fetched_via="composio_exa",
                published_date=row.get("publishedDate"),
            )
            self._add_doc(doc)
            docs.append(doc)
        return docs

    async def direct_fetch(self, url: str, *, purpose: str) -> EvidenceDoc | None:
        self.budget.spend("HTTPX_FETCH")
        started = time.monotonic()
        try:
            headers = {"User-Agent": "Mozilla/5.0 (compatible; composio-research/1.0)"}
            async with httpx.AsyncClient(
                timeout=self.settings.http_timeout_seconds, follow_redirects=True, headers=headers
            ) as client:
                response = await client.get(url)
            content_type = response.headers.get("content-type", "")
            if "html" not in content_type and "text" not in content_type:
                raise ToolError(f"unsupported content-type {content_type}")
            title, text = _extract_html(response.text)
            doc = EvidenceDoc(
                url=str(response.url),
                title=title,
                text=text[: self.settings.page_char_budget],
                purpose=purpose,
                fetched_via="httpx_fallback",
            )
            self._add_doc(doc)
            self._log(
                ToolCall(
                    seq=len(self.calls) + 1,
                    tool="HTTPX_FETCH",
                    source="httpx",
                    purpose=purpose,
                    arguments={"url": url},
                    ok=response.status_code < 400,
                    duration_s=time.monotonic() - started,
                    result_count=1 if text else 0,
                )
            )
            return doc
        except Exception as exc:
            self._log(
                ToolCall(
                    seq=len(self.calls) + 1,
                    tool="HTTPX_FETCH",
                    source="httpx",
                    purpose=purpose,
                    arguments={"url": url},
                    ok=False,
                    duration_s=time.monotonic() - started,
                    error=f"{type(exc).__name__}: {exc}"[:500],
                )
            )
            return None

    def _add_doc(self, doc: EvidenceDoc) -> None:
        existing = self.docs.get(doc.url)
        if existing is None or len(doc.text) > len(existing.text):
            self.docs[doc.url] = doc

    async def catalog(self, app: str, hint: str | None) -> dict[str, Any]:
        self.budget.spend("COMPOSIO_CATALOG")
        started = time.monotonic()
        try:
            matches = await self.runtime.catalog_matches(app, hint)
            self._log(
                ToolCall(
                    seq=len(self.calls) + 1,
                    tool="COMPOSIO_CATALOG",
                    source="sdk_api",
                    purpose="composio_toolkit",
                    arguments={"app": app},
                    ok=True,
                    duration_s=time.monotonic() - started,
                    result_count=len(matches.get("matches") or []),
                )
            )
            return matches
        except Exception as exc:
            self._log(
                ToolCall(
                    seq=len(self.calls) + 1,
                    tool="COMPOSIO_CATALOG",
                    source="sdk_api",
                    purpose="composio_toolkit",
                    arguments={"app": app},
                    ok=False,
                    duration_s=time.monotonic() - started,
                    error=f"{type(exc).__name__}: {exc}"[:500],
                )
            )
            raise

    async def github(self, url: str, purpose: str = "mcp_github") -> dict[str, Any]:
        self.budget.spend("GITHUB_REPO")
        started = time.monotonic()
        try:
            info = await self.runtime.github_repo(url)
            self._log(
                ToolCall(
                    seq=len(self.calls) + 1,
                    tool="GITHUB_REPO",
                    source="github_api",
                    purpose=purpose,
                    arguments={"url": url},
                    ok=True,
                    duration_s=time.monotonic() - started,
                    result_count=1 if info.get("exists") else 0,
                )
            )
            return info
        except Exception as exc:
            self._log(
                ToolCall(
                    seq=len(self.calls) + 1,
                    tool="GITHUB_REPO",
                    source="github_api",
                    purpose=purpose,
                    arguments={"url": url},
                    ok=False,
                    duration_s=time.monotonic() - started,
                    error=f"{type(exc).__name__}: {exc}"[:500],
                )
            )
            raise

    async def registry(self, query: str) -> list[dict[str, Any]]:
        self.budget.spend("MCP_REGISTRY")
        started = time.monotonic()
        try:
            rows = await self.runtime.mcp_registry(query)
            self._log(
                ToolCall(
                    seq=len(self.calls) + 1,
                    tool="MCP_REGISTRY",
                    source="public_api",
                    purpose="mcp_registry",
                    arguments={"query": query},
                    ok=True,
                    duration_s=time.monotonic() - started,
                    result_count=len(rows),
                )
            )
            return rows
        except Exception as exc:
            self._log(
                ToolCall(
                    seq=len(self.calls) + 1,
                    tool="MCP_REGISTRY",
                    source="public_api",
                    purpose="mcp_registry",
                    arguments={"query": query},
                    ok=False,
                    duration_s=time.monotonic() - started,
                    error=f"{type(exc).__name__}: {exc}"[:500],
                )
            )
            return []

    async def close(self) -> None:
        await self.mcp.close()

    def tool_log(self) -> list[dict[str, Any]]:
        return [c.log() for c in self.calls]
