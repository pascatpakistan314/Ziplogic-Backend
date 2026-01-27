"""
Edit tools for code reviewer - Claude Code style editing
Supports: replace_range, search_replace, insert_lines, read_file_lines
"""

import os
import re
from typing import Optional
from langchain_core.tools import tool


@tool(parse_docstring=True)
def replace_range(
    file_path: str,
    start_line: int,
    end_line: int,
    new_content: str
) -> str:
    """
    Replace a range of lines in a file (Claude Code style).

    Args:
        file_path: Path to file (absolute or relative to workspace)
        start_line: Starting line number (1-indexed, inclusive)
        end_line: Ending line number (1-indexed, inclusive)
        new_content: New content to insert (can be empty to delete)

    Returns:
        Success/error message
    """
    try:
        if not os.path.exists(file_path):
            return f"Error: File not found: {file_path}"

        # Read file
        with open(file_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()

        # Validate line numbers
        if start_line < 1 or end_line < start_line or end_line > len(lines):
            return f"Error: Invalid line range {start_line}-{end_line} for file with {len(lines)} lines"

        # Replace range (convert to 0-indexed)
        new_lines = lines[:start_line-1] + [new_content + '\n'] + lines[end_line:]

        # Write back
        with open(file_path, 'w', encoding='utf-8') as f:
            f.writelines(new_lines)

        return f"✓ Replaced lines {start_line}-{end_line} in {file_path}"

    except Exception as e:
        return f"Error replacing range in {file_path}: {str(e)}"


@tool(parse_docstring=True)
def search_replace(
    file_path: str,
    search_pattern: str,
    replacement: str,
    is_regex: bool = False,
    max_replacements: Optional[int] = None
) -> str:
    """
    Search and replace in a file (supports regex).

    Args:
        file_path: Path to file
        search_pattern: Pattern to search for (string or regex)
        replacement: Replacement text
        is_regex: Whether search_pattern is a regex (default: False)
        max_replacements: Max number of replacements (None = all)

    Returns:
        Success message with count of replacements
    """
    try:
        if not os.path.exists(file_path):
            return f"Error: File not found: {file_path}"

        # Read file
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()

        # Perform replacement
        if is_regex:
            if max_replacements:
                new_content, count = re.subn(search_pattern, replacement, content, count=max_replacements)
            else:
                new_content, count = re.subn(search_pattern, replacement, content)
        else:
            if max_replacements:
                new_content = content.replace(search_pattern, replacement, max_replacements)
                count = min(content.count(search_pattern), max_replacements)
            else:
                count = content.count(search_pattern)
                new_content = content.replace(search_pattern, replacement)

        if count == 0:
            return f"No matches found for '{search_pattern}' in {file_path}"

        # Write
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(new_content)

        return f"✓ Made {count} replacement(s) in {file_path}"

    except Exception as e:
        return f"Error in search_replace for {file_path}: {str(e)}"


@tool(parse_docstring=True)
def read_file_lines(
    file_path: str,
    start_line: Optional[int] = None,
    end_line: Optional[int] = None
) -> str:
    """
    Read specific lines from a file with line numbers.

    Args:
        file_path: Path to file
        start_line: Starting line (1-indexed, None = start)
        end_line: Ending line (1-indexed, None = end)

    Returns:
        File content with line numbers
    """
    try:
        if not os.path.exists(file_path):
            return f"Error: File not found: {file_path}"

        with open(file_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()

        # Slice lines
        start = (start_line - 1) if start_line else 0
        end = end_line if end_line else len(lines)

        selected = lines[start:end]

        # Format with line numbers
        output = []
        for i, line in enumerate(selected, start=start+1):
            output.append(f"{i:4d}| {line.rstrip()}")

        return "\n".join(output)

    except Exception as e:
        return f"Error reading {file_path}: {str(e)}"


@tool(parse_docstring=True)
def insert_lines(
    file_path: str,
    after_line: int,
    content: str
) -> str:
    """
    Insert new lines after a specific line number.

    Args:
        file_path: Path to file
        after_line: Line number to insert after (0 = insert at start)
        content: Content to insert

    Returns:
        Success message
    """
    try:
        if not os.path.exists(file_path):
            return f"Error: File not found: {file_path}"

        with open(file_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()

        if after_line < 0 or after_line > len(lines):
            return f"Error: Invalid line number {after_line} for file with {len(lines)} lines"

        # Insert content
        new_lines = lines[:after_line] + [content + '\n'] + lines[after_line:]

        with open(file_path, 'w', encoding='utf-8') as f:
            f.writelines(new_lines)

        return f"✓ Inserted content after line {after_line} in {file_path}"

    except Exception as e:
        return f"Error inserting lines in {file_path}: {str(e)}"


# Export tools
edit_tools = [
    replace_range,
    search_replace,
    read_file_lines,
    insert_lines
]

edit_tools_map = {tool.name: tool for tool in edit_tools}
