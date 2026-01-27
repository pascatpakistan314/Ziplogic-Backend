# agent/utils/paths.py
from __future__ import annotations
import os
from pathlib import Path
from typing import Optional, Union, Any

_WORKSPACE_PATH: Optional[Path] = None

def set_workspace_path(p: Union[str, Path]) -> Path:
    """Set global workspace path (absolute Path)."""
    global _WORKSPACE_PATH
    _WORKSPACE_PATH = Path(p).expanduser().resolve()
    return _WORKSPACE_PATH

def get_workspace_path() -> Path:
    """Get global workspace path or resolve from env/CWD."""
    global _WORKSPACE_PATH
    if _WORKSPACE_PATH is not None:
        return _WORKSPACE_PATH
    env = os.getenv("WORKSPACE_PATH")
    if env:
        return set_workspace_path(env)
    # default to ./workspace_repo if it exists, else CWD
    default = Path("./workspace_repo")
    return set_workspace_path(default if default.exists() else Path.cwd())

def ensure_workspace_path(state: Optional[Any] = None) -> Path:
    """
    Idempotently ensure a workspace path exists and persist it back to state.
    Priority: state.workspace_path -> state.workspace_dir -> global -> env -> CWD
    """
    cand: Optional[Union[str, Path]] = None
    if state is not None:
        cand = getattr(state, "workspace_path", None) or getattr(state, "workspace_dir", None)
    p = set_workspace_path(cand or get_workspace_path())
    # persist on state under both names for compatibility
    if state is not None:
        try:
            setattr(state, "workspace_path", str(p))
            if hasattr(state, "workspace_dir") and not getattr(state, "workspace_dir", None):
                setattr(state, "workspace_dir", str(p))
        except Exception:
            pass
    return p

def normalize_under_workspace(path_like: Union[str, Path], workspace: Optional[Union[str, Path]] = None) -> Path:
    """
    Normalize a file/dir path relative to workspace (absolute).
    - If path is absolute: return as-is.
    - If path starts with './workspace_repo/': replace with actual workspace.
    - Else: treat as relative to workspace.
    """
    w = Path(workspace).expanduser().resolve() if workspace else get_workspace_path()
    p = Path(path_like)
    if p.is_absolute():
        return p
    # replace legacy './workspace_repo/' prefix
    legacy = Path("./workspace_repo")
    try:
        s = str(p).replace("\\", "/")
        if s.startswith(str(legacy).replace("\\", "/") + "/"):
            tail = s[len(str(legacy)) + 1:]
            return (w / tail).resolve()
    except Exception:
        pass
    return (w / p).resolve()

def _coalesce_first(*vals: Optional[str]) -> Optional[str]:
    for v in vals:
        if v:
            return v
    return None

def get_project_directory(state: Optional[Any] = None) -> Path:
    """
    Resolve the ACTIVE project directory (not just the workspace root).

    Priority order (first hit wins):
      1. state.project_dir / state.project_path / state.project_root / state.workspace_path
      2. env PROJECT_DIR (absolute or relative to workspace)
      3. latest 'project_*' directory inside workspace
      4. workspace itself (fallback)
    """
    workspace = ensure_workspace_path(state)

    # 1) From state (check multiple field names for compatibility)
    state_choice = _coalesce_first(
        getattr(state, "project_dir", None) if state is not None else None,
        getattr(state, "project_path", None) if state is not None else None,
        getattr(state, "project_root", None) if state is not None else None,
        getattr(state, "workspace_path", None) if state is not None else None,
    )
    if state_choice:
        p = Path(state_choice)
        if not p.is_absolute():
            p = (workspace / p).resolve()
        if p.exists() and p.is_dir():
            return p

    # 2) From env
    env_choice = os.getenv("PROJECT_DIR")
    if env_choice:
        p = Path(env_choice)
        if not p.is_absolute():
            p = (workspace / p).resolve()
        if p.exists() and p.is_dir():
            return p

    # 3) Latest project_* directory inside workspace
    try:
        project_dirs = [
            d for d in workspace.iterdir()
            if d.is_dir() and d.name.startswith("project_")
        ]
        if project_dirs:
            return max(project_dirs, key=lambda d: d.stat().st_mtime)
    except Exception:
        pass

    # 4) Fallback to workspace
    return workspace

def force_into_directory(path_like: Union[str, Path], base_dir: Union[str, Path]) -> Path:
    """
    Force any file/dir path to live inside base_dir:
      - Absolute paths outside base_dir are rewritten as base_dir/<basename>.
      - Relative paths are resolved under base_dir.
    """
    base = Path(base_dir).expanduser().resolve()
    raw = Path(path_like or "")
    if not str(raw):
        return base
    if raw.is_absolute():
        abs_path = raw.resolve()
        try:
            if os.path.commonpath([abs_path, base]) == str(base):
                return abs_path
        except Exception:
            pass
        return (base / abs_path.name).resolve()
    return (base / raw).resolve()

def validate_is_file_path(p: Union[str, Path], project_dir: Optional[Union[str, Path]] = None) -> None:
    """
    Raise if p points to a directory or equals the workspace root or the project root.
    If project_dir is not provided, only workspace root is checked (back-compat).
    """
    w = get_workspace_path()
    pp = Path(p).resolve()
    if pp == w or str(pp).rstrip("/\\") == str(w).rstrip("/\\") or pp.is_dir():
        raise ValueError(f"Invalid file_path: '{pp}' is a directory or the workspace root; a file path is required.")
    if project_dir:
        pr = Path(project_dir).expanduser().resolve()
        if pp == pr or str(pp).rstrip("/\\") == str(pr).rstrip("/\\"):
            raise ValueError(f"Invalid file_path: '{pp}' is the project root; a file path is required.")
