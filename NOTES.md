# NOTES — verified setup facts

Last updated: 2026-11-23

This file records what I actually inspected/fetched before writing agent code. It is the
source of truth for package names, signatures, and the model configuration used here.

---

## 1. Input data inspection

Command run: a Python walk of `data/*.json` that loaded every file, printed its top-level
shape, and counted app records.

Result:

- `data/` contains **10 JSON files**, one per category.
- Every file is a **top-level dict** with this schema:

```json
{
  "category": "CRM and Sales",
  "categoryNumber": 1,
  "source": {"spreadsheet": "...", "sheet": "Sheet1", "url": "..."},
  "appCount": 10,
  "apps": [{"number": 1, "app": "Salesforce", "hint": "salesforce.com"}]
}
```

- There are exactly two app-record shapes (both compatible):
  - 87 records: `{app, hint, number}`
  - 13 records: `{app, hint, note, number}`
- **Total apps: 100.** `number` values are unique and cover 1..100.
- **No duplicate app names** (case-insensitive check).
- Category names come from the file, not from the individual app record.
- The loader handles both the observed dict-with-`apps` form and a bare top-level list.
  A `url` field is not present in the input; `hint` is a domain or docs path and is the
  disambiguation hint. The normalized record is
  `{name, category, url, description, source_file}` where `url` is derived from `hint`
  only when it is a domain/path (and is kept as a hint string otherwise), and
  `description` comes from `note`.

Ambiguous / extra-care names (resolved using `hint` + `note` during research):

| Input name | Disambiguation hint |
|---|---|
| Close | close.com (CRM), not "close" the verb |
| Copper | copper.com (CRM) |
| Front | front.com (helpdesk) |
| Plain | plain.com (support) |
| Fathom | fathom.video (meeting notes), not Fathom Analytics |
| Consensus | consensus.app (research) |
| Clay | clay.com (data enrichment), not clay.com pottery |
| Sherlock | github.com/sherlock-project/sherlock (OSS CLI, not a hosted API) |
| Paygent Connect | "NMI-powered" note; `paygent` hint is not a URL |
| iPayX | ipayx.ai/docs |
| Waterfall.io | waterfall.io (contact/company intel) |
| fanbasis | fanbasis.com |
| higgsfield | higgsfield.ai/cli |
| NotebookLM | note says "Enterprise API"; consumer product has no public API |
| Otter AI | note says "MCP server"; check whether the MCP is official |
| Devin | note says "MCP"; docs.devin.ai |
| Mermaid CLI | OSS GitHub project, not a SaaS API |
| YouTube Transcript | hint is transcriptapi.com (third-party), not YouTube itself |

---

## 2. Composio docs fetched (current, 2026-11-23)

Fetched from `docs.composio.dev` (Markdown endpoints) and the Composio GitHub README:

- `https://docs.composio.dev/llms.txt` — current documentation index
- `https://docs.composio.dev/docs/quickstart.md`
- `https://docs.composio.dev/docs/sessions-via-mcp.md`
- `https://docs.composio.dev/reference/sdk-reference/python/session.md`
- `https://docs.composio.dev/docs/instant-tools.md`
- `https://docs.composio.dev/docs/authentication.md`
- `https://docs.composio.dev/docs/authentication/manually-authenticating.md`
- `https://docs.composio.dev/docs/agent-setup/unattended-authentication.md`
- `https://docs.composio.dev/reference/api-reference/toolkits/getToolkits.md`
- `https://docs.composio.dev/reference/api-reference/tools/getTools.md`
- `https://docs.composio.dev/toolkits/exa.md`
- `https://docs.composio.dev/toolkits/tavily.md`
- `https://docs.composio.dev/toolkits/firecrawl.md`
- `https://raw.githubusercontent.com/ComposioHQ/composio/next/README.md`

Key confirmations:

- Current REST API is **v3.1** at `https://backend.composio.dev/api/v3.1`.
- The current Python package is **`composio`**, not `composio-core` (legacy v1).
- Integrations are **sessions**: `composio.sessions.create(user_id=..., ...)`.
  `Composio()` reads `COMPOSIO_API_KEY`; no provider package is required to use MCP.
- Session execution: `session.execute(tool_slug, arguments={...})`; result is a Pydantic
  model with `result_type` (`completed` / `failed` / `blocked`) and `model_dump(mode="json")`.
- Session search: `session.search(query="...")`.
- MCP: create the session with `mcp=True`, then use `session.mcp.url` and
  `session.mcp.headers` as the MCP endpoint. MCP requires `composio >= 0.22.1`; installed
  version is 0.34.0. The exported header is `x-api-key` for a project-key session.
- Direct-tools MCP preset: `session_preset=SESSION_PRESET_DIRECT_TOOLS` gives one MCP URL
  with exactly the tools you list (`tools={"exa": {"enable": [...]}}`).
- Toolkit catalog: `GET /api/v3.1/toolkits` with `x-api-key`; supports `search=` and
  returns `items[].{slug,name,app,auth_schemes,instant,...}`. `GET /api/v3.1/tools`
  supports `toolkit_slug`, `query`, `include_pricing`, `toolkit_versions=latest`.
- Unattended agent signup: `composio login --agent` (CLI), which writes
  `~/.composio/agent.json`; the project key is `composio.api_key`.
