from __future__ import annotations

import atexit
import json
import os
import re
import shutil
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


_RUNTIME_TEMP_ROOT: Path | None = None
_RUNTIME_TEMP_SESSION: Path | None = None
_RUNTIME_CLEANUP_REGISTERED = False
_RUNTIME_WORKSPACE_ROOT: Path | None = None

LEGACY_TEMP_FILE_PATTERN = re.compile(r"^[A-Za-z0-9_]{8}$")
LEGACY_TEMP_DIR_NAMES = {
    "__pycache__",
    ".codex_tmp",
}
LEGACY_TEMP_DIR_PREFIXES = (
    "verify_case",
    "pytest-cache-files-",
)


def ensure_directory(path: str | Path) -> Path:
    directory = Path(path)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def resolve_app_path(*relative_parts: str, anchor_file: str | Path | None = None, anchor_levels_up: int = 0) -> Path:
    if getattr(sys, "frozen", False):
        bundle_root = getattr(sys, "_MEIPASS", None)
        if bundle_root:
            return Path(bundle_root).joinpath(*relative_parts)
        return Path(sys.executable).resolve().parent.joinpath(*relative_parts)

    if anchor_file is None:
        base_path = Path.cwd()
    else:
        base_path = Path(anchor_file).resolve()
        for _ in range(anchor_levels_up):
            base_path = base_path.parent
    return base_path.joinpath(*relative_parts)


def _remove_path(path: Path) -> None:
    try:
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)
    except Exception:
        pass


def cleanup_runtime_temp_root(path: str | Path) -> Path:
    root = ensure_directory(path)
    for child in root.iterdir():
        _remove_path(child)
    return root


def _is_legacy_root_temp_file(path: Path) -> bool:
    return path.is_file() and LEGACY_TEMP_FILE_PATTERN.fullmatch(path.name) is not None and path.stat().st_size <= 4096


def _is_legacy_root_temp_dir(path: Path) -> bool:
    return path.is_dir() and (
        path.name in LEGACY_TEMP_DIR_NAMES or any(path.name.startswith(prefix) for prefix in LEGACY_TEMP_DIR_PREFIXES)
    )


def cleanup_workspace_temp_artifacts(path: str | Path | None = None) -> dict[str, int]:
    workspace_root = Path(path or Path.cwd())
    if not workspace_root.exists():
        return {"files": 0, "dirs": 0}

    removed_files = 0
    removed_dirs = 0

    for child in workspace_root.iterdir():
        try:
            if _is_legacy_root_temp_file(child):
                _remove_path(child)
                if not child.exists():
                    removed_files += 1
            elif _is_legacy_root_temp_dir(child):
                _remove_path(child)
                if not child.exists():
                    removed_dirs += 1
        except Exception:
            continue

    return {"files": removed_files, "dirs": removed_dirs}


def _activate_temp_directory(path: str | Path) -> Path:
    candidate = ensure_directory(path)
    with tempfile.NamedTemporaryFile(prefix="road_report_", dir=candidate, delete=True):
        pass
    tempfile.tempdir = str(candidate)
    os.environ["TMP"] = str(candidate)
    os.environ["TEMP"] = str(candidate)
    os.environ["TMPDIR"] = str(candidate)
    return candidate


def _cleanup_runtime_temp_session() -> None:
    global _RUNTIME_TEMP_ROOT
    global _RUNTIME_TEMP_SESSION
    global _RUNTIME_WORKSPACE_ROOT

    if _RUNTIME_TEMP_SESSION is not None:
        _remove_path(_RUNTIME_TEMP_SESSION)
        _RUNTIME_TEMP_SESSION = None

    if _RUNTIME_TEMP_ROOT is not None and _RUNTIME_TEMP_ROOT.exists():
        for child in list(_RUNTIME_TEMP_ROOT.iterdir()):
            _remove_path(child)
        try:
            if not any(_RUNTIME_TEMP_ROOT.iterdir()):
                _RUNTIME_TEMP_ROOT.rmdir()
        except Exception:
            pass
        _RUNTIME_TEMP_ROOT = None

    if _RUNTIME_WORKSPACE_ROOT is not None:
        cleanup_workspace_temp_artifacts(_RUNTIME_WORKSPACE_ROOT)
        _RUNTIME_WORKSPACE_ROOT = None


