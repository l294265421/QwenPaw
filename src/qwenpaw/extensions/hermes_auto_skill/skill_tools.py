# -*- coding: utf-8 -*-
# flake8: noqa: E501
# pylint: disable=line-too-long
"""Workspace skill tools: skills_list, skill_view, skill_manage.

These operate on the current agent workspace ``skills/`` directory and
``skill.json`` manifest. Persistence logic lives in
``workspace_skill_store`` (no dependency on :class:`~qwenpaw.agents.skills_manager.SkillService`).
Tool JSON schemas are derived from signatures and docstrings (AgentScope), like
other QwenPaw tools.
"""

import json
import logging
import os
import re
import tempfile
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Literal

import yaml
from agentscope.message import TextBlock
from agentscope.tool import ToolResponse

from .fuzzy_match import fuzzy_find_and_replace
from .skills_guard import SkillScanBlocked
from .workspace_skill_store import (
    create_workspace_skill,
    delete_workspace_skill,
    disable_workspace_skill,
    ensure_skill_dir_exists,
    is_workspace_builtin,
    list_skill_summaries,
    normalize_skill_dir_name,
    read_text_file_with_encoding_fallback,
    resolve_skills_dir,
    save_skill_skill_md,
    scan_skill_dir_or_raise,
    workspace_dir_for_tools,
)

logger = logging.getLogger(__name__)

# Set to True when skill_manage mutates workspace skills (for post-run reload).
skill_workspace_mutation_occurred: ContextVar[bool] = ContextVar(
    "skill_workspace_mutation_occurred",
    default=False,
)

_MAX_SKILL_MD_CHARS = 100_000
_MAX_DESCRIPTION_LENGTH = 1024
_MAX_SUPPORTING_FILE_BYTES = 1_048_576
_ALLOWED_SUBDIRS = frozenset({"references", "templates", "scripts", "assets"})
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")

def _workspace_dir() -> Path:
    return workspace_dir_for_tools()


def _json_tool_response(payload: dict[str, Any]) -> ToolResponse:
    return ToolResponse(
        content=[
            TextBlock(
                type="text",
                text=json.dumps(payload, ensure_ascii=False),
            ),
        ],
    )


def _validate_skill_name(name: str) -> str | None:
    if not name or not name.strip():
        return "Skill name is required."
    n = name.strip()
    if len(n) > 64:
        return "Skill name exceeds 64 characters."
    if not _NAME_RE.match(n):
        return (
            "Invalid skill name. Use lowercase letters, numbers, "
            "hyphens, dots, and underscores; must start with a letter or digit."
        )
    return None


def _validate_support_path(rel: str) -> str | None:
    if not rel or not str(rel).strip():
        return "file_path is required."
    normalized = str(rel).replace("\\", "/").strip()
    if ".." in normalized or normalized.startswith("/"):
        return "Invalid path."
    parts = Path(normalized).parts
    if not parts or parts[0] not in _ALLOWED_SUBDIRS:
        allowed = ", ".join(sorted(_ALLOWED_SUBDIRS))
        return f"Path must start with one of: {allowed}"
    if len(parts) < 2:
        return "Provide a file path under a subdirectory, e.g. references/notes.md"
    return None


def _resolve_under_skill(skill_dir: Path, rel: str) -> tuple[Path | None, str | None]:
    err = _validate_support_path(rel)
    if err:
        return None, err
    normalized = rel.replace("\\", "/").strip()
    target = (skill_dir / normalized).resolve()
    root = skill_dir.resolve()
    try:
        target.relative_to(root)
    except ValueError:
        return None, "Path escapes skill directory."
    return target, None


def _validate_category(category: str | None) -> str | None:
    if category is None:
        return None
    if not isinstance(category, str):
        return "Category must be a string."
    cat = category.strip()
    if not cat:
        return None
    if "/" in cat or "\\" in cat:
        return (
            f"Invalid category '{cat}'. Use lowercase letters, numbers, "
            "hyphens, dots, and underscores. Categories must be a single segment."
        )
    if len(cat) > 64:
        return "Category exceeds 64 characters."
    if not _NAME_RE.match(cat):
        return (
            f"Invalid category '{cat}'. Use lowercase letters, numbers, "
            "hyphens, dots, and underscores."
        )
    return None


