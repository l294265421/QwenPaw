# -*- coding: utf-8 -*-
"""Hermes-style automatic skill evolution (background reviewer agent).

Agent class patching is applied when loading ``QwenPawAgent`` via
``qwenpaw.agents`` (see ``agents.__getattr__``) or when importing
``qwenpaw.extensions.hermes_auto_skill.bootstrap``.
"""

__all__: list[str] = []
