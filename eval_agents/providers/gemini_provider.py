"""Google Gemini adapter — official `google-genai` SDK."""
from __future__ import annotations

from .base import ChatMessage, ModelResponse, Provider, ToolCall, ToolHistory, ToolSpec, ToolTurn


_SYNTH_ID = "local:"  # prefix for call ids we invent when Gemini omits one


class GeminiProvider(Provider):
    supports_tools = True

    def __init__(self, model: str = "gemini-3.1-pro-preview"):
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
        # Gemini 2.5+ models "think" by default and thinking tokens count
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

    def complete_with_tools(
        self,
        history: ToolHistory,
        tools: list[ToolSpec],
        system: str | None = None,
        max_tokens: int = 4096,
    ) -> ToolTurn:
        from google.genai import types

        contents = []
        for item in history:
            if isinstance(item, ChatMessage):
                contents.append(types.Content(
                    role="model" if item.role == "assistant" else "user",
                    parts=[types.Part(text=item.content)],
                ))
            elif isinstance(item, ToolTurn):
                # Echo the model's Content unchanged: it carries the thought
                # signatures Gemini requires on the next turn.
                contents.append(item.native)
            else:
                contents.append(types.Content(role="user", parts=[
                    types.Part(function_response=types.FunctionResponse(
                        # Echo Gemini's call id when it gave one (synthetic ids stay local).
                        id=None if r.call_id.startswith(_SYNTH_ID) else r.call_id,
                        name=r.name,
                        response={"error": r.content} if r.is_error else {"result": r.content},
                    ))
                    for r in item
                ]))

        thinking_budget = min(1024, max(128, max_tokens // 2))
        resp = self.client.models.generate_content(
            model=self.model,
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=system,
                max_output_tokens=max_tokens,
                thinking_config=types.ThinkingConfig(thinking_budget=thinking_budget),
                tools=[types.Tool(function_declarations=[
                    types.FunctionDeclaration(name=t.name, description=t.description, parameters_json_schema=t.parameters)
                    for t in tools
                ])],
                # We run the loop ourselves against the sandbox.
                automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            ),
        )
        content = resp.candidates[0].content if resp.candidates else None
        parts = (content.parts if content else None) or []
        calls = [
            ToolCall(id=p.function_call.id or f"{_SYNTH_ID}{p.function_call.name}-{i}", name=p.function_call.name,
                     arguments=dict(p.function_call.args or {}))
            for i, p in enumerate(parts) if p.function_call
        ]
        usage = resp.usage_metadata
        return ToolTurn(
            text="".join(p.text for p in parts if p.text and not p.thought),
            model=self.model,
            tool_calls=calls,
            input_tokens=getattr(usage, "prompt_token_count", 0) or 0,
            output_tokens=getattr(usage, "candidates_token_count", 0) or 0,
            native=content if content else types.Content(role="model", parts=[types.Part(text="")]),
        )