def _validate_frontmatter(content: str) -> str | None:
    if not content.strip():
        return "Content cannot be empty."
    if not content.startswith("---"):
        return (
            "SKILL.md must start with YAML frontmatter (---). "
            "See existing skills for format."
        )
    end_match = re.search(r"\n---\s*\n", content[3:])
    if not end_match:
        return "SKILL.md frontmatter is not closed. Ensure you have a closing '---' line."
    yaml_content = content[3 : end_match.start() + 3]
    try:
        parsed = yaml.safe_load(yaml_content)
    except yaml.YAMLError as exc:
        return f"YAML frontmatter parse error: {exc}"
    if not isinstance(parsed, dict):
        return "Frontmatter must be a YAML mapping (key: value pairs)."
    if "name" not in parsed:
        return "Frontmatter must include 'name' field."
    if "description" not in parsed:
        return "Frontmatter must include 'description' field."
    if len(str(parsed["description"])) > _MAX_DESCRIPTION_LENGTH:
        return f"Description exceeds {_MAX_DESCRIPTION_LENGTH} characters."
    body = content[end_match.end() + 3 :].strip()
    if not body:
        return (
            "SKILL.md must have content after the frontmatter "
            "(instructions, procedures, etc.)."
        )
    return None


def _validate_content_size(content: str, label: str = "SKILL.md") -> str | None:
    if len(content) > _MAX_SKILL_MD_CHARS:
        return (
            f"{label} content is {len(content):,} characters "
            f"(limit: {_MAX_SKILL_MD_CHARS:,}). "
            "Consider splitting into supporting files under references/ or templates/."
        )
    return None


def _atomic_write_text(
    file_path: Path,
    content: str,
    encoding: str = "utf-8",
) -> None:
    file_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(
        dir=str(file_path.parent),
        prefix=f".{file_path.name}.tmp.",
        suffix="",
    )
    try:
        with os.fdopen(fd, "w", encoding=encoding) as handle:
            handle.write(content)
        os.replace(temp_path, file_path)
    except Exception:
        try:
            os.unlink(temp_path)
        except OSError:
            logger.error(
                "Failed to remove temporary file %s during atomic write",
                temp_path,
                exc_info=True,
            )
        raise


def _workspace_skill_dir(ws: Path, skill_key: str) -> Path | None:
    """Canonical workspace path: ``<workspace>/skills/<skill_key>/`` (flat layout)."""
    return ensure_skill_dir_exists(ws, skill_key)


def _skill_dir_display(ws: Path, skill_dir: Path) -> str:
    try:
        return str(skill_dir.relative_to(ws))
    except ValueError:
        return str(skill_dir)


def _is_builtin_skill(ws: Path, name: str) -> bool:
    return is_workspace_builtin(ws, name)


async def skills_list(category: str | None = None) -> ToolResponse:
    """List available skills (name, description, source). Use skill_view(name) for full SKILL.md.

    Args:
        category (`str`, optional):
            Optional filter string matched against skill names (substring, case-insensitive).
    """
    try:
        ws = _workspace_dir()
        rows: list[dict[str, Any]] = list_skill_summaries(ws)
        if category and str(category).strip():
            cat = str(category).strip().lower()
            rows = [
                r
                for r in rows
                if cat in (r.get("name") or "").lower()
            ]
        rows.sort(key=lambda r: (r.get("name") or "").lower())
        return _json_tool_response(
            {
                "success": True,
                "skills": rows,
                "count": len(rows),
                "hint": "Use skill_view(name) for full SKILL.md; skill_manage to edit.",
            },
        )
    except Exception as exc:
        logger.warning("skills_list failed: %s", exc, exc_info=True)
        return _json_tool_response({"success": False, "error": str(exc)})


