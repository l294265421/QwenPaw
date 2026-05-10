# -*- coding: utf-8 -*-
"""Post-turn scheduling for background skill evolution (runner integration)."""

from __future__ import annotations

import asyncio
import copy
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from qwenpaw.config.config import AgentProfileConfig

logger = logging.getLogger(__name__)


async def schedule_skill_auto_evolution_after_turn(
    runner: Any,
    agent: Any,
    agent_config: "AgentProfileConfig",
) -> None:
    """Fire-and-forget reviewer when ``skill_auto_evolution_enabled`` is set."""
    running = agent_config.running
    if not getattr(running, "skill_auto_evolution_enabled", False):
        return

    hist = await agent.memory.get_memory()
    max_hist = getattr(
        running,
        "skill_auto_evolution_max_history_messages",
        80,
    )
    hist_copy = copy.deepcopy(hist[-max_hist:])

    min_tool_calls = max(
        0,
        int(
            getattr(running, "skill_auto_evolution_min_tool_calls", 0),
        ),
    )
    should_schedule = True
    if min_tool_calls > 0:
        tool_call_count = 0
        for msg in hist_copy:
            msg_content = getattr(msg, "content", None)
            if isinstance(msg_content, list):
                for block in msg_content:
                    if (
                        isinstance(block, dict)
                        and block.get("type") == "tool_use"
                    ):
                        tool_call_count += 1
        if tool_call_count < min_tool_calls:
            should_schedule = False
            logger.debug(
                "skip skill_auto_evolution: tool calls %s < %s",
                tool_call_count,
                min_tool_calls,
            )

    if not should_schedule:
        return

    try:
        from qwenpaw.extensions.hermes_auto_skill.skill_evolution import (
            run_skill_evolution_background,
        )

        asyncio.create_task(
            run_skill_evolution_background(
                agent_config=agent_config,
                workspace_dir=getattr(agent, "_workspace_dir", None),
                request_context=dict(getattr(agent, "_request_context", {})),
                history_messages=hist_copy,
                agent_id=getattr(runner, "agent_id", "default"),
                manager=getattr(runner, "_manager", None),
                reload_after_mutation=getattr(
                    running,
                    "skill_auto_evolution_reload",
                    True,
                ),
                max_history_messages=max_hist,
            ),
        )
    except Exception:
        logger.debug(
            "schedule skill_auto_evolution failed",
            exc_info=True,
        )
