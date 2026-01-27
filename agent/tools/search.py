"""
Ripgrep-based search tools with automatic project directory detection (fixed).

Key fixes:
- Prefer project directory when directory is None/"."/workspace root.
- Use cwd=<root> and search '.' for ripgrep to make globs and --files reliable on Windows.
- Return absolute paths from glob_files.
"""

import os
import re
import subprocess
import json
from pathlib import Path
from typing import Optional, Literal, List

from langchain_core.tools import tool
from agent.utils.paths import get_project_directory, get_workspace_path


# ---------- helpers ----------

def _check_ripgrep_installed() -> bool:
    """Check if ripgrep is installed and accessible."""
    try:
        result = subprocess.run(['rg', '--version'], capture_output=True, text=True, timeout=5)
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False

def _ripgrep_error_message() -> str:
    """Return helpful error message when ripgrep is not installed."""
    return """Error: ripgrep (rg) is not installed.

Please install ripgrep to enable fast code searching:

Windows:
  choco install ripgrep
  OR
  scoop install ripgrep
  OR
  Download from: https://github.com/BurntSushi/ripgrep/releases

Linux/macOS:
  See: INSTALL_RIPGREP.md

After installation, restart your terminal and try again."""


def _safe_get_project_dir() -> Optional[str]:
    try:
        return str(get_project_directory())
    except Exception:
        return None


def _safe_get_workspace_dir() -> Optional[str]:
    try:
        return str(get_workspace_path())
    except Exception:
        return None


def _is_subpath(path: str, parent: str) -> bool:
    try:
        Path(path).resolve().relative_to(Path(parent).resolve())
        return True
    except Exception:
        return False


def _resolve_search_root(directory: Optional[str] = None) -> str:
    """
    Resolve preferred search root with this priority:
      1) If directory is None / "" / "." -> project_dir or workspace_dir or CWD
      2) If directory equals the workspace root -> prefer project_dir (your desired default)
      3) If directory is relative -> try under project_dir, else under workspace_dir, else abspath
      4) If directory is absolute -> use it as-is
    """
    project_dir = _safe_get_project_dir()
    workspace_dir = _safe_get_workspace_dir()

    # 1) None / "." / "" => prefer project
    if not directory or directory == ".":
        return project_dir or workspace_dir or os.getcwd()

    # Normalize incoming directory
    if os.path.isabs(directory):
        # 2) If caller passed the workspace root, prefer project dir
        if workspace_dir and Path(directory).resolve() == Path(workspace_dir).resolve():
            return project_dir or directory
        return directory

    # 3) Relative directory: resolve against project first, then workspace
    if project_dir:
        candidate = (Path(project_dir) / directory).resolve()
        if candidate.exists():
            return str(candidate)
    if workspace_dir:
        candidate = (Path(workspace_dir) / directory).resolve()
        if candidate.exists():
            return str(candidate)

    # 4) Fallback: absolute from CWD
    return str(Path(directory).resolve())


def _run_rg(args: List[str], cwd: str, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        cwd=cwd,
        timeout=timeout,
        encoding='utf-8',
        errors='ignore'
    )


# ---------- core runners ----------

def _ripgrep_search(
    root: str,
    pattern: str,
    glob_pattern: Optional[str],
    case_sensitive: bool,
    is_regex: bool,
    output_mode: str,
    context: int,
    show_line_numbers: bool,
    multiline: bool,
    head_limit: Optional[int],
    file_type: Optional[str],
):
    if not _check_ripgrep_installed():
        return _ripgrep_error_message()

    cmd: List[str] = ['rg', '--color=never']

    if not case_sensitive:
        cmd.append('-i')
    if not is_regex:
        cmd.append('-F')
    if multiline:
        cmd.append('-U')

    if output_mode == "files_with_matches":
        cmd.append('-l')
    elif output_mode == "count":
        cmd.append('-c')
    else:
        cmd.append('--heading')
        if show_line_numbers:
            cmd.append('-n')
        if context > 0:
            cmd.extend(['-C', str(context)])

    if glob_pattern:
        cmd.extend(['-g', glob_pattern])
    if file_type:
        cmd.extend(['-t', file_type])

    if head_limit and output_mode == "content":
        cmd.extend(['-m', str(head_limit)])

    # IMPORTANT: search in '.' with cwd=root so globs and paths work cross-platform
    cmd.extend([pattern, '.'])

    try:
        result = _run_rg(cmd, cwd=root, timeout=120)

        # rg returns 1 when no matches
        if result.returncode not in (0, 1):
            return f"Error: {result.stderr.strip() or 'ripgrep error'}"
        if result.returncode == 1 or not result.stdout.strip():
            return "No matches found."

        out = result.stdout

        if head_limit and output_mode in ("files_with_matches", "count"):
            lines = out.strip().splitlines()
            out = "\n".join(lines[:head_limit])

        return out.strip()
    except subprocess.TimeoutExpired:
        return "Search timed out after 2 minutes. Try narrowing your search."
    except Exception as e:
        return f"Error running ripgrep: {e}"


def _ripgrep_json_search(
    root: str,
    pattern: str,
    glob_pattern: Optional[str],
    case_sensitive: bool,
    head_limit: Optional[int],
):
    if not _check_ripgrep_installed():
        return []

    cmd: List[str] = ['rg', '--json', '--color=never']
    if not case_sensitive:
        cmd.append('-i')
    if glob_pattern:
        cmd.extend(['-g', glob_pattern])
    cmd.extend([pattern, '.'])

    try:
        result = _run_rg(cmd, cwd=root, timeout=120)
        if result.returncode not in (0, 1):
            return []

        matches = []
        for line in result.stdout.strip().splitlines():
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if data.get('type') == 'match':
                d = data.get('data', {})
                rel = d.get('path', {}).get('text', '')
                abs_path = str((Path(root) / rel).resolve())
                matches.append({
                    'file': abs_path,
                    'line_number': d.get('line_number'),
                    'line': d.get('lines', {}).get('text', '').rstrip(),
                    'match': (d.get('submatches') or [{}])[0].get('match', {}).get('text', '')
                })
                if head_limit and len(matches) >= head_limit:
                    break
        return matches
    except Exception:
        return []