async def skill_view(
    name: str,
    file_path: str | None = None,
) -> ToolResponse:
    """Load a skill's main instructions (SKILL.md) or a supporting file under that skill.

    Supporting files live under references/, templates/, scripts/, or assets/.

    Args:
        name (`str`):
            Skill name; use skills_list to discover names.
        file_path (`str`, optional):
            Relative path within the skill, e.g. references/api.md or templates/x.yaml.
            Omit to read SKILL.md.
    """
    try:
        err = _validate_skill_name(name or "")
        if err:
            return _json_tool_response({"success": False, "error": err})
        try:
            skill_key = normalize_skill_dir_name(name.strip())
        except ValueError as exc:
            return _json_tool_response({"success": False, "error": str(exc)})
        ws = _workspace_dir()
        root = resolve_skills_dir(ws) / skill_key
        skill_md = root / "SKILL.md"
        if not skill_md.is_file():
            return _json_tool_response(
                {
                    "success": False,
                    "error": f"Skill '{skill_key}' not found in this workspace.",
                },
            )
        if not file_path:
            text = read_text_file_with_encoding_fallback(skill_md)
            return _json_tool_response(
                {
                    "success": True,
                    "name": skill_key,
                    "skill_md": str(skill_md),
                    "content": text,
                },
            )
        target, perr = _resolve_under_skill(root, file_path)
        if perr or target is None:
            return _json_tool_response({"success": False, "error": perr or "path error"})
        if not target.is_file():
            return _json_tool_response(
                {"success": False, "error": f"File not found: {file_path}"},
            )
        raw = target.read_bytes()
        if len(raw) > _MAX_SUPPORTING_FILE_BYTES:
            return _json_tool_response(
                {
                    "success": False,
                    "error": f"File too large ({len(raw)} bytes; max {_MAX_SUPPORTING_FILE_BYTES}).",
                },
            )
        text = read_text_file_with_encoding_fallback(target)
        return _json_tool_response(
            {
                "success": True,
                "name": skill_key,
                "file_path": file_path,
                "content": text,
            },
        )
    except Exception as exc:
        logger.warning("skill_view failed: %s", exc, exc_info=True)
        return _json_tool_response({"success": False, "error": str(exc)})


def _skill_manage_create(
    ws: Path,
    skill_key: str,
    content: str,
    category: str | None,
) -> dict[str, Any]:
    cerr = _validate_category(category)
    if cerr:
        return {"success": False, "error": cerr}
    ferr = _validate_frontmatter(content)
    if ferr:
        return {"success": False, "error": ferr}
    serr = _validate_content_size(content)
    if serr:
        return {"success": False, "error": serr}

    existing = _workspace_skill_dir(ws, skill_key)
    if existing:
        return {
            "success": False,
            "error": (
                f"A skill named '{skill_key}' already exists at "
                f"{_skill_dir_display(ws, existing)}."
            ),
        }

    created = create_workspace_skill(
        ws,
        skill_key,
        content,
        enable=True,
    )
    if not created:
        return {
            "success": False,
            "error": f"Skill '{skill_key}' already exists or could not be created.",
        }

    skill_root = resolve_skills_dir(ws)
    skill_dir = skill_root / skill_key
    out: dict[str, Any] = {
        "success": True,
        "message": f"Skill '{created}' created.",
        "name": created,
        "path": str(skill_dir.relative_to(ws)),
        "skill_md": str(skill_dir / "SKILL.md"),
    }
    if category and str(category).strip():
        out["category"] = str(category).strip()
        out["layout_note"] = (
            "Workspace skills use a flat directory layout under skills/ "
            f"(this skill lives at skills/{skill_key}/; category is metadata only)."
        )
    out["hint"] = (
        "To add reference files or templates, use skill_manage(action='write_file', "
        f"name='{skill_key}', file_path='references/example.md', "
        "file_content='...')"
    )
    return out


def _skill_manage_edit(
    ws: Path,
    skill_key: str,
    content: str,
) -> dict[str, Any]:
    ferr = _validate_frontmatter(content)
    if ferr:
        return {"success": False, "error": ferr}
    serr = _validate_content_size(content)
    if serr:
        return {"success": False, "error": serr}

    root = _workspace_skill_dir(ws, skill_key)
    if not root:
        return {
            "success": False,
            "error": (
                f"Skill '{skill_key}' not found in this workspace. "
                "Use skills_list()."
            ),
        }

    result = save_skill_skill_md(ws, skill_key, content)
    if not result.get("success"):
        return {"success": False, "error": result.get("reason", "save_failed")}
    return {
        "success": True,
        "message": f"Skill '{skill_key}' updated.",
        "mode": result.get("mode"),
        "path": _skill_dir_display(ws, root),
    }


