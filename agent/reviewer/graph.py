"""Reviewer Agent Graph - Simplified Claude Code style with Resilient LLM"""

from dotenv import load_dotenv
load_dotenv()

import os
import json
import time
from pathlib import Path
from langchain_openai import ChatOpenAI
from langchain_core.messages import AIMessage
from langgraph.constants import END, START
from langgraph.graph import StateGraph
from langgraph.prebuilt import ToolNode

from agent.reviewer.state import CodeReviewerState, ProposedEdit, AppliedEdit
from agent.tools.search import search_tools, glob_files
from agent.tools.codemap import codemap_tools
from agent.tools.edit import edit_tools
from agent.tools.write import get_files_structure
from helpers.prompts import markdown_to_prompt_template
from agent.utils.paths import get_project_directory
from openai import APITimeoutError
import httpx

# Load prompts
find_issues_prompt = markdown_to_prompt_template("agent/reviewer/prompts/find_issues.md")
propose_fixes_prompt = markdown_to_prompt_template("agent/reviewer/prompts/propose_fixes.md")

# Helper function to build resilient LLM for reviewer
def build_reviewer_llm():
    """Build a ChatOpenAI instance with resilient settings for code review."""
    return ChatOpenAI(
        model="gpt-5",           # Valid OpenAI model (was gpt-5)
        temperature=1,            # Keep original temperature
        timeout=90,               # Give slower networks headroom
        max_retries=3,            # Transient failures retry automatically
        model_kwargs={"max_completion_tokens": 8192},  # Keep original token limit
    )

# Backoff wrapper for LLM invocations
def invoke_with_backoff(runnable, payload, retries=3, base_wait=2):
    """
    Invoke a runnable with exponential backoff on timeout errors.

    Args:
        runnable: The LangChain runnable to invoke
        payload: The input payload for the runnable
        retries: Maximum number of retry attempts
        base_wait: Base wait time in seconds (will be exponentially increased)

    Returns:
        The result of the runnable invocation

    Raises:
        APITimeoutError or httpx.ConnectTimeout if all retries are exhausted
    """
    for attempt in range(1, retries + 1):
        try:
            return runnable.invoke(payload)
        except (APITimeoutError, httpx.ConnectTimeout) as e:
            if attempt == retries:
                raise
            wait = base_wait ** attempt
            print(f"[LLM] Timeout, retrying in {wait}s (attempt {attempt}/{retries})")
            time.sleep(wait)

# Create runnables with resilient LLM
model = build_reviewer_llm()

# Bind tools to find_issues so it can use search/codemap tools
find_issues_runnable = find_issues_prompt | model.bind_tools(search_tools + codemap_tools)
propose_fixes_runnable = propose_fixes_prompt | model

# Tool node for research phase
tool_node = ToolNode(search_tools + codemap_tools, messages_key="review_scratchpad")


def detect_project_file_extensions(project_dir: Path) -> list[str]:
    """
    Intelligently detect which code file extensions exist in the project.
    Uses fast os.walk to scan once and return only relevant patterns.

    Returns:
        List of glob patterns like "**/*.js", "**/*.py" for files that actually exist
    """
    # Map extensions to their glob patterns
    extension_map = {
        ".py": "**/*.py",       # Python
        ".js": "**/*.js",       # JavaScript
        ".jsx": "**/*.jsx",     # React JSX
        ".ts": "**/*.ts",       # TypeScript
        ".tsx": "**/*.tsx",     # React TSX
        ".java": "**/*.java",   # Java
        ".go": "**/*.go",       # Go
        ".rs": "**/*.rs",       # Rust
        ".cpp": "**/*.cpp",     # C++
        ".c": "**/*.c",         # C
        ".h": "**/*.h",         # C/C++ headers
        ".hpp": "**/*.hpp",     # C++ headers
        ".cs": "**/*.cs",       # C#
        ".rb": "**/*.rb",       # Ruby
        ".php": "**/*.php",     # PHP
        ".swift": "**/*.swift", # Swift
        ".kt": "**/*.kt",       # Kotlin
        ".scala": "**/*.scala", # Scala
    }

    found_extensions = set()

    try:
        # Quick scan using os.walk (no tool calls!)
        for root, dirs, files in os.walk(project_dir):
            # Skip hidden directories and common non-code directories
            dirs[:] = [d for d in dirs if not d.startswith('.') and d not in {'node_modules', '__pycache__', 'dist', 'build', 'target', 'venv'}]

            for file in files:
                if file.startswith('.'):
                    continue

                _, ext = os.path.splitext(file)
                if ext in extension_map:
                    found_extensions.add(ext)

        # Convert extensions to glob patterns
        patterns = [extension_map[ext] for ext in sorted(found_extensions)]

        print(f"[REVIEWER] Detected file extensions in project: {sorted(found_extensions)}")
        print(f"[REVIEWER] Will use {len(patterns)} glob patterns instead of {len(extension_map)}")

        return patterns if patterns else ["**/*.py"]  # Fallback to Python if nothing found

    except Exception as e:
        print(f"[REVIEWER] Error detecting extensions: {e}, using default patterns")
        return ["**/*.py", "**/*.js", "**/*.ts"]  # Safe fallback