# ---------- public tools ----------

@tool(parse_docstring=True)
def grep_search(
    pattern: str,
    directory: Optional[str] = None,
    glob_pattern: Optional[str] = None,
    case_sensitive: bool = False,
    is_regex: bool = True,
    output_mode: Literal["content", "files_with_matches", "count"] = "files_with_matches",
    context: int = 0,
    show_line_numbers: bool = True,
    multiline: bool = False,
    head_limit: Optional[int] = None,
    file_type: Optional[str] = None
):
    """
    Advanced grep-like search using ripgrep.

    Args:
        pattern: Regex/literal pattern (REQUIRED)
        directory: Search root (defaults to project directory; workspace/CWD fallback)
        glob_pattern: e.g. '*.py', '*.{ts,tsx}'
        case_sensitive: default False
        is_regex: default True (set False for literals)
        output_mode: 'content' | 'files_with_matches' | 'count'
        context: only for content mode
        show_line_numbers: default True
        multiline: allow '.' to match newlines
        head_limit: limit lines/files printed
        file_type: ripgrep -t shorthand, e.g. 'py','js'
    """
    root = _resolve_search_root(directory)
    return _ripgrep_search(
        root=root,
        pattern=pattern,
        glob_pattern=glob_pattern,
        case_sensitive=case_sensitive,
        is_regex=is_regex,
        output_mode=output_mode,
        context=context,
        show_line_numbers=show_line_numbers,
        multiline=multiline,
        head_limit=head_limit,
        file_type=file_type,
    )


@tool(parse_docstring=True)
def glob_files(
    pattern: str,
    directory: Optional[str] = None,
    head_limit: Optional[int] = None
):
    """
    List files matching a glob pattern.

    Args:
        pattern: Glob, e.g. '**/*.py', 'src/**/*.tsx', '*.{js,ts}'
        directory: Search root (defaults to project directory; workspace/CWD fallback)
        head_limit: Limit number of results
    """
    root = _resolve_search_root(directory)

    # Fast path: ripgrep --files with cwd=root
    if _check_ripgrep_installed():
        try:
            cmd: List[str] = ['rg', '--files']
            if pattern:
                cmd.extend(['-g', pattern])

            result = _run_rg(cmd, cwd=root, timeout=60)

            # rg --files returns 0 even if no files; handle empty stdout
            if result.returncode != 0 or not result.stdout.strip():
                return "No files found matching the pattern."

            rel_files = [line.strip() for line in result.stdout.splitlines() if line.strip()]
            abs_files = [str((Path(root) / p).resolve()) for p in rel_files if os.path.isfile(os.path.join(root, p))]

            # sort by mtime (newest first)
            abs_files = sorted(abs_files, key=lambda f: os.path.getmtime(f) if os.path.exists(f) else 0, reverse=True)

            if head_limit:
                abs_files = abs_files[:head_limit]

            return "\n".join(abs_files) if abs_files else "No files found matching the pattern."
        except Exception as e:
            # fall back to Python glob if rg fails
            pass

    # Fallback: Python glob
    path = Path(root)
    if not path.exists():
        return f"Directory not found: {root}"

    # Support *.{a,b} fan-out
    patterns: List[str]
    if '{' in pattern and '}' in pattern:
        m = re.match(r'(.*)\{([^}]+)\}(.*)', pattern)
        if m:
            prefix, exts, suffix = m.groups()
            patterns = [f"{prefix}{ext}{suffix}" for ext in exts.split(',')]
        else:
            patterns = [pattern]
    else:
        patterns = [pattern]

    files: List[Path] = []
    for pat in patterns:
        files.extend(path.glob(pat))

    # unique, files only, abs & sort by mtime
    uniq = list({f.resolve() for f in files if f.is_file()})
    uniq.sort(key=lambda f: f.stat().st_mtime if f.exists() else 0, reverse=True)

    if head_limit:
        uniq = uniq[:head_limit]

    return "\n".join(str(f) for f in uniq) if uniq else "No files found matching the pattern."


@tool(parse_docstring=True)
def search_with_context(
    pattern: str,
    directory: Optional[str] = None,
    before_context: int = 2,
    after_context: int = 2,
    glob_pattern: Optional[str] = None
):
    """
    Search with asymmetric context using ripgrep.

    Args:
        pattern: REQUIRED
        directory: Search root (defaults to project directory; workspace/CWD fallback)
        before_context: Lines before
        after_context: Lines after
        glob_pattern: Optional file filter
    """
    root = _resolve_search_root(directory)

    if not _check_ripgrep_installed():
        return _ripgrep_error_message()

    cmd: List[str] = ['rg', '-n', '--heading', '--color=never', '-B', str(before_context), '-A', str(after_context)]
    if glob_pattern:
        cmd.extend(['-g', glob_pattern])
    cmd.extend([pattern, '.'])

    try:
        result = _run_rg(cmd, cwd=root, timeout=120)
        if result.returncode not in (0, 1):
            return "No matches found."
        return result.stdout.strip() if result.stdout.strip() else "No matches found."
    except Exception as e:
        return f"Error: {e}"


# exported
search_tools = [
    grep_search,
    glob_files,
    search_with_context
]
search_tools_map = {tool.name: tool for tool in search_tools}