def _skill_manage_patch(
    ws: Path,
    skill_key: str,
    old_string: str,
    new_string: str,
    file_path: str | None,
    replace_all: bool,
) -> dict[str, Any]:
    root = _workspace_skill_dir(ws, skill_key)
    if not root:
        return {"success": False, "error": f"Skill '{skill_key}' not found."}

    if file_path:
        perr = _validate_support_path(file_path)
        if perr:
            return {"success": False, "error": perr}
        target, res_err = _resolve_under_skill(root, file_path)
        if res_err or target is None:
            return {"success": False, "error": res_err or "path"}
    else:
        target = root / "SKILL.md"

    if not target.is_file():
        rel = "SKILL.md" if not file_path else file_path
        return {"success": False, "error": f"File not found: {rel}"}

    text = read_text_file_with_encoding_fallback(target)
    new_content, match_count, strategy, match_error = fuzzy_find_and_replace(
        text,
        old_string,
        new_string,
        replace_all,
    )
    if match_error:
        preview = text[:500] + ("..." if len(text) > 500 else "")
        out: dict[str, Any] = {
            "success": False,
            "error": match_error,
            "file_preview": preview,
        }
        return out

    label = "SKILL.md" if not file_path else file_path
    err = _validate_content_size(new_content, label=label)
    if err:
        return {"success": False, "error": err}

    if file_path:
        nbytes = len(new_content.encode("utf-8"))
        if nbytes > _MAX_SUPPORTING_FILE_BYTES:
            return {
                "success": False,
                "error": (
                    f"Patched file would be {nbytes:,} bytes "
                    f"(limit: {_MAX_SUPPORTING_FILE_BYTES:,})."
                ),
            }

    if not file_path:
        ferr = _validate_frontmatter(new_content)
        if ferr:
            return {
                "success": False,
                "error": f"Patch would break SKILL.md structure: {ferr}",
            }
        result = save_skill_skill_md(ws, skill_key, new_content)
        if not result.get("success"):
            return {"success": False, "error": result.get("reason", "patch_failed")}
        strat = f", strategy: {strategy}" if strategy else ""
        return {
            "success": True,
            "message": (
                f"Patched SKILL.md in '{skill_key}' ("
                f"{match_count} replacement{'s' if match_count > 1 else ''}{strat})."
            ),
            "strategy": strategy,
        }

    original = text
    try:
        _atomic_write_text(target, new_content)
        scan_skill_dir_or_raise(root, skill_key)
    except SkillScanBlocked:
        _atomic_write_text(target, original)
        raise

    return {
        "success": True,
        "message": (
            f"Patched {file_path} in '{skill_key}' ("
            f"{match_count} replacement{'s' if match_count > 1 else ''})."
        ),
        "strategy": strategy,
    }


def _skill_manage_write_file(
    ws: Path,
    skill_key: str,
    file_path: str,
    file_content: str,
) -> dict[str, Any]:
    perr = _validate_support_path(file_path)
    if perr:
        return {"success": False, "error": perr}

    content_bytes = len(file_content.encode("utf-8"))
    if content_bytes > _MAX_SUPPORTING_FILE_BYTES:
        return {
            "success": False,
            "error": (
                f"File content is {content_bytes:,} bytes "
                f"(limit: {_MAX_SUPPORTING_FILE_BYTES:,} bytes / 1 MiB)."
            ),
        }
    cerr = _validate_content_size(file_content, label=file_path)
    if cerr:
        return {"success": False, "error": cerr}

    root = _workspace_skill_dir(ws, skill_key)
    if not root:
        return {
            "success": False,
            "error": (
                f"Skill '{skill_key}' not found. "
                "Create it first with action='create'."
            ),
        }

    target, res_err = _resolve_under_skill(root, file_path)
    if res_err or target is None:
        return {"success": False, "error": res_err or "path"}

    original = (
        read_text_file_with_encoding_fallback(target)
        if target.exists()
        else None
    )
    try:
        _atomic_write_text(target, file_content)
        scan_skill_dir_or_raise(root, skill_key)
    except SkillScanBlocked:
        if original is not None:
            _atomic_write_text(target, original)
        else:
            target.unlink(missing_ok=True)
        raise

    return {
        "success": True,
        "message": f"File '{file_path}' written to skill '{skill_key}'.",
        "path": str(target),
    }


