"""Subscription-based adapters — no API keys.

These bridge to the vendors' coding-agent CLIs, which authenticate with a
consumer subscription (or a free account) instead of pay-per-token API keys:

    claude-code  ->  `claude -p`     (Claude Pro/Max subscription)
    codex-cli    ->  `codex exec`    (ChatGPT Plus/Pro subscription)
    gemini-cli   ->  `gemini -p`     (free personal Google account)

Setup (one time each):
    npm install -g @anthropic-ai/claude-code   && claude   # then /login
    npm install -g @openai/codex               && codex login
    npm install -g @google/gemini-cli          && gemini   # OAuth on first run

Trade-offs vs the direct API adapters: you're benchmarking model+agent-CLI
rather than the bare model, latency includes CLI startup, subscription rate
limits apply, and token counts are only reported by Claude Code. Fine for
personal benchmarking; don't route production traffic through these.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile

from .base import ChatMessage, MissingCredentials, ModelResponse, Provider

# Env var overrides let users point at a non-PATH install,
# e.g. CLAUDE_CLI_PATH=~/.claude/local/claude
_BINARY_ENV = {"claude": "CLAUDE_CLI_PATH", "codex": "CODEX_CLI_PATH", "gemini": "GEMINI_CLI_PATH"}

# Passing model="default" omits the model flag and uses the CLI's default.
DEFAULT = "default"


class CliProvider(Provider):
    binary: str
    timeout_s = 600  # agent CLIs can be slow; generous per-call ceiling

    def __init__(self, model: str = DEFAULT):
        super().__init__(model)
        override = os.environ.get(_BINARY_ENV.get(self.binary, ""), "")
        self.binary_path = (
            os.path.expanduser(override) if override else shutil.which(self.binary)
        )
        if not self.binary_path or not os.path.exists(self.binary_path):
            raise MissingCredentials(
                f"{self.binary!r} CLI not found on PATH — install it and log in "
                f"(see eval_agents/providers/cli_providers.py docstring), or set "
                f"{_BINARY_ENV.get(self.binary, 'the binary path env var')}"
            )

    def _run(self, args: list[str], stdin: str | None = None) -> subprocess.CompletedProcess:
        proc = subprocess.run(
            [self.binary_path, *args],
            input=stdin,
            capture_output=True,
            text=True,
            timeout=self.timeout_s,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"{self.binary} exited {proc.returncode}: {proc.stderr.strip()[-400:]}"
            )
        return proc

    @staticmethod
    def _flatten(messages: list[ChatMessage], system: str | None) -> str:
        """Fold system + history into one prompt (the CLIs are single-shot)."""
        parts = []
        if system:
            parts.append(f"System instructions:\n{system}\n---")
        for m in messages:
            prefix = "" if len(messages) == 1 else f"[{m.role}] "
            parts.append(f"{prefix}{m.content}")
        return "\n\n".join(parts)


class ClaudeCodeProvider(CliProvider):
    """`claude -p` — uses a Claude Pro/Max subscription login.

    Model accepts an alias (`opus`, `sonnet`, `haiku`) or a full model ID.
    The JSON output includes real token usage.
    """

    binary = "claude"

    def complete(self, messages, system=None, max_tokens=4096) -> ModelResponse:
        args = ["-p", "--output-format", "json"]
        if self.model != DEFAULT:
            args += ["--model", self.model]
        prompt = self._flatten(messages, None)
        if system:
            args += ["--append-system-prompt", system]

        proc = self._run(args, stdin=prompt)
        data = json.loads(proc.stdout)
        if data.get("is_error"):
            raise RuntimeError(f"claude returned an error result: {data.get('result', '')[:400]}")
        usage = data.get("usage") or {}
        return ModelResponse(
            text=data.get("result", ""),
            model=data.get("modelUsage") and next(iter(data["modelUsage"])) or self.model,
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
            raw=data,
        )


class CodexProvider(CliProvider):
    """`codex exec` — uses a ChatGPT Plus/Pro subscription login."""

    binary = "codex"

    def complete(self, messages, system=None, max_tokens=4096) -> ModelResponse:
        prompt = self._flatten(messages, system)
        with tempfile.NamedTemporaryFile("r", suffix=".md", delete=False) as tmp:
            out_path = tmp.name
        try:
            args = ["exec", "--skip-git-repo-check", "--output-last-message", out_path]
            if self.model != DEFAULT:
                args += ["--model", self.model]
            args.append(prompt)
            self._run(args)
            with open(out_path) as f:
                text = f.read().strip()
        finally:
            os.unlink(out_path)
        return ModelResponse(text=text, model=self.model)


class GeminiCliProvider(CliProvider):
    """`gemini -p` — free with a personal Google account (OAuth on first run)."""

    binary = "gemini"

    def complete(self, messages, system=None, max_tokens=4096) -> ModelResponse:
        args = ["-p", self._flatten(messages, system)]
        if self.model != DEFAULT:
            args += ["--model", self.model]
        proc = self._run(args)
        return ModelResponse(text=proc.stdout.strip(), model=self.model)