def prepare_runtime_temp_directory(path: str | Path | None = None, workspace_root: str | Path | None = None) -> Path:
    global _RUNTIME_TEMP_ROOT
    global _RUNTIME_TEMP_SESSION
    global _RUNTIME_CLEANUP_REGISTERED
    global _RUNTIME_WORKSPACE_ROOT

    if _RUNTIME_TEMP_SESSION is not None and _RUNTIME_TEMP_SESSION.exists():
        return _RUNTIME_TEMP_SESSION

    _RUNTIME_WORKSPACE_ROOT = Path(workspace_root or Path.cwd())
    cleanup_workspace_temp_artifacts(_RUNTIME_WORKSPACE_ROOT)

    temp_root = Path(path or (Path.cwd() / ".runtime_temp"))
    cleanup_runtime_temp_root(temp_root)

    session_dir = temp_root / f"session_{now_timestamp()}_{os.getpid()}"
    _activate_temp_directory(session_dir)
    _RUNTIME_TEMP_ROOT = temp_root
    _RUNTIME_TEMP_SESSION = session_dir

    if not _RUNTIME_CLEANUP_REGISTERED:
        atexit.register(_cleanup_runtime_temp_session)
        _RUNTIME_CLEANUP_REGISTERED = True

    return session_dir


@contextmanager
def override_temp_directory(path: str | Path):
    previous_tempdir = tempfile.tempdir
    previous_tmp = os.environ.get("TMP")
    previous_temp = os.environ.get("TEMP")
    previous_tmpdir = os.environ.get("TMPDIR")
    temp_dir = _activate_temp_directory(path)

    try:
        yield temp_dir
    finally:
        tempfile.tempdir = previous_tempdir
        if previous_tmp is None:
            os.environ.pop("TMP", None)
        else:
            os.environ["TMP"] = previous_tmp
        if previous_temp is None:
            os.environ.pop("TEMP", None)
        else:
            os.environ["TEMP"] = previous_temp
        if previous_tmpdir is None:
            os.environ.pop("TMPDIR", None)
        else:
            os.environ["TMPDIR"] = previous_tmpdir


def load_json(path: str | Path) -> dict[str, Any]:
    with Path(path).open("r", encoding="utf-8-sig") as file:
        return json.load(file)


def save_json(path: str | Path, data: dict[str, Any]) -> Path:
    target = Path(path)
    ensure_directory(target.parent)
    with target.open("w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)
    return target


def now_timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def normalize_field_name(value: str | None) -> str:
    if value is None:
        return ""
    return "".join(str(value).strip().lower().split()).replace("_", "")


def guess_field_name(columns: Iterable[str], aliases: Iterable[str]) -> str | None:
    alias_map = {normalize_field_name(column): column for column in columns}
    for alias in aliases:
        matched = alias_map.get(normalize_field_name(alias))
        if matched:
            return matched
    return None


def coerce_to_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = value.strip().replace(",", "")
        if not value:
            return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def meters_from_diameter(value: Any, default: float = 0.0) -> float:
    numeric = coerce_to_float(value)
    if numeric is None:
        return default
    if numeric > 10:
        return numeric / 1000.0
    return numeric


def round_or_none(value: Any, digits: int) -> float | None:
    numeric = coerce_to_float(value)
    if numeric is None:
        return None
    return round(numeric, digits)


def unique_join(values: Iterable[Any], separator: str = ", ") -> str | None:
    items: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if not text or text.lower() == "nan" or text in seen:
            continue
        seen.add(text)
        items.append(text)
    return separator.join(items) if items else None


def first_non_empty(values: Iterable[Any]) -> Any:
    for value in values:
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return value
    return None