def _skill_manage_remove_file(
    ws: Path,
    skill_key: str,
    file_path: str,
) -> dict[str, Any]:
    perr = _validate_support_path(file_path)
    if perr:
        return {"success": False, "error": perr}

    root = _workspace_skill_dir(ws, skill_key)
    if not root:
        return {"success": False, "error": f"Skill '{skill_key}' not found."}

    target, res_err = _resolve_under_skill(root, file_path)
    if res_err or target is None:
        return {"success": False, "error": res_err or "path"}

    if not target.is_file():
        available = []
        for subdir in sorted(_ALLOWED_SUBDIRS):
            sub = root / subdir
            if sub.exists():
                for found in sub.rglob("*"):
                    if found.is_file():
                        available.append(str(found.relative_to(root)))
        return {
            "success": False,
            "error": f"File '{file_path}' not found in skill '{skill_key}'.",
            "available_files": available if available else None,
        }

    target.unlink()
    parent = target.parent
    if parent != root and parent.exists() and not any(parent.iterdir()):
        parent.rmdir()

    scan_skill_dir_or_raise(root, skill_key)

    return {
        "success": True,
        "message": f"File '{file_path}' removed from skill '{skill_key}'.",
    }


def _mark_mutation() -> None:
    skill_workspace_mutation_occurred.set(True)


