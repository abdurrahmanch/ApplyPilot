"""
Unified LLM client for ApplyPilot.

Auto-detects provider from environment:
  GEMINI_API_KEY    -> Google Gemini (primary)
  OPENAI_API_KEY    -> OpenAI (fallback)
  ANTHROPIC_API_KEY -> Anthropic (fallback)
  LLM_URL           -> Local llama.cpp / Ollama compatible endpoint

LLM_MODEL env var overrides the default (fast) model for any provider.
LLM_MODEL_QUALITY env var sets a higher-quality model for critical steps
(resume tailoring, cover letters). Falls back to LLM_MODEL if not set.

When a model hits a 429 rate limit, the client automatically tries the
next model in the fallback chain — including cross-provider fallback to
OpenAI and Anthropic if their API keys are configured.
"""

import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass

import httpx

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Model registry — each entry knows its provider, endpoint, and API key
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ModelEntry:
    """A model with everything needed to call it."""
    name: str
    provider: str           # "gemini", "openai", "anthropic", "local"
    base_url: str
    api_key: str


# ---------------------------------------------------------------------------
# Claude Code CLI provider
#
# Arch's deployment has no Anthropic API key: every LLM stage runs through the
# locally installed `claude` binary, which authenticates via his existing
# subscription. See DECISIONS.md D1.
# ---------------------------------------------------------------------------

CLAUDE_CLI_FAST_DEFAULT = "haiku"
CLAUDE_CLI_QUALITY_DEFAULT = "sonnet"

# Wall-clock ceiling for one `claude -p` invocation. Generous: batch scoring
# calls carry many jobs per prompt and runtime is explicitly not a constraint.
_CLI_TIMEOUT = 900


def claude_cli_available() -> bool:
    """True when the `claude` binary is on PATH."""
    return shutil.which("claude") is not None


def _claude_cli_chain(quality: bool = False) -> list[ModelEntry]:
    """Build the CLI-backed chain. Quality tier degrades to the fast model."""
    fast = os.environ.get("CLAUDE_CLI_MODEL", CLAUDE_CLI_FAST_DEFAULT)
    strong = os.environ.get("CLAUDE_CLI_MODEL_QUALITY", CLAUDE_CLI_QUALITY_DEFAULT)
    models = [strong, fast] if quality else [fast]
    # dict.fromkeys dedupes while preserving order (fast == strong is legal)
    return [ModelEntry(m, "claude_cli", "", "") for m in dict.fromkeys(models)]


def _flatten_messages(messages: list[dict]) -> str:
    """Collapse a chat-style message list into one prompt string.

    `claude -p` takes a single prompt, so system messages are hoisted into a
    leading block and any multi-turn history is rendered as labelled sections.
    A lone user message passes through untouched.
    """
    system = [m["content"] for m in messages if m.get("role") == "system"]
    rest = [m for m in messages if m.get("role") != "system"]

    parts: list[str] = []
    if system:
        parts.append("\n\n".join(system))
    if len(rest) == 1:
        parts.append(rest[0]["content"])
    else:
        for m in rest:
            label = "Assistant" if m.get("role") == "assistant" else "User"
            parts.append(f"{label}:\n{m['content']}")
    return "\n\n".join(parts).strip()


