# -*- coding: utf-8 -*-
"""Self-contained workspace skill.json + ``skills/`` I/O for hermes_auto_skill.

Mirrors the behavior of ``SkillService`` subset used by ``skill_manage`` without
importing :mod:`qwenpaw.agents.skills_manager`.

Remaining upstream dependencies (kept minimal):

- ``get_current_workspace_dir`` / ``WORKING_DIR`` — resolve workspace root.

Skill security scanning uses vendored Hermes :mod:`skills_guard` (regex static
analysis), not ``qwenpaw.security.skill_scanner``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypeVar

import frontmatter

try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None

try:
    import msvcrt
except ImportError:  # pragma: no cover
    msvcrt = None

if fcntl is None and msvcrt is None:  # pragma: no cover
    raise ImportError(
        "No file locking module available (need fcntl or msvcrt)",
    )

logger = logging.getLogger(__name__)

_RegistryResult = TypeVar("_RegistryResult")

_REQUIREMENTS_METADATA_NAMESPACES = ("openclaw", "qwenpaw", "clawdbot")

_IGNORED_SKILL_ARTIFACTS = frozenset(
    {
        "__pycache__",
        "__MACOSX",
        ".DS_Store",
        "Thumbs.db",
        "desktop.ini",
    },
)


def workspace_dir_for_tools() -> Path:
    """Resolve workspace directory (single bridge to qwenpaw.config)."""
    from qwenpaw.config.context import get_current_workspace_dir
    from qwenpaw.constant import WORKING_DIR

    root = Path(get_current_workspace_dir() or WORKING_DIR).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    return root


def resolve_skills_dir(workspace_dir: Path) -> Path:
    """Same layout rule as ``get_workspace_skills_dir`` in skills_manager."""
    workspace_dir = Path(workspace_dir).expanduser()
    preferred = workspace_dir / "skills"
    legacy = workspace_dir / "skill"
    if preferred.exists():
        return preferred
    if legacy.exists():
        try:
            legacy.rename(preferred)
        except OSError:
            return legacy
    return preferred


def manifest_path(workspace_dir: Path) -> Path:
    return Path(workspace_dir).expanduser() / "skill.json"


def default_workspace_manifest() -> dict[str, Any]:
    return {
        "schema_version": "workspace-skill-manifest.v1",
        "version": 0,
        "skills": {},
    }


def _lock_path_for(json_path: Path) -> Path:
    return json_path.with_name(f".{json_path.name}.lock")


@contextmanager
def _file_write_lock(lock_path: Path) -> Iterator[None]:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        if fcntl is not None:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        elif msvcrt is not None:  # pragma: no cover
            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            elif msvcrt is not None:  # pragma: no cover
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)


def read_manifest_unlocked(path: Path) -> dict[str, Any]:
    default = default_workspace_manifest()
    if not path.exists():
        return json.loads(json.dumps(default))
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        logger.warning("Malformed JSON in %s, resetting to default", path)
        return json.loads(json.dumps(default))


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    temp_path: Path | None = None
    payload = dict(payload)
    payload["version"] = max(
        int(payload.get("version", 0)) + 1,
        int(datetime.now(timezone.utc).timestamp() * 1000),
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            dir=path.parent,
            prefix=f".{path.stem}_",
            suffix=path.suffix,
            delete=False,
            encoding="utf-8",
        ) as handle:
            handle.write(json.dumps(payload, indent=2, ensure_ascii=False))
            temp_path = Path(handle.name)
        temp_path.replace(path)
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink(missing_ok=True)


def mutate_manifest(
    workspace_dir: Path,
    mutator: Callable[[dict[str, Any]], _RegistryResult],
) -> _RegistryResult:
    path = manifest_path(workspace_dir)
    default = default_workspace_manifest()
    with _file_write_lock(_lock_path_for(path)):
        payload = read_manifest_unlocked(path)
        result = mutator(payload)
        if result is not False:
            write_json_atomic(path, payload)
        return result


def normalize_skill_dir_name(name: str) -> str:
    normalized = str(name or "").strip()
    if not normalized:
        raise ValueError("Skill name cannot be empty")
    if "\x00" in normalized:
        raise ValueError("Skill name cannot contain NUL bytes")
    if normalized in {".", ".."}:
        raise ValueError(f"Invalid skill name: {normalized}")
    if "/" in normalized or "\\" in normalized:
        raise ValueError("Skill name cannot contain path separators")
    return normalized


def read_text_file_with_encoding_fallback(file_path: Path) -> str:
    """Local copy; avoids importing agents.utils.file_handling."""
    file_path = Path(file_path)
    if not file_path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")
    encodings_to_try = (
        "utf-8-sig",
        "utf-8",
        "gbk",
        "cp936",
        "cp1252",
        "latin-1",
    )
    for encoding in encodings_to_try:
        try:
            return file_path.read_text(encoding=encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return file_path.read_text(encoding="utf-8", errors="replace")


def _get_skill_mtime(skill_dir: Path) -> str:
    try:
        dir_mtime = skill_dir.stat().st_mtime
        skill_md = skill_dir / "SKILL.md"
        md_mtime = skill_md.stat().st_mtime if skill_md.exists() else 0.0
        mtime = max(dir_mtime, md_mtime)
        return (
            datetime.fromtimestamp(mtime, tz=timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )
    except OSError:
        return ""


def _extract_version(post: Any) -> str:
    metadata = post.get("metadata") or {}
    if not isinstance(metadata, dict):
        metadata = {}
    for value in (
        post.get("version"),
        metadata.get("version"),
        metadata.get("builtin_skill_version"),
    ):
        if value not in (None, ""):
            return str(value)
    return ""


def _read_frontmatter_safe(skill_dir: Path, skill_name: str = "") -> dict[str, Any]:
    if not skill_name:
        skill_name = skill_dir.name
    try:
        content = read_text_file_with_encoding_fallback(skill_dir / "SKILL.md")
        return frontmatter.loads(content)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Failed to read SKILL.md frontmatter for '%s' at %s: %s",
            skill_name,
            skill_dir,
            exc,
        )
        return {"name": skill_name, "description": ""}


def _extract_requirements_as_dict(post: dict[str, Any]) -> dict[str, Any]:
    metadata = post.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    requires: Any | None = None
    for namespace in _REQUIREMENTS_METADATA_NAMESPACES:
        provider_metadata = metadata.get(namespace)
        if isinstance(provider_metadata, dict):
            requires = provider_metadata.get("requires")
            if requires is not None:
                break

    if requires is None:
        requires = metadata.get(
            "requires",
            post.get("requires", {}),
        )

    if isinstance(requires, list):
        return {"require_bins": list(requires), "require_envs": []}

    if not isinstance(requires, dict):
        return {"require_bins": [], "require_envs": []}

    return {
        "require_bins": list(requires.get("bins", [])),
        "require_envs": list(requires.get("env", [])),
    }


def build_skill_metadata(
    skill_name: str,
    skill_dir: Path,
    *,
    source: str,
    protected: bool = False,
) -> dict[str, Any]:
    post = _read_frontmatter_safe(skill_dir, skill_name)
    requirements = _extract_requirements_as_dict(post)
    return {
        "name": skill_name,
        "description": str(post.get("description", "") or ""),
        "version_text": _extract_version(post),
        "commit_text": "",
        "source": source,
        "protected": protected,
        "requirements": requirements,
        "updated_at": _get_skill_mtime(skill_dir),
    }


def _agents_builtin_skills_dir() -> Path:
    import qwenpaw.agents as agents_pkg

    return Path(agents_pkg.__file__).resolve().parent / "skills"


def packaged_builtin_versions() -> dict[str, str]:
    versions: dict[str, str] = {}
    builtin_dir = _agents_builtin_skills_dir()
    if not builtin_dir.exists():
        return versions
    for skill_dir in sorted(builtin_dir.iterdir()):
        if skill_dir.is_dir() and (skill_dir / "SKILL.md").exists():
            post = _read_frontmatter_safe(skill_dir, skill_dir.name)
            versions[skill_dir.name] = _extract_version(post)
    return versions


def _validate_skill_content(content: str) -> None:
    post = frontmatter.loads(content)
    skill_name = str(post.get("name") or "").strip()
    skill_description = str(post.get("description") or "").strip()
    if not skill_name or not skill_description:
        raise ValueError(
            "SKILL.md must include non-empty frontmatter name and description",
        )


def _copy_skill_dir(source: Path, target: Path) -> None:
    if target.exists():
        shutil.rmtree(target)

    def _ignore(_dir: str, names: list[str]) -> set[str]:
        return {name for name in names if name in _IGNORED_SKILL_ARTIFACTS}

    shutil.copytree(
        source,
        target,
        ignore=_ignore,
    )


def _write_skill_to_dir(skill_dir: Path, content: str) -> None:
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")


@contextmanager
def staged_skill_dir(skill_name: str) -> Iterator[Path]:
    temp_root = Path(
        tempfile.mkdtemp(prefix=f"qwenpaw_skill_stage_{skill_name}_"),
    )
    stage_dir = temp_root / skill_name
    try:
        yield stage_dir
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)


def scan_skill_dir_or_raise(skill_dir: Path, skill_name: str) -> None:
    """Hermes-style guard (regex + structural checks); aligns with agent-created policy."""
    from .skills_guard import (
        SkillScanBlocked,
        format_scan_report,
        scan_skill,
        should_allow_install,
    )

    try:
        result = scan_skill(skill_dir, source="agent-created")
        allowed, reason = should_allow_install(result)
    except Exception as exc:
        logger.warning(
            "Skills guard failed for %s (%s): %s",
            skill_dir,
            skill_name,
            exc,
            exc_info=True,
        )
        return

    if allowed is False:
        report = format_scan_report(result)
        raise SkillScanBlocked(
            f"Security scan blocked this skill ({reason}):\n{report}",
        )
    if allowed is None:
        logger.warning(
            "Skills guard reported confirmation-needed for '%s' (%s): %s",
            skill_name,
            skill_dir,
            reason,
        )


def manifest_entry_source(
    workspace_dir: Path,
    skill_name: str,
) -> str | None:
    data = read_manifest_unlocked(manifest_path(workspace_dir))
    entry = data.get("skills", {}).get(skill_name)
    if not isinstance(entry, dict):
        return None
    return str(entry.get("source", "") or "") or None


def is_workspace_builtin(workspace_dir: Path, skill_name: str) -> bool:
    return manifest_entry_source(workspace_dir, skill_name) == "builtin"


def list_skill_summaries(workspace_dir: Path) -> list[dict[str, Any]]:
    """Rows compatible with prior ``skills_list`` output."""
    data = read_manifest_unlocked(manifest_path(workspace_dir))
    skill_root = resolve_skills_dir(workspace_dir)
    rows: list[dict[str, Any]] = []
    for skill_name, entry in sorted(data.get("skills", {}).items()):
        skill_dir = skill_root / skill_name
        source = entry.get("source", "workspace") if isinstance(entry, dict) else "workspace"
        description = ""
        md_path = skill_dir / "SKILL.md"
        if md_path.is_file():
            try:
                content = read_text_file_with_encoding_fallback(md_path)
                post = frontmatter.loads(content)
                description = str(post.get("description", "") or "")
            except Exception:  # noqa: BLE001
                pass
        rows.append(
            {
                "name": skill_name,
                "description": description[:1024],
                "source": source,
            },
        )
    return rows


def ensure_skill_dir_exists(workspace_dir: Path, skill_key: str) -> Path | None:
    root = resolve_skills_dir(workspace_dir) / skill_key
    if root.is_dir() and (root / "SKILL.md").is_file():
        return root
    return None


def create_workspace_skill(
    workspace_dir: Path,
    name: str,
    content: str,
    *,
    enable: bool = True,
    config: dict[str, Any] | None = None,
) -> str | None:
    _validate_skill_content(content)
    skill_name = normalize_skill_dir_name(name)
    skill_root = resolve_skills_dir(workspace_dir)
    skill_root.mkdir(parents=True, exist_ok=True)
    skill_dir = skill_root / skill_name
    if skill_dir.exists():
        return None

    with staged_skill_dir(skill_name) as staged:
        _write_skill_to_dir(staged, content)
        scan_skill_dir_or_raise(staged, skill_name)
        _copy_skill_dir(staged, skill_dir)

    def _update(payload: dict[str, Any]) -> None:
        payload.setdefault("skills", {})
        entry = payload["skills"].get(skill_name) or {}
        if "source" in entry:
            source = entry["source"]
        elif skill_name in packaged_builtin_versions():
            source = "builtin"
        else:
            source = "customized"
        metadata = build_skill_metadata(
            skill_name,
            skill_dir,
            source=source,
            protected=False,
        )
        payload["skills"][skill_name] = {
            "enabled": bool(entry.get("enabled", enable)),
            "channels": entry.get("channels") or ["all"],
            "source": metadata["source"],
            "config": (
                dict(config)
                if config is not None
                else dict(entry.get("config") or {})
            ),
            "metadata": metadata,
            "requirements": metadata["requirements"],
            "updated_at": metadata["updated_at"],
        }

    mutate_manifest(workspace_dir, _update)
    return skill_name


def save_skill_skill_md(
    workspace_dir: Path,
    skill_name: str,
    content: str,
    *,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """In-place SKILL.md save; mirrors ``SkillService._save_skill_in_place``."""
    mp = manifest_path(workspace_dir)
    manifest = read_manifest_unlocked(mp)
    old_entry = manifest.get("skills", {}).get(skill_name)
    if old_entry is None:
        return {"success": False, "reason": "not_found"}

    new_config = (
        config if config is not None else old_entry.get("config") or {}
    )
    skill_root = resolve_skills_dir(workspace_dir)
    skill_root.mkdir(parents=True, exist_ok=True)
    skill_dir = skill_root / skill_name

    old_md = (
        (skill_dir / "SKILL.md").read_text(encoding="utf-8")
        if (skill_dir / "SKILL.md").exists()
        else ""
    )
    content_changed = content != old_md
    if not content_changed and new_config == (old_entry.get("config") or {}):
        return {"success": True, "mode": "noop", "name": skill_name}

    if content_changed:
        with staged_skill_dir(skill_name) as staged:
            if skill_dir.exists():
                _copy_skill_dir(skill_dir, staged)
            (staged / "SKILL.md").write_text(content, encoding="utf-8")
            scan_skill_dir_or_raise(staged, skill_name)
        (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")

    source = (
        "customized"
        if content_changed
        else old_entry.get("source", "customized")
    )
    metadata = build_skill_metadata(
        skill_name,
        skill_dir,
        source=source,
        protected=False,
    )

    def _edit(payload: dict[str, Any]) -> None:
        payload.setdefault("skills", {})
        current_entry = payload["skills"].get(skill_name) or old_entry or {}
        next_entry = {
            "enabled": bool(current_entry.get("enabled", False)),
            "channels": current_entry.get("channels") or ["all"],
            "source": metadata["source"],
            "config": new_config,
            "metadata": metadata,
            "requirements": metadata["requirements"],
            "updated_at": metadata["updated_at"],
        }
        existing_tags = current_entry.get("tags")
        if existing_tags is not None:
            next_entry["tags"] = existing_tags
        payload["skills"][skill_name] = next_entry

    mutate_manifest(workspace_dir, _edit)
    return {"success": True, "mode": "edit", "name": skill_name}


def disable_workspace_skill(workspace_dir: Path, skill_name: str) -> bool:

    def _update(payload: dict[str, Any]) -> bool:
        entry = payload.get("skills", {}).get(skill_name)
        if entry is None:
            return False
        entry["enabled"] = False
        return True

    updated = mutate_manifest(workspace_dir, _update)
    return bool(updated)


def delete_workspace_skill(workspace_dir: Path, skill_name: str) -> bool:
    manifest = read_manifest_unlocked(manifest_path(workspace_dir))
    entry = manifest.get("skills", {}).get(skill_name)
    if entry is None or entry.get("enabled", False):
        return False

    skill_dir = resolve_skills_dir(workspace_dir) / skill_name
    if skill_dir.exists():
        shutil.rmtree(skill_dir)

    def _update(payload: dict[str, Any]) -> None:
        payload.get("skills", {}).pop(skill_name, None)

    mutate_manifest(workspace_dir, _update)
    return True


_TIMESTAMP_SUFFIX_RE = re.compile(r"(-\d{14})+$")


def suggest_conflict_name(skill_name: str, existing_names: set[str]) -> str:
    base = _TIMESTAMP_SUFFIX_RE.sub("", skill_name) or skill_name
    import time as time_mod

    for _ in range(100):
        suffix = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        candidate = f"{base}-{suffix}"
        if candidate not in existing_names:
            return candidate
        time_mod.sleep(0.01)
    return f"{base}-{suffix}"
