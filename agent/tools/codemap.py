"""
Code analysis tools with improved encoding detection, error handling, and automatic project directory detection.

Features:
- Automatically resolves relative paths to project directory
- Falls back to workspace/cwd if project not set
- Improved encoding detection with chardet
- Support for Python, JavaScript, TypeScript files
- Extract function/class definitions with line numbers
- Get full function implementations
"""
from typing import Optional
from pathlib import Path
import os
from langchain_core.tools import tool
from tree_sitter_languages import get_language, get_parser
import chardet
from agent.utils.paths import get_project_directory, get_workspace_path


def resolve_file_path(file_path: str) -> str:
    """
    Resolve file path to absolute path.
    
    Priority:
      1. If path is absolute -> return as-is
      2. If path is relative -> resolve relative to project directory
      3. Fallback to workspace, then cwd
    
    Args:
        file_path: Path to resolve (relative or absolute)
    
    Returns:
        Absolute path as string
    """
    # If already absolute, return as-is
    if os.path.isabs(file_path):
        return file_path
    
    # Try to resolve relative to project directory
    try:
        project_dir = get_project_directory()
        resolved = (Path(project_dir) / file_path).resolve()
        if resolved.exists():
            return str(resolved)
    except Exception:
        pass
    
    # Fallback to workspace
    try:
        workspace = get_workspace_path()
        resolved = (Path(workspace) / file_path).resolve()
        if resolved.exists():
            return str(resolved)
    except Exception:
        pass
    
    # Ultimate fallback to current working directory
    return str(Path(file_path).resolve())


@tool(parse_docstring=True)
def get_code_definitions(file_path: str) -> str:
    """
    Extract function and class definitions from a file.
    Shows signatures with their actual source file line numbers and ... between definitions.

    Args:
        file_path: Path to the source file to analyze (relative paths resolve to project directory)
    """
    # Resolve path
    full_path = resolve_file_path(file_path)
    
    # Determine language
    suffix = full_path.split(".")[-1]
    lang_map = {
        "py": "python",
        "js": "javascript",
        "jsx": "javascript",
        "ts": "typescript",
        "tsx": "typescript"
    }
    lang = lang_map.get(suffix)
    if not lang:
        return f"Unsupported file type: {suffix}"

    # Initialize parser - handle different tree-sitter-languages API versions
    try:
        language = get_language(lang)
        parser = get_parser(lang)
    except TypeError as e:
        # API change in tree-sitter-languages - try alternative initialization
        try:
            from tree_sitter import Language, Parser
            import tree_sitter_languages

            # Try getting language by attribute access
            lang_obj = getattr(tree_sitter_languages, f"get_{lang}", None)
            if lang_obj:
                language = lang_obj()
                parser = Parser()
                parser.set_language(language)
            else:
                return f"Error initializing tree-sitter for {lang}: {str(e)}\nPlease update tree-sitter-languages: pip install --upgrade tree-sitter-languages"
        except Exception as inner_e:
            return f"Error initializing tree-sitter for {lang}: {str(e)}\nInner error: {str(inner_e)}\nPlease update tree-sitter-languages: pip install --upgrade tree-sitter-languages"
    except Exception as e:
        return f"Error initializing tree-sitter for {lang}: {str(e)}"

    # Read file content with encoding detection
    try:
        with open(full_path, "rb") as f:
            raw_content = f.read()
        
        # Detect encoding
        detected = chardet.detect(raw_content)
        encoding = detected.get('encoding', 'utf-8')
        
        # For tree-sitter, we need bytes
        code = raw_content
    except FileNotFoundError:
        return f"File not found: {full_path}"
    except Exception as e:
        return f"Error reading file {full_path}: {str(e)}"
    
    tree = parser.parse(code)

    # Define query for functions and classes
    query_str = """
    (class_definition
        name: (identifier) @name.definition.class
        body: (block 
            (function_definition
                name: (identifier) @name.definition.method
                parameters: (parameters) @params.definition.method)?) @body.definition.class)

    (function_definition
        name: (identifier) @name.definition.function
        parameters: (parameters) @params.definition.function
        body: (block) @body.definition.function)
    """

    query = language.query(query_str)
    captures = query.captures(tree.root_node)

    # Process captures to extract definitions
    output_lines = [f"\n{full_path}:\n"]
    current_def = {}
    in_class = False
    last_line_number = 0
    
    for node, tag in captures:
        current_line = node.start_point[0] + 1
        
        # Add ... between definitions if there's a gap
        if last_line_number > 0 and current_line > last_line_number + 1:
            output_lines.append("...")

        if tag == "name.definition.class":
            in_class = True
            output_lines.append(f"{current_line}| class {node.text.decode('utf-8', errors='replace')}:")
            last_line_number = current_line
        elif tag == "name.definition.method" and in_class:
            method_name = node.text.decode('utf-8', errors='replace')
            current_def['method_name'] = method_name
            current_def['line'] = current_line
        elif tag == "params.definition.method" and in_class:
            params = node.text.decode('utf-8', errors='replace')
            line_num = current_def['line']
            output_lines.append(f"{line_num}|     def {current_def['method_name']}{params}:")
            last_line_number = line_num
        elif tag == "body.definition.method":
            line_num = node.start_point[0] + 1
            output_lines.append(f"{line_num}|         ...")
            last_line_number = line_num
        elif tag == "body.definition.class":
            in_class = False
        elif tag == "name.definition.function":
            current_def['name'] = node.text.decode('utf-8', errors='replace')
            current_def['line'] = current_line
        elif tag == "params.definition.function":
            params = node.text.decode('utf-8', errors='replace')
            line_num = current_def['line']
            output_lines.append(f"{line_num}| def {current_def['name']}{params}:")
            last_line_number = line_num
        elif tag == "body.definition.function":
            line_num = node.start_point[0] + 1
            output_lines.append(f"{line_num}|     ...")
            last_line_number = line_num

    return "\n".join(output_lines)