def _build_fallback_chain(primary_model: str, quality: bool = False) -> list[ModelEntry]:
    """Build a cross-provider fallback chain starting from the primary model.

    Gemini models come first (free tier), then OpenAI (cheap), then Anthropic.
    Only includes providers whose API keys are configured.
    """
    gemini_key = os.environ.get("GEMINI_API_KEY", "")
    openai_key = os.environ.get("OPENAI_API_KEY", "")
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
    deepseek_key = os.environ.get("DEEPSEEK_API_KEY", "")
    gemini_url = "https://generativelanguage.googleapis.com/v1beta/openai"
    openai_url = "https://api.openai.com/v1"
    anthropic_url = "https://api.anthropic.com"
    deepseek_url = "https://api.deepseek.com/v1"

    # Gemini chains — use verified model IDs only
    if quality:
        gemini_models = [
            "gemini-2.5-pro",
            "gemini-2.5-flash",
            "gemini-2.0-flash",
        ]
    else:
        gemini_models = [
            "gemini-2.5-flash",
            "gemini-2.0-flash",
            "gemini-2.0-flash-lite",
        ]

    # OpenAI fallbacks (cost-efficient)
    if quality:
        openai_models = ["gpt-4.1-mini", "gpt-4.1-nano"]
    else:
        openai_models = ["gpt-4.1-nano", "gpt-4.1-mini"]

    # Anthropic fallbacks (cost-efficient)
    if quality:
        anthropic_models = ["claude-sonnet-4-5-20250514", "claude-haiku-4-5-20251001"]
    else:
        anthropic_models = ["claude-haiku-4-5-20251001"]

    chain: list[ModelEntry] = []

    # Start from the primary model in the Gemini chain
    if gemini_key:
        started = False
        for m in gemini_models:
            if m == primary_model:
                started = True
            if started:
                chain.append(ModelEntry(m, "gemini", gemini_url, gemini_key))
        # If primary wasn't found in chain, add full chain
        if not started:
            chain.append(ModelEntry(primary_model, "gemini", gemini_url, gemini_key))
            for m in gemini_models:
                if m != primary_model:
                    chain.append(ModelEntry(m, "gemini", gemini_url, gemini_key))

    # DeepSeek fallbacks (cheap, OpenAI-compatible)
    if quality:
        deepseek_models = ["deepseek-chat"]
    else:
        deepseek_models = ["deepseek-chat"]

    # OpenAI fallbacks
    if openai_key:
        for m in openai_models:
            chain.append(ModelEntry(m, "openai", openai_url, openai_key))

    # DeepSeek fallbacks
    if deepseek_key:
        for m in deepseek_models:
            chain.append(ModelEntry(m, "deepseek", deepseek_url, deepseek_key))

    # Anthropic fallbacks
    if anthropic_key:
        for m in anthropic_models:
            chain.append(ModelEntry(m, "anthropic", anthropic_url, anthropic_key))

    # No API keys anywhere -> fall back to the local Claude Code CLI, which
    # authenticates through the user's existing Claude subscription.
    if not chain and claude_cli_available():
        chain = _claude_cli_chain(quality=quality)

    # If nothing was added (no keys, no CLI), raise
    if not chain:
        raise RuntimeError(
            "No LLM provider configured. "
            "Set GEMINI_API_KEY, OPENAI_API_KEY, DEEPSEEK_API_KEY, or ANTHROPIC_API_KEY, "
            "or install the Claude Code CLI (`claude`) and sign in."
        )

    return chain


# ---------------------------------------------------------------------------
# Provider detection (for primary model selection)
# ---------------------------------------------------------------------------

def _detect_provider(quality: bool = False) -> tuple[str, str, str]:
    """Return (base_url, model, api_key) for the primary provider."""
    gemini_key = os.environ.get("GEMINI_API_KEY", "")
    openai_key = os.environ.get("OPENAI_API_KEY", "")
    local_url = os.environ.get("LLM_URL", "")

    model_override = os.environ.get("LLM_MODEL", "")
    quality_model = os.environ.get("LLM_MODEL_QUALITY", "")

    if quality and quality_model:
        chosen_model = quality_model
    else:
        chosen_model = model_override

    if gemini_key and not local_url:
        return (
            "https://generativelanguage.googleapis.com/v1beta/openai",
            chosen_model or "gemini-2.5-flash",
            gemini_key,
        )
    if openai_key and not local_url:
        return (
            "https://api.openai.com/v1",
            chosen_model or "gpt-4.1-nano",
            openai_key,
        )
    if local_url:
        return (
            local_url.rstrip("/"),
            chosen_model or "local-model",
            os.environ.get("LLM_API_KEY", ""),
        )
    if claude_cli_available():
        default = (CLAUDE_CLI_QUALITY_DEFAULT if quality
                   else CLAUDE_CLI_FAST_DEFAULT)
        env_key = "CLAUDE_CLI_MODEL_QUALITY" if quality else "CLAUDE_CLI_MODEL"
        return ("", os.environ.get(env_key, default), "")
    raise RuntimeError(
        "No LLM provider configured. "
        "Set GEMINI_API_KEY, OPENAI_API_KEY, or LLM_URL, "
        "or install the Claude Code CLI (`claude`) and sign in."
    )


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

_MAX_RETRIES = 3
_TIMEOUT = 300  # seconds


