# -*- coding: utf-8 -*-
"""Background skill evolution: reuse session history + skill-only tools."""

from __future__ import annotations

import asyncio
import copy
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agentscope.message import Msg

from qwenpaw.config.context import (
    set_current_recent_max_bytes,
    set_current_workspace_dir,
)
from qwenpaw.constant import WORKING_DIR

from qwenpaw.extensions.hermes_auto_skill.skill_tools import (
    skill_workspace_mutation_occurred,
)

if TYPE_CHECKING:
    from qwenpaw.config.config import AgentProfileConfig

logger = logging.getLogger(__name__)

_SKILL_REVIEW_USER = (
    "Review the conversation above and consider saving or updating a skill "
    "if appropriate.\n\n"
    "Focus on: was a non-trivial approach used that required trial and error, "
    "or did the user expect a reusable procedure?\n\n"
    "You may use skills_list, skill_view, and skill_manage (create, edit, "
    "patch, delete, write_file, remove_file) on this workspace's skills.\n"
    "Do not modify builtin skills. If nothing should be saved, reply briefly "
    "that there is nothing to do and avoid calling tools."
)


async def run_skill_evolution_background(
    *,
    agent_config: "AgentProfileConfig",
    workspace_dir: Path | None,
    request_context: dict[str, str],
    history_messages: list[Msg],
    agent_id: str,
    manager: Any | None,
    reload_after_mutation: bool = True,
    max_history_messages: int = 80,
) -> None:
    """Run a skill-only reviewer agent on a copy of the session history."""
    token = skill_workspace_mutation_occurred.set(False)
    try:
        wd = Path(workspace_dir or WORKING_DIR).expanduser()
        set_current_workspace_dir(wd)
        set_current_recent_max_bytes(
            agent_config.running.tool_result_compact.recent_max_bytes,
        )

        from qwenpaw.agents.react_agent import QwenPawAgent

        trimmed = history_messages[-max_history_messages:]

        reviewer = QwenPawAgent(
            agent_config=agent_config,
            env_context=None,
            enable_memory_manager=False,
            mcp_clients=[],
            memory_manager=None,
            request_context=dict(request_context),
            namesake_strategy="skip",
            workspace_dir=wd,
            task_tracker=None,
            skill_evolution_reviewer=True,
        )
        await reviewer.register_mcp_clients()
        reviewer.set_console_output_enabled(enabled=False)

        for msg in trimmed:
            await reviewer.memory.add(copy.deepcopy(msg))

        review_msg = Msg(
            name="user",
            role="user",
            content=_SKILL_REVIEW_USER,
        )
        await reviewer.reply(review_msg)

        mutated = skill_workspace_mutation_occurred.get()
        if mutated and reload_after_mutation and manager is not None:
            try:
                await manager.reload_agent(agent_id)
            except Exception as exc:
                logger.warning(
                    "Skill evolution: reload_agent failed for %s: %s",
                    agent_id,
                    exc,
                    exc_info=True,
                )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.debug(
            "Skill evolution background task failed: %s",
            exc,
            exc_info=True,
        )
    finally:
        skill_workspace_mutation_occurred.reset(token)
