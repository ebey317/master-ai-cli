"""
Model switching logic with reasoning effort integration.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from scripts.model_reasoning import (
    ModelSwitchRequest,
    ReasoningEffort,
    apply_reasoning_effort,
    run_model_picker_with_reasoning,
)
from sensei import config, agent  # type: ignore[attr-defined]


@dataclass
class SwitchResult:
    success: bool
    model: str
    provider: str
    reasoning_effort: Optional[ReasoningEffort] = None
    message: str = ""
    error: Optional[str] = None


async def switch_model(request: ModelSwitchRequest) -> SwitchResult:
    """
    Switch the active model and apply reasoning effort.
    Reasoning is applied AFTER the agent swap so it lands on the rebuilt agent.
    """
    # 1. Perform the model switch
    try:
        await agent.set_model(request.model, provider=request.provider)
    except Exception as e:
        return SwitchResult(
            success=False,
            model=request.model,
            provider=request.provider or "unknown",
            error=f"Model switch failed: {e}",
        )

    # 2. Apply reasoning effort if specified (after swap, per upstream design)
    reasoning_msg = ""
    if request.reasoning_effort is not None:
        persist_global = request.scope == "global"
        reasoning_msg = "\n" + apply_reasoning_effort(
            session_key=agent.get_session_id(),
            effort=request.reasoning_effort,
            scope=request.scope,
            persist_global=persist_global,
        )

    # 3. Build confirmation message
    msg = f"Switched to {request.provider or 'default'} · {request.model}"
    if reasoning_msg:
        msg += reasoning_msg

    return SwitchResult(
        success=True,
        model=request.model,
        provider=request.provider or "default",
        reasoning_effort=request.reasoning_effort,
        message=msg,
    )


async def handle_model_command(args: list[str]) -> SwitchResult:
    """
    Parse and execute /model command with --reasoning flag.
    Usage: /model <model> [--provider <name>] [--reasoning <level>] [--global|--once]
    """
    parser = argparse.ArgumentParser(prog="/model", add_help=False)
    parser.add_argument("model", nargs="?")
    parser.add_argument("--provider")
    parser.add_argument("--global", dest="scope", action="store_const", const="global", default="session")
    parser.add_argument("--once", dest="scope", action="store_const", const="once")
    from scripts.model_reasoning import add_reasoning_arg
    add_reasoning_arg(parser)

    try:
        parsed = parser.parse_args(args)
    except SystemExit:
        return SwitchResult(
            success=False,
            model="",
            provider="",
            error="Usage: /model <model> [--provider <name>] [--reasoning <level>] [--global|--once]",
        )

    if not parsed.model:
        # Launch interactive picker
        request = ModelSwitchRequest(
            model="",
            provider=parsed.provider,
            reasoning_effort=ReasoningEffort.parse(parsed.reasoning) if parsed.reasoning else None,
            scope=parsed.scope,
            explicit_provider=bool(parsed.provider),
        )
        request = await run_model_picker_with_reasoning(request)
    else:
        request = ModelSwitchRequest(
            model=parsed.model,
            provider=parsed.provider,
            reasoning_effort=ReasoningEffort.parse(parsed.reasoning) if parsed.reasoning else None,
            scope=parsed.scope,
            explicit_provider=bool(parsed.provider),
        )

    return await switch_model(request)
