"""Google Gemini adapter — official `google-genai` SDK."""
from __future__ import annotations

from .base import ChatMessage, ModelResponse, Provider


class GeminiProvider(Provider):
    def __init__(self, model: str = "gemini-2.5-pro"):
        super().__init__(model)
        from google import genai

        # Reads GEMINI_API_KEY (or GOOGLE_API_KEY) from the environment.
        self.client = genai.Client()

    def complete(
        self,
        messages: list[ChatMessage],
        system: str | None = None,
        max_tokens: int = 4096,
    ) -> ModelResponse:
        from google.genai import types

        contents = [
            types.Content(
                role="model" if m.role == "assistant" else "user",
                parts=[types.Part(text=m.content)],
            )
            for m in messages
        ]
        # Gemini 2.5 models "think" by default and thinking tokens count
        # against max_output_tokens — with small budgets the visible text can
        # come back empty. Bound thinking to at most half the budget
        # (min 128, the smallest budget 2.5-pro accepts).
        thinking_budget = min(1024, max(128, max_tokens // 2))
        resp = self.client.models.generate_content(
            model=self.model,
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=system,
                max_output_tokens=max_tokens,
                thinking_config=types.ThinkingConfig(thinking_budget=thinking_budget),
            ),
        )
        usage = resp.usage_metadata
        return ModelResponse(
            text=resp.text or "",
            model=self.model,
            input_tokens=getattr(usage, "prompt_token_count", 0) or 0,
            output_tokens=getattr(usage, "candidates_token_count", 0) or 0,
            raw=resp,
        )
