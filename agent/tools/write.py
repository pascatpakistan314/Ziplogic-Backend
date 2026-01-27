from langchain_core.tools import tool
import os
from gitingest import ingest
import asyncio
from typing import Optional

# WebSocket integration for real-time file updates
_websocket_handler: Optional[object] = None
_workspace_path: str = None  # Dynamic workspace path set by set_workspace_path()

def set_websocket_handler(handler):
    """Set WebSocket handler for real-time file updates"""
    global _websocket_handler
    _websocket_handler = handler

def set_workspace_path(workspace_path: str):
    """Set the workspace path for file operations"""
    global _workspace_path
    _workspace_path = workspace_path
    # Also set it on the tool functions for access
    create_file._workspace_path = workspace_path
    write_to_file._workspace_path = workspace_path

def get_workspace_path() -> Optional[str]:
    """Get the current workspace path"""
    global _workspace_path
    return _workspace_path

async def notify_file_change(file_path: str, content: str):
    """Notify frontend of file changes via WebSocket"""
    if _websocket_handler and hasattr(_websocket_handler, 'send_files'):
        try:
            await _websocket_handler.send_files({file_path: content})
        except Exception as e:
            print(f"Error sending file update via WebSocket: {e}")


@tool(parse_docstring=True)
def create_file(path: str, content: str) -> str:
    """Create a new file at the specified path with the given content. If the file already exists,
    returns an error message instead of overwriting.

    Args:
        path: The path from the root of the folder to the file
        content: The text content to write to the file

    Returns:
        str: A success message with the file path, or an error message if creation failed
    """
    try:
        # Use workspace_path from context if available, error if not set
        workspace_path = getattr(create_file, '_workspace_path', None)
        if workspace_path is None:
            raise ValueError("Workspace path not set. Call set_workspace_path() first.")
        if not path.startswith('/') and not os.path.isabs(path):
            full_path = os.path.join(workspace_path, path)
        else:
            full_path = path

        # Ensure the directory exists
        os.makedirs(os.path.dirname(full_path), exist_ok=True)

        with open(full_path, "w", encoding="utf-8") as f:
            f.write(content)

        # Notify WebSocket of file change (run async in background)
        try:
            asyncio.create_task(notify_file_change(full_path, content))
        except Exception:
            # If event loop is not running, skip WebSocket notification
            pass

        return f"Successfully created file at {full_path}"
    except Exception as e:
        return f"Error creating file: {str(e)}"


@tool(parse_docstring=True)
def write_to_file(path: str, content: str) -> str:
    """Override the content of an existing file at the specified path. If the file doesn't exist,
    returns an error message instead of creating it.

    Args:
        path: The path to the file to override
        content: The new text content that will completely replace the current content

    Returns:
        str: A success message with the file path, or an error message if writing failed
    """
    try:
        if not os.path.exists(path):
            return f"Error: File {path} does not exist"

        with open(path, "w", encoding="utf-8") as f:
            f.write(content)

        # Notify WebSocket of file change (run async in background)
        try:
            asyncio.create_task(notify_file_change(path, content))
        except Exception:
            # If event loop is not running, skip WebSocket notification
            pass

        return f"Successfully overridden file at {path}"
    except Exception as e:
        return f"Error overriding file: {str(e)}"


@tool(parse_docstring=True)
def get_files_structure(directory: str = "./workspace_repo") -> str:
    """Generate a JSON representation of the file and directory structure starting from the specified directory.
    Uses gitingest to analyze the codebase structure.

    Args:
        directory: The root directory to start scanning from (defaults to "./workspace_repo")

    Returns:
        str: A string representing the hierarchical directory structure and file listing
    """
    try:
        # Try to use gitingest if not in event loop
        summary, tree, content = ingest(directory)
        return tree
    except RuntimeError as e:
        if "asyncio.run() cannot be called from a running event loop" in str(e):
            # Fallback to simple directory listing
            import json
            
            def build_tree(path):
                tree = {}
                try:
                    for item in sorted(os.listdir(path)):
                        if item.startswith('.'):
                            continue
                        item_path = os.path.join(path, item)
                        if os.path.isdir(item_path):
                            # Limit depth to avoid recursion issues
                            tree[item] = "directory"
                        else:
                            tree[item] = "file"
                except PermissionError:
                    pass
                return tree
            
            structure = build_tree(directory)
            return json.dumps(structure, indent=2)
        else:
            raise


# List of available tools
write_tools = [create_file, write_to_file, get_files_structure]
write_tools_map = {tool.name: tool for tool in write_tools}
