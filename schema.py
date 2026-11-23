"""Pydantic output contract for one researched app."""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Confidence = Literal["high", "medium", "low", "not_found"]
AccessModel = Literal[
    "self_serve_free",
    "self_serve_trial",
    "paid_plan_required",
    "admin_approval",
    "app_review_required",
    "partner_only",
    "contact_sales",
    "no_api",
    "unknown",
]
Verdict = Literal["easy", "moderate", "hard", "blocked"]
Status = Literal["drafted", "verified", "needs_human_review"]
AuthMethod = Literal["oauth2", "api_key", "basic", "bearer_token", "jwt", "other"]

ACCESS_MODELS: tuple[str, ...] = (
    "self_serve_free",
    "self_serve_trial",
    "paid_plan_required",
    "admin_approval",
    "app_review_required",
    "partner_only",
    "contact_sales",
    "no_api",
    "unknown",
)
VERDICTS: tuple[str, ...] = ("easy", "moderate", "hard", "blocked")


class Claim(BaseModel):
    """A single factual field. High/medium confidence requires a source and quote."""

    model_config = ConfigDict(extra="forbid")

    value: str | bool | list[str] | None = None
    source_url: str | None = None
    quote: str | None = None
    confidence: Confidence = "not_found"
    retrieved_at: str = Field(default_factory=lambda: date.today().isoformat())

    @field_validator("source_url")
    @classmethod
    def _valid_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            return None
        if not value.startswith(("http://", "https://")):
            raise ValueError("source_url must be an absolute http(s) URL")
        return value

    @field_validator("quote")
    @classmethod
    def _short_quote(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = " ".join(value.split())
        if len(value) > 300:
            raise ValueError(f"quote must be <=300 chars (got {len(value)}); shorten it verbatim")
        return value or None

    @field_validator("retrieved_at")
    @classmethod
    def _iso_date(cls, value: str) -> str:
        try:
            return date.fromisoformat(value).isoformat()
        except ValueError as exc:
            raise ValueError("retrieved_at must be an ISO date (YYYY-MM-DD)") from exc

    @model_validator(mode="after")
    def _evidence_consistency(self) -> "Claim":
        if self.confidence in ("high", "medium"):
            if not self.source_url:
                raise ValueError("confidence high/medium requires source_url")
            if not self.quote:
                raise ValueError("confidence high/medium requires a verbatim quote")
        if self.quote and not self.source_url:
            raise ValueError("a quote requires a source_url")
        if self.confidence == "not_found" and self.value is not None:
            raise ValueError("confidence=not_found requires value to be null")
        return self


class AppResearch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # identity (plain strings from the input)
    app: str
    category: str

    # claims
    one_line_description: Claim
    has_public_api: Claim
    api_type: Claim
    api_breadth: Claim
    docs_url: Claim
    auth_methods: Claim
    oauth_scopes_documented: Claim
    access_model: Claim
    access_notes: Claim
    official_mcp_server: Claim
    community_mcp_servers: Claim
    composio_toolkit_exists: Claim
    rate_limits: Claim
    webhooks: Claim
    sandbox_or_test_env: Claim
    sdk_languages: Claim
    api_versioning_or_deprecation_notes: Claim

    # derived judgement (not a Claim)
    buildability_verdict: Verdict
    verdict_reason: str
    main_blocker: str | None = None
    needs_outreach: bool
    outreach_reason: str | None = None

    open_questions: list[str] = Field(default_factory=list)
    status: Status = "drafted"

    # audit is optional and never used as evidence; it keeps provenance for eval
    audit: dict[str, Any] = Field(default_factory=dict)

    @field_validator("app", "category")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("must be non-empty")
        return value.strip()

    @field_validator("verdict_reason")
    @classmethod
    def _reason(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("verdict_reason must be non-empty")
        return value.strip()

    @field_validator("main_blocker", "outreach_reason")
    @classmethod
    def _optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None

    @model_validator(mode="after")
    def _field_types_and_consistency(self) -> "AppResearch":
        expect_str = (
            "one_line_description",
            "api_type",
            "api_breadth",
            "docs_url",
            "access_model",
            "access_notes",
            "official_mcp_server",
            "rate_limits",
            "api_versioning_or_deprecation_notes",
        )
        expect_bool = (
            "has_public_api",
            "oauth_scopes_documented",
            "composio_toolkit_exists",
            "webhooks",
            "sandbox_or_test_env",
        )
        expect_list = ("auth_methods", "community_mcp_servers", "sdk_languages")

        for name in expect_str:
            claim: Claim = getattr(self, name)
            if claim.value is not None and not isinstance(claim.value, str):
                raise ValueError(f"{name}.value must be a string or null")
        for name in expect_bool:
            claim = getattr(self, name)
            if claim.value is not None and not isinstance(claim.value, bool):
                raise ValueError(f"{name}.value must be a boolean or null")
        for name in expect_list:
            claim = getattr(self, name)
            if claim.value is None:
                continue
            if not isinstance(claim.value, list) or not all(isinstance(x, str) for x in claim.value):
                raise ValueError(f"{name}.value must be a list[str] or null")

        if self.access_model.value is not None and self.access_model.value not in ACCESS_MODELS:
            raise ValueError(f"access_model.value must be one of {ACCESS_MODELS}")
        if self.auth_methods.value:
            bad = [m for m in self.auth_methods.value if m not in (
                "oauth2", "api_key", "basic", "bearer_token", "jwt", "other"
            )]
            if bad:
                raise ValueError(f"auth_methods contains unsupported values: {bad}")
        for name in ("docs_url", "official_mcp_server"):
            claim = getattr(self, name)
            if isinstance(claim.value, str) and claim.value.lower() not in ("none", "null", "n/a"):
                if not claim.value.startswith(("http://", "https://")):
                    raise ValueError(f"{name}.value must be a URL or null")
        if self.official_mcp_server.value and str(self.official_mcp_server.value).lower() in ("none", "null", "n/a"):
            # Normalize explicit "none" to null, but keep the evidence/quote.
            self.official_mcp_server.value = None

        if self.buildability_verdict == "blocked" and not self.main_blocker:
            raise ValueError("main_blocker is required when buildability_verdict=blocked")
        if self.needs_outreach and not self.outreach_reason:
            raise ValueError("outreach_reason is required when needs_outreach=true")
        return self

    def claim(self, field: str) -> Claim:
        value = getattr(self, field)
        if not isinstance(value, Claim):
            raise TypeError(f"{field} is not a Claim")
        return value


CLAIM_FIELDS: tuple[str, ...] = (
    "one_line_description",
    "has_public_api",
    "api_type",
    "api_breadth",
    "docs_url",
    "auth_methods",
    "oauth_scopes_documented",
    "access_model",
    "access_notes",
    "official_mcp_server",
    "community_mcp_servers",
    "composio_toolkit_exists",
    "rate_limits",
    "webhooks",
    "sandbox_or_test_env",
    "sdk_languages",
    "api_versioning_or_deprecation_notes",
)

HIGH_RISK_FIELDS: tuple[str, ...] = (
    "auth_methods",
    "access_model",
    "official_mcp_server",
    "has_public_api",
    "buildability_verdict",
    "needs_outreach",
)

FIELD_DESCRIPTIONS: dict[str, str] = {
    "one_line_description": "One factual sentence describing what the product does.",
    "has_public_api": "Whether a public, documented API exists (true/false).",
    "api_type": "REST, GraphQL, SOAP, or other (single primary type).",
    "api_breadth": "Rough size and read/write scope, e.g. 'REST, ~200 resources, read-write'.",
    "docs_url": "Canonical official developer documentation/API reference URL.",
    "auth_methods": "Any of oauth2, api_key, basic, bearer_token, jwt, other.",
    "oauth_scopes_documented": "Whether OAuth scopes/permissions are documented.",
    "access_model": "One of: " + ", ".join(ACCESS_MODELS) + ".",
    "access_notes": "Exactly what gates API access (plan tier, review, partner, etc.).",
    "official_mcp_server": "Vendor-operated MCP server URL, or null if none.",
    "community_mcp_servers": "URLs of credible community/third-party MCP servers.",
    "composio_toolkit_exists": "Whether a matching toolkit exists in Composio's catalog.",
    "rate_limits": "Documented rate limits (numbers/units) or 'not documented'.",
    "webhooks": "Whether the vendor documents webhooks/event callbacks.",
    "sandbox_or_test_env": "Whether a documented sandbox/test environment exists.",
    "sdk_languages": "Official SDK languages (list).",
    "api_versioning_or_deprecation_notes": "Versioning scheme and deprecation/sunset policy.",
}