- Instant Tools: run supported tools on Composio provider accounts without a user
  provider key, charged to the organization's prepaid balance. Must be enabled in
  project Settings; not on by default. Exa/Tavily/Firecrawl tools declare Instant
  support per tool.

### Installed / verified versions

Verified locally with `uv pip list` and imports:

| Package | Version |
|---|---|
| Python | 3.14.9 |
| `composio` | 0.34.0 |
| `composio-client` | 2.1.36 |
| `pydantic` | 2.14.4 |
| `mcp` | 2.3.0 |
| `httpx` | 0.28.1 |
| `tenacity` | 9.2.1 |
| `beautifulsoup4` | 4.15.0 |
| `python-dotenv` | 1.2.4 |
| Composio CLI | `@composio/cli@0.6.3` |

Confirmed signatures from the installed package:

```python
Composio(provider=None, **kwargs)
Composio.sessions.create(...)
ToolRouterSession.execute(tool_slug, *, arguments=None, account=None,
                          input_responses=None, request_state=None)
ToolRouterSession.search(*, query, model=None)
```

---

## 3. Model / provider configuration

- There is **no project or global OpenCode model config file** in this workspace
  (`opencode.json` / `.opencode/` absent; `opencode api get /api/info` reports v2.0.26).
- The active model for this OpenCode session is **`opencode/deepseek-v4.1-flash`**.
- Verified it is callable non-interactively and returns JSON:
  `opencode run -m opencode/deepseek-v4.1-flash --format json "<prompt>"`
- Verified a second model for the verifier: **`opencode/gemini-3.8-flash`** returns
  `step_finish` events with `cost` and `tokens` (`input`, `output`, `reasoning`, `cache`).
- No model API keys are present in the environment. The agents therefore use the local
  OpenCode runner as the model transport (`LLM_BACKEND=opencode`), which reuses the
  OpenCode-authenticated provider. A direct OpenAI-compatible backend is also supported
  in `config.py` for portability, with keys read from `.env`.
- Model names are single config values and independently swappable:
  - `RESEARCHER_MODEL=opencode/deepseek-v4.1-flash`
  - `VERIFIER_MODEL=opencode/gemini-3.8-flash`
- **Composio provider package:** there is no Composio provider adapter for OpenCode
  (providers exist for OpenAI, Anthropic, LangChain, etc.). We therefore use session
  tool schemas + `session.execute()` for the SDK path and the session MCP URL for the MCP
  path. No framework provider package is imported.

---

## 4. Search / scrape toolkit choice

Chosen primary: **Exa** (`EXA` toolkit).

Why:

- One toolkit covers both halves of the required research: `EXA_SEARCH` (web search) and
  `EXA_GET_CONTENTS_ACTION` (full page text / highlights / live crawl).
- Auth schemes documented as **API_KEY, BEARER_TOKEN**; `EXA_GET_CONTENTS_ACTION` can
  return full page text, which is required for verbatim quote verification.
- 65 tools, so it also supports follow-up fetches without a second vendor.
- Instant support is declared for the answer/search/contents tools (paid from the
  Composio balance), but Instant is **off by default** and needs project enablement +
  prepaid balance.

Fallbacks (already wired in the config as ordered alternatives):

1. **Tavily** (`TAVILY_SEARCH`, `TAVILY_EXTRACT`) — single API_KEY, simple schemas.
2. **Firecrawl** (`FIRECRAWL_SEARCH` / `FIRECRAWL_SCRAPE`) — single API_KEY, robust JS
   rendering, but large tool surface (246 tools) and higher latency.

None of these are "no auth". A provider API key or Composio Instant credit is required.
The code will not fake search results; if neither is available it will stop with the real
error and mark the app `needs_human_review`.

---

## 5. Blocking manual setup (current status)

### 5.1 Composio API key — BLOCKED

- `COMPOSIO_API_KEY` is **not set** and no key is stored on this machine.
- `composio login --agent` (the documented unattended path) fails because
  `agents.composio.dev` currently returns **HTTP 503**:

```
services/AgentRequestError • agents.composio.dev request failed with HTTP 503
```

- `https://backend.composio.dev/api/v3.1/toolkits` without a key returns HTTP 401
  (`No authentication provided`), confirming the backend itself is reachable.

**What I need from you (pick one):**

1. Create/select a Composio project and paste a project API key into `.env` as
   `COMPOSIO_API_KEY=...` (Settings → API keys:
   <https://dashboard.composio.dev/~/project/settings/api-keys>), or
2. Let me retry `composio login --agent` later once `agents.composio.dev` recovers. The
   project key will be read from `~/.composio/agent.json` and written to `.env` without
   being printed.

### 5.2 Search / scrape credentials — BLOCKED until 5.1 is resolved

Pick one:

1. **Exa (recommended):** add `EXA_API_KEY=...` to `.env`. After 5.1, I will create/attach
   the Exa API-key connection once and reuse it.
2. **Tavily:** add `TAVILY_API_KEY=...` instead (same flow).
3. **Firecrawl:** add `FIRECRAWL_API_KEY=...` instead (same flow).
4. **Composio Instant:** enable *Instant Toolkits* in project Settings and add prepaid
   balance. Then no provider key is needed, but usage is billed to the Composio balance.
   This path is unverified until I have a Composio key.

I have not invented results, mocked a search, or written any placeholder app data.
