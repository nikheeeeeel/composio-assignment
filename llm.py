"""Model transport.

Default backend is the locally configured OpenCode runner. It is called through the
`json-only` agent, whose permissions deny every tool, so a synthesis prompt cannot run
shell commands or read files. A plain OpenAI-compatible backend is also supported.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_attempt, wait_exponential

from config import Settings


class LLMError(RuntimeError):
    pass


@dataclass(slots=True)
class LLMResponse:
    text: str
    model: str
    purpose: str
    duration_s: float
    tokens: dict[str, int] = field(default_factory=dict)
    cost_usd: float | None = None

    def log(self) -> dict[str, Any]:
        return {
            "purpose": self.purpose,
            "model": self.model,
            "duration_s": round(self.duration_s, 3),
            "tokens": self.tokens,
            "cost_usd": self.cost_usd,
        }


class _TransientLLMError(LLMError):
    pass


def _try_json(text: str) -> Any:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:].lstrip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    decoder = json.JSONDecoder()
    for index, char in enumerate(cleaned):
        if char not in "[{":
            continue
        try:
            value, _ = decoder.raw_decode(cleaned[index:])
            return value
        except json.JSONDecodeError:
            continue
    raise LLMError(f"model did not return valid JSON (first 300 chars: {cleaned[:300]!r})")


class LLMClient:
    def __init__(self, settings: Settings, calls: list[LLMResponse] | None = None):
        self.settings = settings
        self.calls: list[LLMResponse] = calls if calls is not None else []

    async def complete(
        self, *, system: str, user: str, model: str, purpose: str, temperature: float = 0.1
    ) -> LLMResponse:
        if self.settings.llm_backend == "opencode":
            response = await self._complete_opencode(system=system, user=user, model=model, purpose=purpose)
        elif self.settings.llm_backend == "openai_compatible":
            response = await self._complete_openai(
                system=system, user=user, model=model, purpose=purpose, temperature=temperature
            )
        else:
            raise LLMError(f"unknown LLM_BACKEND: {self.settings.llm_backend}")
        self.calls.append(response)
        return response

    async def _complete_opencode(self, *, system: str, user: str, model: str, purpose: str) -> LLMResponse:
        prompt = f"{system}\n\n---\n\nUSER TASK:\n{user}"
        agent_file = self.settings.base_dir / ".opencode" / "agents" / "json-only.md"
        if not agent_file.exists():
            raise LLMError(f"missing tool-less OpenCode agent: {agent_file}")
        cmd = [
            "opencode",
            "run",
            "--agent",
            "json-only",
            "-m",
            model,
            "--format",
            "json",
            "--title",
            f"research:{purpose}",
            prompt,
        ]
        started = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(self.settings.base_dir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout_b, stderr_b = await asyncio.wait_for(
                    proc.communicate(), timeout=self.settings.llm_timeout_seconds
                )
            except TimeoutError as exc:
                proc.kill()
                await proc.communicate()
                raise _TransientLLMError(f"opencode run timed out after {self.settings.llm_timeout_seconds}s") from exc
        except FileNotFoundError as exc:
            raise LLMError("`opencode` executable not found on PATH") from exc

        duration = time.monotonic() - started
        stdout = stdout_b.decode("utf-8", "replace")
        stderr = stderr_b.decode("utf-8", "replace")
        text_parts: list[str] = []
        tokens = {"input": 0, "output": 0, "reasoning": 0}
        cost = 0.0
        errors: list[str] = []
        session_id: str | None = None
        for line in stdout.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            etype = event.get("type")
            session_id = session_id or event.get("sessionID")
            if etype == "text":
                part = event.get("part") or {}
                if part.get("text"):
                    text_parts.append(part["text"])
            elif etype == "step_finish":
                part = event.get("part") or {}
                usage = part.get("tokens") or {}
                for key in tokens:
                    tokens[key] += int(usage.get(key) or 0)
                cost += float(part.get("cost") or 0)
            elif etype == "error":
                errors.append(json.dumps(event.get("error") or event)[:500])

        if proc.returncode != 0 or errors:
            raise _TransientLLMError(
                f"opencode run failed (rc={proc.returncode}): {'; '.join(errors) or stderr[-800:]}"
            )
        text = "".join(text_parts).strip()
        if not text:
            raise LLMError(f"opencode run returned no text; stderr={stderr[-800:]}")
        usage = await self._opencode_session_usage(session_id) if session_id else None
        if usage:
            tokens, cost = usage
        return LLMResponse(
            text=text,
            model=model,
            purpose=purpose,
            duration_s=duration,
            tokens=tokens,
            cost_usd=round(cost, 8) if cost else None,
        )

    async def _opencode_session_usage(self, session_id: str) -> tuple[dict[str, int], float] | None:
        """Read exact token/cost usage from the local OpenCode session API.

        The CLI's event stream omits step_finish for custom agents, but the session
        context exposes per-assistant-message cost and tokens. This is best-effort:
        missing usage never fails a research call.
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                "opencode",
                "api",
                "get",
                f"/api/session/{session_id}/context",
                cwd=str(self.settings.base_dir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_b, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
        except Exception:
            return None
        if proc.returncode != 0:
            return None
        try:
            payload = json.loads(stdout_b.decode("utf-8", "replace"))
        except json.JSONDecodeError:
            return None
        tokens = {"input": 0, "output": 0, "reasoning": 0}
        cost = 0.0
        for message in payload.get("data") or []:
            if message.get("type") != "assistant":
                continue
            usage = message.get("tokens") or {}
            for key in tokens:
                tokens[key] += int(usage.get(key) or 0)
            cost += float(message.get("cost") or 0)
        return tokens, cost

    async def _complete_openai(
        self, *, system: str, user: str, model: str, purpose: str, temperature: float
    ) -> LLMResponse:
        headers = {"Authorization": f"Bearer {self.settings.openai_api_key}"}
        payload = {
            "model": model,
            "temperature": temperature,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        }
        started = time.monotonic()
        async with httpx.AsyncClient(timeout=self.settings.llm_timeout_seconds) as client:
            response = await client.post(
                f"{self.settings.openai_base_url}/chat/completions", headers=headers, json=payload
            )
        duration = time.monotonic() - started
        if response.status_code >= 400:
            if response.status_code in (429, 500, 502, 503, 504):
                raise _TransientLLMError(f"model API {response.status_code}: {response.text[:500]}")
            raise LLMError(f"model API {response.status_code}: {response.text[:500]}")
        data = response.json()
        text = data["choices"][0]["message"]["content"]
        usage = data.get("usage") or {}
        return LLMResponse(
            text=text,
            model=model,
            purpose=purpose,
            duration_s=duration,
            tokens={
                "input": int(usage.get("prompt_tokens") or 0),
                "output": int(usage.get("completion_tokens") or 0),
                "reasoning": 0,
            },
            cost_usd=None,
        )

    async def _with_retries(self, fn, **kwargs) -> LLMResponse:
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(4),
            wait=wait_exponential(multiplier=2, min=2, max=30),
            retry=retry_if_exception_type(_TransientLLMError),
            reraise=True,
        ):
            with attempt:
                return await fn(**kwargs)
        raise LLMError("unreachable")

    async def complete_resilient(
        self, *, system: str, user: str, model: str, purpose: str, temperature: float = 0.1
    ) -> LLMResponse:
        return await self._with_retries(
            self.complete, system=system, user=user, model=model, purpose=purpose, temperature=temperature
        )

    async def complete_json(
        self,
        *,
        system: str,
        user: str,
        model: str,
        purpose: str,
        temperature: float = 0.1,
    ) -> tuple[dict[str, Any], list[LLMResponse]]:
        calls: list[LLMResponse] = []
        response = await self.complete_resilient(
            system=system, user=user, model=model, purpose=purpose, temperature=temperature
        )
        calls.append(response)
        try:
            value = _try_json(response.text)
        except LLMError as first_error:
            repair_user = (
                f"{user}\n\n---\n\nYour previous output was invalid JSON:\n{response.text[:4000]}\n\n"
                f"Parser error: {first_error}\nReturn ONLY one valid JSON object, no fences, no commentary."
            )
            response2 = await self.complete_resilient(
                system=system, user=repair_user, model=model, purpose=f"{purpose}:json-repair"
            )
            calls.append(response2)
            value = _try_json(response2.text)
        if not isinstance(value, dict):
            raise LLMError(f"{purpose}: expected a JSON object, got {type(value).__name__}")
        return value, calls
