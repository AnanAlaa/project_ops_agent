"""OpenAI Agents SDK wrappers for the guardrails in app/guardrails.py.

Attach to agents like this:
    Agent(..., input_guardrails=[input_guard])            # first agent in the chain (extraction)
    Agent(..., output_guardrails=[output_guard])          # the status writer (output_type=StatusReport)

When a guardrail fires, the SDK raises InputGuardrailTripwireTriggered / OutputGuardrailTripwireTriggered.
Catch those in the workflow, show the reason to the user and write it to the audit log.
"""
from __future__ import annotations

from typing import Any

from agents import GuardrailFunctionOutput, RunContextWrapper, input_guardrail, output_guardrail

from app import guardrails
from app.context import ToolContext
from app.models import StatusReport


def _flatten(input_value: Any) -> str:
    """The SDK passes either a string or a list of input items."""
    if isinstance(input_value, str):
        return input_value
    parts: list[str] = []
    for item in input_value or []:
        content = item.get("content") if isinstance(item, dict) else getattr(item, "content", None)
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for c in content:
                text = c.get("text") if isinstance(c, dict) else getattr(c, "text", None)
                if text:
                    parts.append(text)
    return "\n".join(parts)


@input_guardrail
async def input_guard(ctx: RunContextWrapper[ToolContext], agent: Any, input: Any) -> GuardrailFunctionOutput:
    result = guardrails.check_input(_flatten(input))
    return GuardrailFunctionOutput(output_info=result.as_dict(), tripwire_triggered=not result.allowed)


@output_guardrail
async def output_guard(ctx: RunContextWrapper[ToolContext], agent: Any, output: Any) -> GuardrailFunctionOutput:
    if not isinstance(output, StatusReport):
        try:
            output = StatusReport.model_validate(output)
        except Exception:
            info = {"allowed": False, "category": "invalid_output", "reason": "Output is not a valid StatusReport."}
            return GuardrailFunctionOutput(output_info=info, tripwire_triggered=True)
    result = guardrails.check_output(output, ctx.context)
    return GuardrailFunctionOutput(output_info=result.as_dict(), tripwire_triggered=not result.allowed)
