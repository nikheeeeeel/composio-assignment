"""Central configuration. All tunables come from .env or sane defaults."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


def _float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return float(raw)


@dataclass(slots=True)
class Settings:
    base_dir: Path = BASE_DIR
    data_dir: Path = field(default_factory=lambda: BASE_DIR / os.getenv("DATA_DIR", "data"))
    results_dir: Path = field(default_factory=lambda: BASE_DIR / os.getenv("RESULTS_DIR", "results"))

    composio_api_key: str = field(default_factory=lambda: os.getenv("COMPOSIO_API_KEY", "").strip())
    search_provider: str = field(default_factory=lambda: os.getenv("SEARCH_PROVIDER", "exa").strip().lower())
    exa_api_key: str = field(default_factory=lambda: os.getenv("EXA_API_KEY", "").strip())
    tavily_api_key: str = field(default_factory=lambda: os.getenv("TAVILY_API_KEY", "").strip())
    firecrawl_api_key: str = field(default_factory=lambda: os.getenv("FIRECRAWL_API_KEY", "").strip())
    github_token: str = field(default_factory=lambda: os.getenv("GITHUB_TOKEN", "").strip())

    llm_backend: str = field(default_factory=lambda: os.getenv("LLM_BACKEND", "opencode").strip().lower())
    researcher_model: str = field(
        default_factory=lambda: os.getenv("RESEARCHER_MODEL", "opencode/deepseek-v4.1-flash").strip()
    )
    verifier_model: str = field(
        default_factory=lambda: os.getenv("VERIFIER_MODEL", "opencode/gemini-3.8-flash").strip()
    )
    openai_api_key: str = field(default_factory=lambda: os.getenv("OPENAI_API_KEY", "").strip())
    openai_base_url: str = field(
        default_factory=lambda: os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    )

    concurrency: int = field(default_factory=lambda: _int("CONCURRENCY", 4))
    tool_concurrency: int = field(default_factory=lambda: _int("TOOL_CONCURRENCY", 4))
    max_tool_calls_per_app: int = field(default_factory=lambda: _int("MAX_TOOL_CALLS_PER_APP", 25))
    max_revision_rounds: int = field(default_factory=lambda: _int("MAX_REVISION_ROUNDS", 2))
    quote_fuzzy_threshold: float = field(default_factory=lambda: _float("QUOTE_FUZZY_THRESHOLD", 0.90))
    max_search_results: int = field(default_factory=lambda: _int("MAX_SEARCH_RESULTS", 5))
    max_pages_per_app: int = field(default_factory=lambda: _int("MAX_PAGES_PER_APP", 10))
    page_char_budget: int = field(default_factory=lambda: _int("PAGE_CHAR_BUDGET", 5000))
    evidence_char_budget: int = field(default_factory=lambda: _int("EVIDENCE_CHAR_BUDGET", 45000))
    exa_max_age_hours: int = field(default_factory=lambda: _int("EXA_MAX_AGE_HOURS", 24))
    llm_timeout_seconds: int = field(default_factory=lambda: _int("LLM_TIMEOUT_SECONDS", 300))
    http_timeout_seconds: int = field(default_factory=lambda: _int("HTTP_TIMEOUT_SECONDS", 30))
    user_id: str = field(default_factory=lambda: os.getenv("COMPOSIO_RESEARCH_USER_ID", "composio-research-agent"))

    @property
    def pass1_dir(self) -> Path:
        return self.results_dir / "pass1"

    @property
    def final_dir(self) -> Path:
        return self.results_dir / "final"

    @property
    def logs_dir(self) -> Path:
        return self.results_dir / "logs"

    @property
    def eval_dir(self) -> Path:
        return self.base_dir / "eval"

    def ensure_dirs(self) -> None:
        for path in (self.pass1_dir, self.final_dir, self.logs_dir, self.eval_dir):
            path.mkdir(parents=True, exist_ok=True)

    def validate(self) -> None:
        missing = []
        if not self.composio_api_key:
            missing.append("COMPOSIO_API_KEY")
        if self.search_provider == "exa" and not self.exa_api_key:
            missing.append("EXA_API_KEY")
        if self.search_provider == "tavily" and not self.tavily_api_key:
            missing.append("TAVILY_API_KEY")
        if self.search_provider == "firecrawl" and not self.firecrawl_api_key:
            missing.append("FIRECRAWL_API_KEY")
        if self.llm_backend == "openai_compatible" and not self.openai_api_key:
            missing.append("OPENAI_API_KEY")
        if missing:
            raise RuntimeError(
                "Missing required credentials: " + ", ".join(missing) + ". Add them to .env (see .env.example)."
            )


settings = Settings()
