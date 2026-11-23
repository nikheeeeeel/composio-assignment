# composio-assignment

An evidence-first research agent that answers one question for each of 100 SaaS apps:
**how hard would it be to build an agent toolkit / integration for this app?**

For every app it produces a structured record covering the public API, docs, auth methods,
access gating, official/community MCP servers, Composio toolkit availability, rate limits,
webhooks, sandbox environments, SDKs, and a final `easy | moderate | hard | blocked`
buildability verdict — with a source URL and a verbatim quote attached to every claim.

The agent is deliberately distrustful of itself: each draft goes through a **mechanical
verifier** (no LLM) and an **adversarial LLM verifier**, and is revised until both pass or
the revision budget runs out.

---

## What's in the box

| Path | Purpose |
|---|---|
| `main.py` | CLI entry point |
| `pipeline.py` | Research → verify → revise → save orchestration |
| `researcher.py` | Agent 1: gathers evidence and writes the draft |
| `verifier.py` | Agent 2: judges claims + independently re-derives high-risk fields |
| `verify_mechanical.py` | Non-LLM checks: re-fetch sources, fuzzy-match quotes, audit domains |
| `composio_tools.py` | Composio sessions (MCP first, SDK fallback) + Exa/GitHub/MCP-registry tools |
| `schema.py` | Pydantic output contract (`AppResearch`, 17 sourced claims) |
| `evidence.py` | Builds the compact, citation-friendly evidence bundle |
| `prompts.py` | System/user prompts for research, judging, and re-derivation |
| `llm.py` | Model transport (OpenCode runner or any OpenAI-compatible API) |
| `data_loader.py` | Normalizes `data/*.json` into app records |
| `data/` | Input: 10 category files, 100 apps |
| `results/pass1/` | First drafts (one JSON per app) |
| `results/final/` | Verified / needs-review records |
| `results/logs/` | Per-app tool calls, LLM calls, token/cost usage, verification rounds |
| `summaries/` | Generated category-level analysis of the 100 drafts |
| `eval/human_verification_sample.csv` | Sample for manual spot-checks |
| `build_case_study.py` | Builds the single-page `case-study.html` report |
| `.opencode/agents/json-only.md` | Tool-less OpenCode agent used purely as a JSON model endpoint |

---

## How it works

For each app the pipeline runs:

1. **Gather** (`researcher.py`)
   - Creates a Composio session exposing exactly `EXA_SEARCH` and `EXA_GET_CONTENTS_ACTION`
     through the session's MCP URL (with automatic fallback to `session.execute()`).
   - Runs nine targeted searches: docs, auth, pricing, MCP, rate limits, webhooks, sandbox,
     SDKs, versioning — restricted to the vendor domain when the input `hint` provides one.
   - Fetches the most promising pages, plus the Composio toolkit catalog, the official MCP
     registry, and relevant GitHub repos.
   - All tool use is capped per app and fully logged.

2. **Synthesize**
   - The evidence bundle goes to the researcher model through the tool-less `json-only`
     agent, so the model can only reason over what was actually fetched.
   - Output is validated against `schema.py` (claims need URL + quote for high/medium
     confidence; `not_found` must be `null`).

3. **Refine** — if high-risk fields (auth, access model, MCP, public API) are still weak,
   the agent runs extra searches and re-synthesizes once.

4. **Verify, two independent layers**
   - **Mechanical (Layer A):** re-fetches every cited URL, checks the quote actually appears
     (fuzzy match, default 0.90), detects login walls / error pages, audits domains.
   - **LLM (Layer B):** one model call judges each claim as
     `SUPPORTED / PARTIALLY / UNSUPPORTED / CONTRADICTED`; a second, independent call
     re-derives the six highest-risk fields *without seeing the researcher's answers*.

5. **Revise** — any critical issue is fed back to the researcher (with fresh evidence if
   budget allows), and the loop repeats up to `MAX_REVISION_ROUNDS` (default 2).

6. **Save** — final status is `verified`, `needs_human_review`, or `error`. Records are
   checkpointed, so a re-run skips anything already finished unless `--force` is passed.

Verification never fabricates: if a source is unreachable or a claim can't be supported,
the field stays `not_found`, an open question is recorded, and the app is marked for review.

---

## Setup

### 1. Requirements