@tool(parse_docstring=True)
def get_function_implementation(file_path: str, function_name: str) -> Optional[str]:
    """
    Extract the implementation of a specific function or method from a file.

    Args:
        file_path: Path to the source file (relative paths resolve to project directory)
        function_name: Name of the function to find
    """
    # Resolve path
    full_path = resolve_file_path(file_path)
    
    # Determine language
    suffix = full_path.split(".")[-1]
    lang_map = {
        "py": "python",
        "js": "javascript",
        "jsx": "javascript",
        "ts": "typescript",
        "tsx": "typescript"
    }
    lang = lang_map.get(suffix)
    if not lang:
        return None

    # Initialize parser - handle different tree-sitter-languages API versions
    try:
        language = get_language(lang)
        parser = get_parser(lang)
    except TypeError as e:
        try:
            from tree_sitter import Language, Parser
            import tree_sitter_languages
            lang_obj = getattr(tree_sitter_languages, f"get_{lang}", None)
            if lang_obj:
                language = lang_obj()
                parser = Parser()
                parser.set_language(language)
            else:
                return f"Error initializing tree-sitter for {lang}: {str(e)}"
        except Exception as inner_e:
            return f"Error initializing tree-sitter: {str(e)}, {str(inner_e)}"
    except Exception as e:
        return f"Error initializing tree-sitter for {lang}: {str(e)}"

    # Read file content with encoding detection
    try:
        with open(full_path, "rb") as f:
            raw_content = f.read()
        
        # Detect encoding
        detected = chardet.detect(raw_content)
        encoding = detected.get('encoding', 'utf-8')
        
        code = raw_content  # tree-sitter needs bytes
    except FileNotFoundError:
        return f"File not found: {full_path}"
    except Exception as e:
        return f"Error reading file {full_path}: {str(e)}"
    
    tree = parser.parse(code)

    # Define query for functions and methods
    query_str = """
    (function_definition
        name: (identifier) @name.function
        parameters: (parameters) @params.function
        body: (block) @body.function)

    (class_definition
        body: (block 
            (function_definition
                name: (identifier) @name.method
                parameters: (parameters) @params.method
                body: (block) @body.method)))
    """

    query = language.query(query_str)
    captures = query.captures(tree.root_node)

    # Find the specific function
    current_def = {}
    for node, tag in captures:
        if tag in ["name.function", "name.method"]:
            if node.text.decode('utf-8', errors='replace') == function_name:
                current_def['name'] = node.text.decode('utf-8', errors='replace')
                current_def['line'] = node.start_point[0] + 1
        elif tag in ["params.function", "params.method"] and current_def.get('name') == function_name:
            current_def['params'] = node.text.decode('utf-8', errors='replace')
        elif tag in ["body.function", "body.method"] and current_def.get('name') == function_name:
            # Extract the full implementation
            implementation = code[node.start_byte:node.end_byte].decode(encoding, errors='replace')
            lines = implementation.split('\n')
            
            # Format output
            output_lines = [f"\n{full_path}:\n"]
            start_line = current_def['line']
            
            # Add function signature
            output_lines.append(f"{start_line}| def {current_def['name']}{current_def['params']}:")
            
            # Add implementation lines with correct line numbers
            for i, line in enumerate(lines):
                line_num = start_line + i + 1
                # Handle indentation
                indent = '    ' if not line.strip() else line[:len(line) - len(line.lstrip())]
                output_lines.append(f"{line_num}|{indent}{line.lstrip()}")
            
            return "\n".join(output_lines)

    return None