def find_issues(state: CodeReviewerState):
    """Use tools to find issues in files"""
    # Get project directory from state (passed from orchestrated agent)
    project_dir = get_project_directory(state)

    # Debug logging
    print(f"[REVIEWER] Project directory resolved to: {project_dir}")
    print(f"[REVIEWER] State workspace_path: {state.workspace_path}")

    # Auto-detect files if not provided
    files = state.files_to_review
    if not files:
        # Intelligently detect which file extensions exist in the project
        # This avoids wasting tool calls on extensions that don't exist!
        patterns = detect_project_file_extensions(project_dir)

        all_files = []
        try:
            for pattern in patterns:
                result = glob_files.invoke({
                    "pattern": pattern,
                    "directory": str(project_dir),
                    "head_limit": 100  # Limit per pattern to avoid overwhelming
                })
                if result and "No files found" not in result and "Error" not in result:
                    all_files.extend(result.strip().split("\n"))

            # Remove duplicates and limit total
            files = list(dict.fromkeys(all_files))[:200]  # Max 200 files total
            print(f"[REVIEWER] Found {len(files)} files to review across {len(patterns)} file types")

        except Exception as e:
            print(f"[REVIEWER] Error auto-detecting files: {e}")
            files = []

    response = find_issues_runnable.invoke({
        "files_to_review": files,
        "review_scratchpad": state.review_scratchpad,
        "codebase_structure": get_files_structure.invoke({"directory": str(project_dir)})
    })

    return {"review_scratchpad": [response], "files_to_review": files}


def propose_fixes(state: CodeReviewerState):
    """Generate proposed edits for issues (returns JSON)"""
    response = propose_fixes_runnable.invoke({
        "review_scratchpad": state.review_scratchpad
    })

    # Parse JSON response to extract proposed edits
    edits = []
    try:
        content = response.content
        if "```json" in content:
            json_str = content.split("```json")[1].split("```")[0].strip()
        elif "```" in content:
            json_str = content.split("```")[1].split("```")[0].strip()
        else:
            json_str = content.strip()

        edits_data = json.loads(json_str)

        # Handle both list and dict responses
        if isinstance(edits_data, list):
            edits = [ProposedEdit(**edit) for edit in edits_data]
        elif isinstance(edits_data, dict):
            edits = [ProposedEdit(**edits_data)]

    except Exception as e:
        print(f"Error parsing edits: {e}")
        print(f"Response content: {response.content}")

    return {
        "proposed_edits": edits,
        "review_scratchpad": [AIMessage(content=f"Proposed {len(edits)} edits")]
    }


def apply_fixes(state: CodeReviewerState):
    """Apply proposed edits if auto_apply is enabled"""
    if not state.auto_apply:
        return {"review_scratchpad": [AIMessage(content="Auto-apply disabled. Edits proposed only.")]}

    applied = []

    for edit in state.proposed_edits:
        try:
            if edit.edit_type == "replace_range":
                from agent.tools.edit import replace_range
                result = replace_range.invoke({
                    "file_path": edit.file_path,
                    "start_line": edit.start_line,
                    "end_line": edit.end_line,
                    "new_content": edit.new_content
                })
                success = "✓" in result
                applied.append(AppliedEdit(edit=edit, success=success, message=result))

            elif edit.edit_type == "search_replace":
                from agent.tools.edit import search_replace
                result = search_replace.invoke({
                    "file_path": edit.file_path,
                    "search_pattern": edit.search_pattern,
                    "replacement": edit.replacement,
                    "is_regex": False
                })
                success = "✓" in result
                applied.append(AppliedEdit(edit=edit, success=success, message=result))

            elif edit.edit_type == "insert_lines":
                from agent.tools.edit import insert_lines
                result = insert_lines.invoke({
                    "file_path": edit.file_path,
                    "after_line": edit.after_line,
                    "content": edit.new_content
                })
                success = "✓" in result
                applied.append(AppliedEdit(edit=edit, success=success, message=result))

        except Exception as e:
            applied.append(AppliedEdit(
                edit=edit,
                success=False,
                message=f"Error: {str(e)}"
            ))

    return {"applied_edits": applied}


def save_artifacts(state: CodeReviewerState):
    """Save artifacts to JSON files"""
    artifacts_dir = Path("artifacts")
    artifacts_dir.mkdir(exist_ok=True)

    # Save proposed_edits.json
    with open(artifacts_dir / "proposed_edits.json", "w", encoding="utf-8") as f:
        json.dump([edit.model_dump() for edit in state.proposed_edits], f, indent=2)

    # Save applied_edits.json
    with open(artifacts_dir / "applied_edits.json", "w", encoding="utf-8") as f:
        json.dump([edit.model_dump() for edit in state.applied_edits], f, indent=2)

    return {"review_scratchpad": [AIMessage(content="Artifacts saved")]}


def should_continue(state: CodeReviewerState):
    """Router: Continue using tools or move to propose fixes"""
    if state.review_scratchpad:
        last_message = state.review_scratchpad[-1]
        if hasattr(last_message, 'tool_calls') and last_message.tool_calls:
            return "tools"

    return "propose_fixes"


# Define workflow
workflow = StateGraph(CodeReviewerState)

# Add nodes
workflow.add_node("find_issues", find_issues)
workflow.add_node("tools", tool_node)
workflow.add_node("propose_fixes", propose_fixes)
workflow.add_node("apply_fixes", apply_fixes)
workflow.add_node("save_artifacts", save_artifacts)

# Add edges
workflow.add_edge(START, "find_issues")
workflow.add_conditional_edges(
    "find_issues",
    should_continue,
    {
        "tools": "tools",
        "propose_fixes": "propose_fixes"
    }
)
workflow.add_edge("tools", "find_issues")
workflow.add_edge("propose_fixes", "apply_fixes")
workflow.add_edge("apply_fixes", "save_artifacts")
workflow.add_edge("save_artifacts", END)

# Compile
swe_reviewer = workflow.compile()