async def skill_manage(
    action: Literal[
        "create",
        "patch",
        "edit",
        "delete",
        "write_file",
        "remove_file",
    ],
    name: str,
    content: str | None = None,
    category: str | None = None,
    file_path: str | None = None,
    file_content: str | None = None,
    old_string: str | None = None,
    new_string: str | None = None,
    replace_all: bool = False,
) -> ToolResponse:
    """Manage workspace skills (procedural memory): create, patch, edit, delete, or files.

    Skills are stored under this workspace's skills/ directory and skill.json manifest.
    Actions: create (full SKILL.md + optional category); patch (old_string/new_string,
    preferred for small fixes); edit (full SKILL.md replacement for major changes);
    delete; write_file; remove_file under references/, templates/, scripts/, or assets/.

    Prefer creating or patching after non-trivial successes; confirm with the user before
    create/delete. For edit, read the skill with skill_view first and send the full updated
    SKILL.md as content.

    Args:
        action (`str`):
            One of: create, patch, edit, delete, write_file, remove_file.
        name (`str`):
            Skill directory name (lowercase, hyphens/underscores, max 64 chars). Must exist
            for patch, edit, delete, write_file, and remove_file.
        content (`str`, optional):
            Full SKILL.md text (YAML frontmatter + body). Required for create and edit.
        category (`str`, optional):
            Optional single-segment label (e.g. devops, mlops), validated like Hermes.
            Workspace storage remains flat: ``<workspace>/skills/<name>/`` (category is
            metadata only, not a filesystem subdirectory).
        file_path (`str`, optional):
            Supporting file path under the skill. Required for write_file and remove_file
            (must start with references/, templates/, scripts/, or assets/). For patch,
            optional; defaults to SKILL.md when omitted.
        file_content (`str`, optional):
            File body for write_file.
        old_string (`str`, optional):
            Text to find for patch (multi-strategy fuzzy match like Hermes); include enough
            context to be unique unless replace_all is true.
        new_string (`str`, optional):
            Replacement text for patch (may be empty to delete the matched span).
        replace_all (`bool`, defaults to `False`):
            If true, patch replaces every occurrence of old_string instead of requiring a
            unique match.
    """
    act = (action or "").strip().lower()
    err = _validate_skill_name((name or "").strip())
    if err:
        return _json_tool_response({"success": False, "error": err})
    try:
        skill_key = normalize_skill_dir_name(name.strip() if name else "")
    except ValueError as exc:
        return _json_tool_response({"success": False, "error": str(exc)})

    ws = _workspace_dir()

    try:
        if act == "create":
            if not content or not str(content).strip():
                return _json_tool_response(
                    {
                        "success": False,
                        "error": (
                            "content is required for create. "
                            "Provide the full SKILL.md text (frontmatter + body)."
                        ),
                    },
                )
            result = _skill_manage_create(ws, skill_key, content, category)
            if not result.get("success"):
                return _json_tool_response(result)
            _mark_mutation()
            return _json_tool_response(result)

        if act == "edit":
            if not content or not str(content).strip():
                return _json_tool_response(
                    {
                        "success": False,
                        "error": (
                            "content is required for edit. "
                            "Provide the full updated SKILL.md text."
                        ),
                    },
                )
            if _is_builtin_skill(ws, skill_key):
                return _json_tool_response(
                    {
                        "success": False,
                        "error": "Cannot replace SKILL.md for builtin skills.",
                    },
                )
            result = _skill_manage_edit(ws, skill_key, content)
            if not result.get("success"):
                return _json_tool_response(result)
            _mark_mutation()
            return _json_tool_response(result)

        if act == "patch":
            if old_string is None or str(old_string) == "":
                return _json_tool_response(
                    {
                        "success": False,
                        "error": "old_string is required for patch.",
                    },
                )
            if new_string is None:
                return _json_tool_response(
                    {
                        "success": False,
                        "error": (
                            "new_string is required for patch "
                            "(use empty string to delete matched text)."
                        ),
                    },
                )
            if _is_builtin_skill(ws, skill_key):
                return _json_tool_response(
                    {
                        "success": False,
                        "error": "Cannot patch builtin skills.",
                    },
                )
            result = _skill_manage_patch(
                ws,
                skill_key,
                old_string,
                new_string,
                file_path,
                replace_all,
            )
            if not result.get("success"):
                return _json_tool_response(result)
            _mark_mutation()
            return _json_tool_response(result)

        if act == "delete":
            if _is_builtin_skill(ws, skill_key):
                return _json_tool_response(
                    {
                        "success": False,
                        "error": "Cannot delete builtin skills.",
                    },
                )
            disable_workspace_skill(ws, skill_key)
            ok = delete_workspace_skill(ws, skill_key)
            if not ok:
                return _json_tool_response(
                    {
                        "success": False,
                        "error": (
                            f"Could not delete '{skill_key}' "
                            "(missing or still enabled)."
                        ),
                    },
                )
            _mark_mutation()
            return _json_tool_response(
                {
                    "success": True,
                    "message": f"Skill '{skill_key}' deleted.",
                },
            )

        if act == "write_file":
            if not file_path:
                return _json_tool_response(
                    {
                        "success": False,
                        "error": (
                            "file_path is required for write_file. "
                            "Example: 'references/api-guide.md'"
                        ),
                    },
                )
            if file_content is None:
                return _json_tool_response(
                    {
                        "success": False,
                        "error": "file_content is required for write_file.",
                    },
                )
            if _is_builtin_skill(ws, skill_key):
                return _json_tool_response(
                    {
                        "success": False,
                        "error": "Cannot add files to builtin skills.",
                    },
                )
            result = _skill_manage_write_file(
                ws,
                skill_key,
                file_path,
                file_content,
            )
            if not result.get("success"):
                return _json_tool_response(result)
            _mark_mutation()
            return _json_tool_response(result)

        if act == "remove_file":
            if not file_path:
                return _json_tool_response(
                    {
                        "success": False,
                        "error": "file_path is required for remove_file.",
                    },
                )
            if _is_builtin_skill(ws, skill_key):
                return _json_tool_response(
                    {
                        "success": False,
                        "error": "Cannot remove files from builtin skills.",
                    },
                )
            result = _skill_manage_remove_file(ws, skill_key, file_path)
            if not result.get("success"):
                return _json_tool_response(result)
            _mark_mutation()
            return _json_tool_response(result)

        return _json_tool_response(
            {
                "success": False,
                "error": (
                    f"Unknown action '{action}'. "
                    "Use: create, edit, patch, delete, write_file, remove_file."
                ),
            },
        )
    except SkillScanBlocked as exc:
        return _json_tool_response({"success": False, "error": str(exc)})
    except Exception as exc:
        logger.warning("skill_manage failed: %s", exc, exc_info=True)
        return _json_tool_response({"success": False, "error": str(exc)})