- Python 3.11+ (tested on 3.14.9)
- A **Composio API key** and an **Exa API key**
- Either:
  - the [OpenCode CLI](https://opencode.ai) installed and authenticated (default), or
  - an OpenAI-compatible model endpoint + API key

The tool layer currently drives an Exa session inside Composio. `TAVILY_*` and
`FIRECRAWL_*` are present in config as intended fallbacks but are not wired into the runtime yet.

### 2. Install

```bash
cd composio
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. Configure

```bash
cp .env.example .env
```

Edit `.env`:

```ini
# Required
COMPOSIO_API_KEY=...        # dashboard.composio.dev → project → Settings → API keys
EXA_API_KEY=...             # exa.ai

# Model transport (default: your local OpenCode runner, no model key needed)
LLM_BACKEND=opencode
RESEARCHER_MODEL=opencode/deepseek-v4.1-flash
VERIFIER_MODEL=opencode/gemini-3.8-flash
```

`LLM_BACKEND=opencode` shells out to `opencode run --agent json-only`, reusing whatever
provider you're already signed into. If you'd rather call a hosted API directly:

```ini
LLM_BACKEND=openai_compatible
OPENAI_API_KEY=...
OPENAI_BASE_URL=https://api.openai.com/v1
RESEARCHER_MODEL=gpt-4.1
VERIFIER_MODEL=gpt-4.1
```

> **Note on Composio auth:** `main.py` creates or reuses an Exa connected account for
> `COMPOSIO_RESEARCH_USER_ID` (default `composio-research-agent`) using your Exa key. No
> manual dashboard connection step is needed. If you don't have a key yet, you can also try
> `composio login --agent`, which writes one to `~/.composio/agent.json`.

---

## Running the agent

Always start with a dry run — it prints the app selection and needs no credentials:

```bash
python main.py --dry-run
python main.py --dry-run --category Finance
```

Then run a small batch (the default is the first 3 apps):

```bash
python main.py --limit 3
```

### Common invocations

```bash
# One app by name (exact match preferred, unique substring accepted)
python main.py --app "Linear"

# Everything in a category
python main.py --category "CRM and Sales"

# All 100 apps, re-running finished ones, higher parallelism
python main.py --all --force --concurrency 6

# Throughput profile: 8 concurrent apps, 8 pages/app, smaller evidence, 1 revision round
python main.py --all --fast

# Research only — save drafts and logs, skip both verification layers
python main.py --all --pass1-only --concurrency 8
```

### Flags

| Flag | Effect |
|---|---|
| `--dry-run` | Print the selection and exit (no credentials needed) |
| `--limit N` | Process at most N apps |
| `--app NAME` | Exact name or unique substring |
| `--category TEXT` | Case-insensitive category substring |
| `--all` | Process every selected app (default: first 3) |
| `--force` | Re-run apps that already have a saved result |
| `--concurrency N` | Apps processed in parallel (default 4) |
| `--tool-concurrency N` | Parallel tool calls within one app (default 4) |
| `--max-revision-rounds N` | Verification/revision loops per app (default 2) |
| `--researcher-model MODEL` | Override `RESEARCHER_MODEL` |
| `--verifier-model MODEL` | Override `VERIFIER_MODEL` |
| `--fast` | Concurrency 8, 8 pages, 4200-char pages, 1 revision round |
| `--pass1-only` | Save `results/pass1` + logs, skip verification |

At the end you get a summary table (status, rounds, tool calls, duration, cost) and a
machine-readable run summary in `results/logs/run_summary.json`.

---

## Outputs

```text
results/
├── pass1/<slug>.json     # every first draft (100 files in the committed run)
├── final/<slug>.json     # verified / needs-human-review records
└── logs/
    ├── <slug>.json       # tool calls, sources, LLM usage, every verification round
    ├── run_summary.json  # latest run
    └── run_<timestamp>.json
```

Each `Claim` looks like this:

```json
{
  "value": ["oauth2", "api_key"],
  "source_url": "https://developers.example.com/auth",
  "quote": "You can authenticate with either OAuth 2.0 or a workspace API key.",
  "confidence": "high",
  "retrieved_at": "2026-11-24"
}
```

## Reports

```bash
# Rebuild the single-page case study (reads results/, summaries/, and the Sheets snapshot)
python build_case_study.py          # → case-study.html

# Regenerate category summaries from results/pass1/*.json
.venv/bin/python summaries/generate_summaries.py
```

## Things to keep in mind

- The committed run is a **pass-1 run**: 100 drafts, with the full mechanical + LLM
  verification loop exercised on 3 apps (Attio, Zendesk, Ahrefs). Treat unverified drafts
  as leads, not ground truth.
- `results/logs/` is the audit trail. Every claim, tool call, and verification judgment is
  traceable there.
- Costs show up per app in the run summary. Use `--limit` or `--fast` while tuning.
- Nothing is mocked: missing credentials, unreachable sources, and exhausted tool budgets
  produce `needs_human_review` or `not_found`, never invented evidence.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Missing required credentials: COMPOSIO_API_KEY, EXA_API_KEY` | Fill them in `.env` |
| `opencode executable not found on PATH` | Install/authenticate the OpenCode CLI, or set `LLM_BACKEND=openai_compatible` |
| `missing tool-less OpenCode agent` | Keep `.opencode/agents/json-only.md` in the project root |
| Everything is skipped | Results already exist; add `--force` |
| Many `needs_human_review` | Expected on gated/enterprise apps; inspect `results/logs/<slug>.json` |
| MCP connection warning in logs | Harmless — the tool layer falls back to the Composio SDK automatically |

See [`NOTES.md`](NOTES.md) for the verified package versions, API details, and the
provider research behind these choices.