@tool(parse_docstring=True)
def get_code_definitions_multi(file_paths: list[str]) -> str:
    """
    Extract function and class definitions from multiple files.
    Shows signatures with their actual source file line numbers and ... between definitions.
    
    Args:
        file_paths: List of file paths to analyze (relative paths resolve to project directory)
    """
    all_definitions = []
    
    for file_path in file_paths:
        definitions = get_code_definitions(file_path)
        if definitions and not definitions.startswith("Unsupported") and not definitions.startswith("Error"):
            all_definitions.append(definitions)
    
    return "\n".join(all_definitions) if all_definitions else "No definitions found in provided files"


@tool(parse_docstring=True)
def get_raw_file_content(file_path: str) -> str:
    """
    Get the raw content of the file with automatic encoding detection.
    Good for non-code files or when you need the exact content.
    
    Args:
        file_path: File path to read (relative paths resolve to project directory)
    """
    # Resolve path
    full_path = resolve_file_path(file_path)
    
    try:
        # Read as binary first
        with open(full_path, "rb") as f:
            raw_content = f.read()
        
        # Detect encoding
        detected = chardet.detect(raw_content)
        encoding = detected.get('encoding', 'utf-8')
        confidence = detected.get('confidence', 0)
        
        # If low confidence or None, try common encodings
        if not encoding or confidence < 0.7:
            encodings_to_try = ['utf-8', 'utf-8-sig', 'latin-1', 'cp1252', 'iso-8859-1']
            for enc in encodings_to_try:
                try:
                    return raw_content.decode(enc)
                except (UnicodeDecodeError, AttributeError):
                    continue
            # If all fail, use utf-8 with error replacement
            return raw_content.decode('utf-8', errors='replace')
        
        # Use detected encoding
        return raw_content.decode(encoding, errors='replace')
        
    except FileNotFoundError:
        return f"File not found: {full_path}"
    except Exception as e:
        return f"Error reading file {full_path}: {str(e)}"


# List of available tools
codemap_tools = [
    get_code_definitions,
    get_function_implementation,
    get_code_definitions_multi,
    get_raw_file_content
]
codemap_tools_map = {tool.name: tool for tool in codemap_tools}
