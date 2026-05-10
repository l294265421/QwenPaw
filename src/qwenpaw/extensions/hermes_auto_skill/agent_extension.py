# -*- coding: utf-8 -*-
"""Subclass ``QwenPawAgent`` with workspace skill tools + reviewer-only toolkit."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, List, Literal, Optional

from agentscope.tool import Toolkit

from qwenpaw.agents.react_agent import QwenPawAgent as _QwenPawAgentBase
from qwenpaw.agents.tools import (
    browser_use,
    chat_with_agent,
    check_agent_task,
    delegate_external_agent,
    desktop_screenshot,
    edit_file,
    execute_shell_command,
    get_current_time,
    get_token_usage,
    glob_search,
    grep_search,
    list_agents,
    read_file,
    send_file_to_user,
    set_user_timezone,
    submit_to_agent,
    view_image,
    view_video,
    write_file,
)
from qwenpaw.agents.prompt import get_active_model_supports_multimodal

from qwenpaw.extensions.hermes_auto_skill.prompt_extra import SKILLS_GUIDANCE
from qwenpaw.extensions.hermes_auto_skill.skill_tools import (
    skill_manage,
    skill_view,
    skills_list,
)

if TYPE_CHECKING:
    from qwenpaw.agents.memory import BaseMemoryManager
    from qwenpaw.config.config import AgentProfileConfig

logger = logging.getLogger(__name__)

NamesakeStrategy = Literal["override", "skip", "raise", "rename"]

# Optional workspace Hermes tools: not part of default builtin_tools; main agent
# registers them only when explicitly present with enabled=True. Skill evolution
# reviewer always registers these regardless of config.
_HERMES_SKILL_TOOL_NAMES = frozenset(
    {"skills_list", "skill_view", "skill_manage"},
)


class HermesSkillQwenPawAgent(_QwenPawAgentBase):
    """Optional Hermes skill tools on the main agent; reviewer-only toolkit for evolution."""

    def __init__(
        self,
        agent_config: "AgentProfileConfig",
        env_context: Optional[str] = None,
        enable_memory_manager: bool = True,
        mcp_clients: Optional[List[Any]] = None,
        memory_manager: "BaseMemoryManager | None" = None,
        request_context: Optional[dict[str, str]] = None,
        namesake_strategy: NamesakeStrategy = "skip",
        workspace_dir: Path | None = None,
        task_tracker: Any | None = None,
        skill_evolution_reviewer: bool = False,
    ) -> None:
        self._skill_evolution_reviewer = skill_evolution_reviewer
        super().__init__(
            agent_config=agent_config,
            env_context=env_context,
            enable_memory_manager=enable_memory_manager,
            mcp_clients=mcp_clients,
            memory_manager=memory_manager,
            request_context=request_context,
            namesake_strategy=namesake_strategy,
            workspace_dir=workspace_dir,
            task_tracker=task_tracker,
        )
        if skill_evolution_reviewer:
            rc = self._agent_config.running
            self.max_iters = getattr(rc, "skill_auto_evolution_max_iters", 8)

    def _hermes_skill_tool_enabled_for_main(self, tool_name: str) -> bool:
        """True only if this Hermes tool is listed under builtin_tools with enabled=True."""
        if tool_name not in _HERMES_SKILL_TOOL_NAMES:
            return False
        try:
            if hasattr(self._agent_config, "tools") and hasattr(
                self._agent_config.tools,
                "builtin_tools",
            ):
                builtin_tools = self._agent_config.tools.builtin_tools
                if tool_name not in builtin_tools:
                    return False
                return bool(builtin_tools[tool_name].enabled)
        except Exception:
            return False
        return False

    def _build_sys_prompt(self) -> str:
        if self._skill_evolution_reviewer:
            return super()._build_sys_prompt()
        if not self._hermes_skill_tool_enabled_for_main("skill_manage"):
            return super()._build_sys_prompt()
        saved_ctx = self._env_context
        self._env_context = None
        try:
            core = super()._build_sys_prompt()
        finally:
            self._env_context = saved_ctx
        core = core + "\n\n" + SKILLS_GUIDANCE
        if saved_ctx is not None:
            core = core + "\n\n" + saved_ctx
        return core

    def _create_toolkit(
        self,
        namesake_strategy: NamesakeStrategy = "skip",
    ) -> Toolkit:
        toolkit = Toolkit()

        enabled_tools: dict[str, bool] = {}
        async_execution_tools: dict[str, bool] = {}
        try:
            if hasattr(self._agent_config, "tools") and hasattr(
                self._agent_config.tools,
                "builtin_tools",
            ):
                builtin_tools = self._agent_config.tools.builtin_tools
                enabled_tools = {
                    name: tool.enabled for name, tool in builtin_tools.items()
                }
                async_execution_tools = {
                    "execute_shell_command": builtin_tools.get(
                        "execute_shell_command",
                    ).async_execution
                    if "execute_shell_command" in builtin_tools
                    else False,
                }
        except Exception as e:
            logger.warning(
                "Failed to load agent tools config: %s, "
                "all tools will be disabled",
                e,
            )

        tool_functions = {
            "execute_shell_command": execute_shell_command,
            "read_file": read_file,
            "write_file": write_file,
            "edit_file": edit_file,
            "grep_search": grep_search,
            "glob_search": glob_search,
            "browser_use": browser_use,
            "desktop_screenshot": desktop_screenshot,
            "view_image": view_image,
            "view_video": view_video,
            "send_file_to_user": send_file_to_user,
            "get_current_time": get_current_time,
            "set_user_timezone": set_user_timezone,
            "get_token_usage": get_token_usage,
            "delegate_external_agent": delegate_external_agent,
            "list_agents": list_agents,
            "chat_with_agent": chat_with_agent,
            "submit_to_agent": submit_to_agent,
            "check_agent_task": check_agent_task,
            "skills_list": skills_list,
            "skill_view": skill_view,
            "skill_manage": skill_manage,
        }

        multimodal = get_active_model_supports_multimodal()
        _skill_only = getattr(self, "_skill_evolution_reviewer", False)

        for tool_name, tool_func in tool_functions.items():
            if _skill_only and tool_name not in _HERMES_SKILL_TOOL_NAMES:
                continue
            if tool_name in _HERMES_SKILL_TOOL_NAMES:
                if not _skill_only and not self._hermes_skill_tool_enabled_for_main(
                    tool_name,
                ):
                    logger.debug(
                        "Skipped Hermes skill tool (not enabled in builtin_tools): %s",
                        tool_name,
                    )
                    continue
            elif not enabled_tools.get(tool_name, True):
                logger.debug("Skipped disabled tool: %s", tool_name)
                continue

            if tool_name in ("view_image", "view_video") and not multimodal:
                logger.debug(
                    "Skipped %s — model does not support multimodal",
                    tool_name,
                )
                continue

            async_exec = async_execution_tools.get(tool_name, False)

            toolkit.register_tool_function(
                tool_func,
                namesake_strategy=namesake_strategy,
                async_execution=async_exec,
            )
            logger.debug(
                "Registered tool: %s (async_execution=%s)",
                tool_name,
                async_exec,
            )

        has_async_tools = (
            not _skill_only
            and any(
                async_execution_tools.get(name, False)
                for name in tool_functions
                if enabled_tools.get(name, True)
                and name not in _HERMES_SKILL_TOOL_NAMES
            )
        )
        if has_async_tools:
            try:
                toolkit.register_tool_function(
                    toolkit.view_task,
                    namesake_strategy=namesake_strategy,
                )
                toolkit.register_tool_function(
                    toolkit.wait_task,
                    namesake_strategy=namesake_strategy,
                )
                toolkit.register_tool_function(
                    toolkit.cancel_task,
                    namesake_strategy=namesake_strategy,
                )
                logger.debug(
                    "Registered background task management tools "
                    "(view_task, wait_task, cancel_task)",
                )
            except Exception as e:
                logger.warning(
                    "Failed to register task management tools: %s",
                    e,
                )

        return toolkit
