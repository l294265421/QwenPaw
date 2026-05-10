# -*- coding: utf-8 -*-
"""Single side-effect entry: swap ``QwenPawAgent`` for the extended subclass."""

from __future__ import annotations


def patch_agent_class() -> None:
    """Replace ``react_agent.QwenPawAgent`` with :class:`HermesSkillQwenPawAgent`."""
    import qwenpaw.agents.react_agent as ra

    from qwenpaw.extensions.hermes_auto_skill.agent_extension import (
        HermesSkillQwenPawAgent,
    )

    if ra.QwenPawAgent is HermesSkillQwenPawAgent:
        return
    ra.QwenPawAgent = HermesSkillQwenPawAgent


patch_agent_class()