class LLMClient:
    """Multi-provider LLM client with automatic model fallback."""

    def __init__(self, base_url: str, model: str, api_key: str,
                 quality: bool = False) -> None:
        self.base_url = base_url
        self.model = model
        self.api_key = api_key
        self.quality = quality
        self._fallback_chain = _build_fallback_chain(model, quality=quality)
        self._client = httpx.Client(timeout=_TIMEOUT)
        # Track which models are temporarily exhausted (daily limit)
        self._exhausted: dict[str, float] = {}

        chain_names = [f"{e.name} ({e.provider})" for e in self._fallback_chain]
        log.info("Fallback chain (%s): %s",
                 "quality" if quality else "fast", " -> ".join(chain_names))

    def chat(
        self,
        messages: list[dict],
        temperature: float = 0.0,
        max_tokens: int = 4096,
    ) -> str:
        """Send a chat completion request with automatic cross-provider fallback."""
        # Qwen3 optimization
        if "qwen" in self.model.lower() and messages:
            first = messages[0]
            if first.get("role") == "user" and not first["content"].startswith("/no_think"):
                messages = [{"role": first["role"], "content": f"/no_think\n{first['content']}"}] + messages[1:]

        # Build list of models to try: skip recently exhausted ones
        now = time.time()
        entries_to_try = [
            e for e in self._fallback_chain
            if e.name not in self._exhausted or (now - self._exhausted[e.name]) > 300
        ]
        if not entries_to_try:
            self._exhausted.clear()
            entries_to_try = list(self._fallback_chain)

        for idx, entry in enumerate(entries_to_try):
            is_last = (idx == len(entries_to_try) - 1)
            result = self._try_entry(entry, messages, temperature, max_tokens, is_last)
            if result is not None:
                return result

        raise RuntimeError(
            f"All models exhausted after trying: "
            f"{[e.name for e in entries_to_try]}. "
            "Wait a few minutes for rate limits to reset."
        )

    def _try_entry(self, entry: ModelEntry, messages: list[dict],
                   temperature: float, max_tokens: int,
                   is_last: bool = False) -> str | None:
        """Try a single model entry. Dispatches to the right provider."""
        if entry.provider == "claude_cli":
            return self._try_claude_cli(entry, messages, is_last)
        elif entry.provider == "anthropic":
            return self._try_anthropic(entry, messages, temperature, max_tokens, is_last)
        else:
            return self._try_openai_compat(entry, messages, temperature, max_tokens, is_last)

    def _try_claude_cli(self, entry: ModelEntry, messages: list[dict],
                        is_last: bool = False) -> str | None:
        """Run one generation through the local `claude` binary.

        Temperature and max_tokens have no CLI equivalent and are ignored.
        The subprocess runs in an empty temp directory with MCP disabled so it
        picks up no project CLAUDE.md, no MCP servers, and no repo context —
        only the prompt we pass on stdin.
        """
        prompt = _flatten_messages(messages)

        for attempt in range(_MAX_RETRIES):
            try:
                with tempfile.TemporaryDirectory(prefix="applypilot-llm-") as workdir:
                    proc = subprocess.run(
                        [
                            "claude", "-p",
                            "--model", entry.name,
                            "--output-format", "json",
                            "--strict-mcp-config",
                            "--mcp-config", '{"mcpServers":{}}',
                        ],
                        input=prompt,
                        capture_output=True,
                        text=True,
                        cwd=workdir,
                        timeout=_CLI_TIMEOUT,
                        check=False,
                    )
            except subprocess.TimeoutExpired:
                log.warning("claude_cli/%s timed out after %ss (attempt %d/%d)",
                            entry.name, _CLI_TIMEOUT, attempt + 1, _MAX_RETRIES)
                continue

            if proc.returncode != 0:
                log.warning("claude_cli/%s exited %d: %.200s",
                            entry.name, proc.returncode, proc.stderr)
                if not is_last:
                    return None
                continue

            try:
                payload = json.loads(proc.stdout)
            except json.JSONDecodeError:
                log.warning("claude_cli/%s returned non-JSON: %.200s",
                            entry.name, proc.stdout)
                continue

            if payload.get("is_error"):
                log.warning("claude_cli/%s reported an error: %.200s",
                            entry.name, str(payload.get("result", "")))
                if not is_last:
                    return None
                continue

            text = payload.get("result", "")
            cost = payload.get("total_cost_usd")
            if cost is not None:
                log.info("claude_cli/%s cost $%.4f", entry.name, cost)
            if text:
                return text
            log.warning("claude_cli/%s returned an empty result", entry.name)

        return None

    def _try_openai_compat(self, entry: ModelEntry, messages: list[dict],
                           temperature: float, max_tokens: int,
                           is_last: bool = False) -> str | None:
        """Try an OpenAI-compatible endpoint (Gemini, OpenAI, local)."""
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {entry.api_key}",
        }
        # DeepSeek deepseek-chat has an 8192 max output token limit
        if entry.provider == "deepseek":
            max_tokens = min(max_tokens, 8192)
        payload = {
            "model": entry.name,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        for attempt in range(_MAX_RETRIES):
            try:
                resp = self._client.post(
                    f"{entry.base_url}/chat/completions",
                    json=payload,
                    headers=headers,
                )
                if resp.status_code == 402:
                    # Payment Required — account out of credits; mark exhausted for 1 hour
                    log.warning("%s/%s payment required (402), marking exhausted for 1h",
                                entry.provider, entry.name)
                    self._exhausted[entry.name] = time.time() + 3600 - 300  # 1h from now
                    return None

                if resp.status_code == 400:
                    body = resp.text.lower()
                    if "api_key_invalid" in body or "api key expired" in body:
                        log.warning("%s/%s API key invalid/expired, trying next",
                                    entry.provider, entry.name)
                        self._exhausted[entry.name] = time.time()
                        return None
                    # Any other 400 (content safety, model not found, malformed prompt)
                    # — don't mark exhausted (it's per-request, not a quota), just skip
                    if not is_last:
                        log.warning("%s/%s 400 Bad Request, trying next: %.120s",
                                    entry.provider, entry.name, resp.text)
                        return None

                if resp.status_code == 404:
                    log.warning("%s/%s model not found (404), trying next",
                                entry.provider, entry.name)
                    self._exhausted[entry.name] = time.time()
                    return None

                if resp.status_code == 429:
                    body = resp.text.lower()
                    if "resource has been exhausted" in body or "quota" in body or "rate_limit" in body:
                        log.warning("%s/%s hit quota limit, trying next",
                                    entry.provider, entry.name)
                        self._exhausted[entry.name] = time.time()
                        return None

                    if attempt < _MAX_RETRIES - 1:
                        wait = 2 ** attempt + 1
                        log.warning("%s/%s 429 (RPM), retry in %ds (%d/%d)",
                                    entry.provider, entry.name, wait,
                                    attempt + 1, _MAX_RETRIES)
                        time.sleep(wait)
                        continue
                    elif not is_last:
                        log.warning("%s/%s still 429, trying next model",
                                    entry.provider, entry.name)
                        return None
                    else:
                        resp.raise_for_status()

                if resp.status_code == 503 and attempt < _MAX_RETRIES - 1:
                    wait = 2 ** attempt
                    log.warning("%s/%s 503, retry in %ds", entry.provider, entry.name, wait)
                    time.sleep(wait)
                    continue

                resp.raise_for_status()
                data = resp.json()
                # Guard against malformed responses (null body, null choices, null content)
                if not isinstance(data, dict) or not data.get("choices"):
                    if not is_last:
                        log.warning("%s/%s: malformed response (no choices), trying next",
                                    entry.provider, entry.name)
                        return None
                    raise RuntimeError(
                        f"Malformed response from {entry.provider}/{entry.name}: "
                        f"no choices in {type(data).__name__}"
                    )
                text = data["choices"][0]["message"]["content"]
                if text is None:
                    # Model returned null content (refusal, tool_call, etc.)
                    if not is_last:
                        log.warning("%s/%s: null content in response, trying next",
                                    entry.provider, entry.name)
                        return None
                    raise RuntimeError(
                        f"Null content from {entry.provider}/{entry.name} "
                        f"(refusal: {data['choices'][0]['message'].get('refusal', 'none')})"
                    )

                if entry.name != self.model:
                    log.info("Used fallback %s/%s (primary: %s)",
                             entry.provider, entry.name, self.model)
                return text

            except httpx.TimeoutException:
                if attempt < _MAX_RETRIES - 1:
                    wait = 2 ** attempt
                    log.warning("%s/%s timeout, retry in %ds",
                                entry.provider, entry.name, wait)
                    time.sleep(wait)
                    continue
                if not is_last:
                    log.warning("%s/%s timeout after retries, trying next",
                                entry.provider, entry.name)
                    return None
                raise

        return None

    def _try_anthropic(self, entry: ModelEntry, messages: list[dict],
                       temperature: float, max_tokens: int,
                       is_last: bool = False) -> str | None:
        """Try the Anthropic Messages API (different format from OpenAI)."""
        headers = {
            "Content-Type": "application/json",
            "x-api-key": entry.api_key,
            "anthropic-version": "2023-06-01",
        }

        # Convert OpenAI message format to Anthropic format
        # Extract system message if present
        system_text = ""
        api_messages = []
        for msg in messages:
            if msg["role"] == "system":
                system_text = msg["content"]
            else:
                api_messages.append({
                    "role": msg["role"],
                    "content": msg["content"],
                })

        # Anthropic requires at least one user message
        if not api_messages:
            return None

        payload: dict = {
            "model": entry.name,
            "messages": api_messages,
            "max_tokens": max_tokens,
        }
        if system_text:
            payload["system"] = system_text
        if temperature > 0:
            payload["temperature"] = temperature

        for attempt in range(_MAX_RETRIES):
            try:
                resp = self._client.post(
                    f"{entry.base_url}/v1/messages",
                    json=payload,
                    headers=headers,
                )
                if resp.status_code == 429:
                    body = resp.text.lower()
                    if "rate_limit" in body or "quota" in body:
                        log.warning("anthropic/%s hit rate limit, trying next", entry.name)
                        self._exhausted[entry.name] = time.time()
                        return None

                    if attempt < _MAX_RETRIES - 1:
                        wait = 2 ** attempt + 1
                        log.warning("anthropic/%s 429, retry in %ds (%d/%d)",
                                    entry.name, wait, attempt + 1, _MAX_RETRIES)
                        time.sleep(wait)
                        continue
                    elif not is_last:
                        return None
                    else:
                        resp.raise_for_status()

                if resp.status_code == 529 and attempt < _MAX_RETRIES - 1:
                    # Anthropic overloaded
                    wait = 2 ** attempt + 2
                    log.warning("anthropic/%s overloaded (529), retry in %ds",
                                entry.name, wait)
                    time.sleep(wait)
                    continue

                resp.raise_for_status()
                data = resp.json()

                # Extract text from Anthropic response format
                text_parts = []
                for block in data.get("content", []):
                    if block.get("type") == "text":
                        text_parts.append(block["text"])
                text = "\n".join(text_parts)

                if entry.name != self.model:
                    log.info("Used fallback anthropic/%s (primary: %s)",
                             entry.name, self.model)
                return text

            except httpx.TimeoutException:
                if attempt < _MAX_RETRIES - 1:
                    wait = 2 ** attempt
                    log.warning("anthropic/%s timeout, retry in %ds",
                                entry.name, wait)
                    time.sleep(wait)
                    continue
                if not is_last:
                    return None
                raise

        return None

    def ask(self, prompt: str, **kwargs) -> str:
        """Convenience: single user prompt -> assistant response."""
        return self.chat([{"role": "user", "content": prompt}], **kwargs)

    def close(self) -> None:
        self._client.close()


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_instance: LLMClient | None = None
_quality_instance: LLMClient | None = None


def get_client(quality: bool = False) -> LLMClient:
    """Return (or create) the module-level LLMClient singleton.

    Args:
        quality: If True, return a client configured with LLM_MODEL_QUALITY
                 for critical steps like resume tailoring and cover letters.
    """
    global _instance, _quality_instance

    if quality and os.environ.get("LLM_MODEL_QUALITY"):
        if _quality_instance is None:
            base_url, model, api_key = _detect_provider(quality=True)
            log.info("LLM quality provider: %s  model: %s", base_url, model)
            _quality_instance = LLMClient(base_url, model, api_key, quality=True)
        return _quality_instance

    if _instance is None:
        base_url, model, api_key = _detect_provider()
        log.info("LLM provider: %s  model: %s", base_url, model)
        _instance = LLMClient(base_url, model, api_key, quality=False)
    return _instance
