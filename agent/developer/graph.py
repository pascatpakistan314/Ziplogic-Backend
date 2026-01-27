import json
import os
import re
import time
from typing import List
from diff_match_patch import diff_match_patch
from langchain_openai import ChatOpenAI
from langchain_core.messages import AnyMessage, AIMessage, HumanMessage
from langchain_core.output_parsers import StrOutputParser, JsonOutputParser
from langgraph.constants import END, START
from langgraph.graph import StateGraph
from langgraph.checkpoint.redis import RedisSaver
from helpers.prompts import markdown_to_prompt_template
from agent.developer.state import SoftwareDeveloperState, Diffs
from agent.developer.helpers import bump_step_counter, get_step_count
from langgraph.prebuilt import ToolNode
from agent.tools.search import search_tools
from agent.tools.codemap import codemap_tools
from agent.tools.write import get_files_structure
from agent.utils.paths import get_project_directory
from openai import APITimeoutError
import httpx

# Helper function to build resilient LLM with timeout and retries
def build_code_llm():
    """Build a ChatOpenAI instance with resilient settings for code generation."""
    return ChatOpenAI(
        model="gpt-5",            # GPT-5 model
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

# Load the extract diff prompt
extract_diffs_tasks_prompt = markdown_to_prompt_template("agent/developer/prompts/create_diff_prompt.md")
implement_diffs_prompt = markdown_to_prompt_template("agent/developer/prompts/implement_diff.md")
implement_new_file_prompt = markdown_to_prompt_template("agent/developer/prompts/implement_new_file.md")

# Create the runnables with resilient LLM
extract_diff_runnable = extract_diffs_tasks_prompt | build_code_llm() | StrOutputParser()
edit_according_to_diff_runnable = implement_diffs_prompt | build_code_llm() | StrOutputParser()
create_new_file_runnable = implement_new_file_prompt | build_code_llm() | StrOutputParser()

# Load the get clear implementation plan prompt
get_clear_implementation_plan_prompt = markdown_to_prompt_template("agent/developer/prompts/get_clear_implementation_plan.md")

# Create the runnable with the prompt and resilient model with tools bound
get_clear_implementation_plan_runnable = (
    get_clear_implementation_plan_prompt | build_code_llm().bind_tools(search_tools + codemap_tools)
)

dmp = diff_match_patch()

def start_implementing(state: SoftwareDeveloperState):
    return {
        "current_task_idx": 0,
        "current_atomic_task_idx": 0
    }


def proceed_to_next_atomic_task(state: SoftwareDeveloperState):
    # Get current indices
    current_task_idx = state.current_task_idx
    current_atomic_task_idx = state.current_atomic_task_idx
    
    # Get the implementation plan
    plan = state.implementation_plan
    
    # Get current task
    current_task = plan.tasks[current_task_idx]
    atomic_tasks = current_task.atomic_tasks
    
    # If we've completed all atomic tasks in current task
    if current_atomic_task_idx >= len(atomic_tasks) - 1:
        # Move to next main task and reset atomic task index
        return {
            "current_task_idx": current_task_idx + 1,
            "current_atomic_task_idx": 0
        }
    # Otherwise, move to next atomic task
    return {
        "current_task_idx": current_task_idx,
        "current_atomic_task_idx": current_atomic_task_idx + 1
    }


def get_clear_implementation_plan_for_atomic_task(state: SoftwareDeveloperState):
    # Increment step counter to track research depth
    current_step = bump_step_counter(state)

    current_task = state.implementation_plan.tasks[state.current_task_idx]
    current_atomic_task = current_task.atomic_tasks[state.current_atomic_task_idx]

    payload = {
        "development_task": current_atomic_task.atomic_task,
        "file_content": state.current_file_content,
        "target_file": current_task.file_path,
        "codebase_structure": state.codebase_structure,
        "additional_context": current_atomic_task.additional_context,
        "atomic_implementation_research": state.atomic_implementation_research
    }

    try:
        result = invoke_with_backoff(get_clear_implementation_plan_runnable, payload)
    except (APITimeoutError, httpx.ConnectTimeout) as e:
        # Fail-soft: push a stub note so router proceeds to implement_plan
        print(f"[LLM] Planning timeout on {current_task.file_path}; proceeding without extra research.")
        result = HumanMessage(content=f"LLM planning timed out; proceed using existing context. Error: {e}")

    # Return updated state with incremented counter
    return {
        "atomic_implementation_research": [result],
        "step_counter": state.step_counter
    }

def should_continue_implementation_research(state: SoftwareDeveloperState):
    """Router to decide if we keep researching or start implementing."""
    research = state.atomic_implementation_research or []

    # HARD GUARDRAIL: Check step counter first to prevent infinite loops
    current_steps = get_step_count(state)
    MAX_STEPS_PER_ATOMIC_TASK = 10  # Hard limit on research cycles

    if current_steps >= MAX_STEPS_PER_ATOMIC_TASK:
        print(f"[GUARDRAIL] Hard stop at {current_steps} research cycles → implementing")
        return "implement_plan"

    # If nothing yet, proceed to implementation (the previous step created the first result)
    if not research:
        return "implement_plan"

    last_research_step = research[-1]
    last_has_tool_calls = bool(getattr(last_research_step, "tool_calls", None))

    if last_has_tool_calls:
        # Stop condition 1: plateau (repetitive content in last 3 steps)
        if len(research) >= 4:
            recent = [str(getattr(m, "content", ""))[:100] for m in research[-3:]]
            if (recent[0] == recent[2]) or (recent[1] == recent[2]):
                print("Research plateau detected → implementing")
                return "implement_plan"

        # Stop condition 2: depth limit (reduced from 6 to 5)
        if len(research) > 5:
            print(f"Research depth cap reached ({len(research)}) → implementing")
            return "implement_plan"

        return "should_continue_research"

    # Stop condition 3: content quality + at least 2 tool results overall
    if len(research) >= 2:
        last_content = str(getattr(last_research_step, "content", ""))
        if len(last_content) > 200:
            tool_results = sum(1 for m in research if getattr(m, "type", None) == "tool")
            if tool_results >= 2:
                print(f"Research sufficient ({tool_results} tool results) → implementing")
                return "implement_plan"

    return "implement_plan"



def prepare_for_implementation(state: SoftwareDeveloperState):
    project_dir = get_project_directory(state)
    current_task = state.implementation_plan.tasks[state.current_task_idx]

    # Check if file exists first
    if not os.path.exists(current_task.file_path):
        file_content = "This is a new file"
    else:
        try:
            # Try UTF-8 first
            with open(current_task.file_path, "r", encoding="utf-8") as file:
                file_content = file.read()
        except UnicodeDecodeError:
            # If UTF-8 fails, try with Latin-1 (which accepts all byte values)
            try:
                with open(current_task.file_path, "r", encoding="latin-1") as file:
                    file_content = file.read()
                print(f"[DEVELOPER] Warning: File {current_task.file_path} is not UTF-8 encoded, read with latin-1")
            except Exception as e:
                # If it's a binary file, skip it
                print(f"[DEVELOPER] Error: Cannot read file {current_task.file_path} as text: {e}")
                file_content = f"# ERROR: Cannot read this file as text. It may be a binary file.\n# File: {current_task.file_path}\n# Error: {e}"
        except Exception as e:
            # Catch any other unexpected errors
            print(f"[DEVELOPER] Unexpected error reading file {current_task.file_path}: {e}")
            file_content = f"# ERROR: Unexpected error reading file\n# File: {current_task.file_path}\n# Error: {e}"

    return {
        "current_file_content": file_content,
        "codebase_structure": get_files_structure.invoke({"directory": str(project_dir)}),
        "atomic_implementation_research": []  # ← was None
    }



def is_implementation_complete(state: SoftwareDeveloperState):
    """
    Check if we've completed all implementation tasks.
    """
    current_task_idx = state.current_task_idx
    plan = state.implementation_plan

    # Safety check 1: Null/None plan validation
    if plan is None or not hasattr(plan, 'tasks'):
        print(f"[DEVELOPER] ⚠ ERROR: Implementation plan is invalid or missing")
        return END

    # Safety check 2: Bounds check - are we beyond all tasks?
    if current_task_idx >= len(plan.tasks):
        print(f"[DEVELOPER] ✅ All {len(plan.tasks)} main task(s) completed successfully")
        return END

    # Safety check 3: Verify current task has atomic tasks to prevent empty loops
    try:
        current_task = plan.tasks[current_task_idx]
        if not hasattr(current_task, 'atomic_tasks') or not current_task.atomic_tasks:
            print(f"[DEVELOPER] ⚠ Task {current_task_idx + 1} has no atomic tasks, ending workflow")
            return END

        # Log progress for visibility
        total_atomic_tasks = len(current_task.atomic_tasks)
        print(f"[DEVELOPER] → Processing main task {current_task_idx + 1}/{len(plan.tasks)} ({total_atomic_tasks} atomic tasks)")
        return "continue"

    except (IndexError, AttributeError) as e:
        print(f"[DEVELOPER] ⚠ ERROR accessing task {current_task_idx}: {e}")
        return END
def convert_tools_messages_to_ai_and_human(implementation_research_scratchpad: List[AnyMessage]):
    messages = []
    for message in implementation_research_scratchpad:
        if message.type == "ai":
            if message.tool_calls:
                tool_name = message.tool_calls[0]["name"]
                tool_args = json.dumps(message.tool_calls[0]["args"])
                messages.append(AIMessage(content=f"I want to call the tool {tool_name} with the following arguments: {tool_args}"))
            else:
                messages.append(message)
        elif message.type == "tool":
            messages.append(HumanMessage(content=f"When executing Tool {message.name} \n The result was {message.content} was called"))
        else:
            messages.append(message)
    return messages

# Helper function to clean generated content
def clean_file_content(content: str) -> str:
    """Clean the generated content from any wrapping or artifacts"""
    if not content:
        return ""
    
    # Remove tool call wrapping if present
    if content.startswith("I want to call the tool"):
        try:
            json_start = content.find('{"')
            if json_start != -1:
                json_str = content[json_start:]
                json_str = json_str.rstrip('"')
                if not json_str.endswith('}'):
                    json_str += '}'
                args = json.loads(json_str)
                content = args.get("file_content", content)
        except:
            match = re.search(r'"file_content":\s*"([^"]*)"', content)
            if match:
                content = match.group(1)
                content = content.replace('\\n', '\n').replace('\\"', '"').replace('\\\\', '\\')
    
    # Remove markdown code blocks if present
    if "```" in content:
        content = re.sub(r'^```[a-zA-Z]*\n', '', content, flags=re.MULTILINE)
        content = re.sub(r'\n```$', '', content, flags=re.MULTILINE)
    
    # Remove explanation patterns
    explanation_patterns = [
        r"^Here's the complete.*?:\n+",
        r"^I'll generate.*?:\n+",
        r"^Creating.*?:\n+",
        r"^The following is.*?:\n+",
    ]
    
    for pattern in explanation_patterns:
        content = re.sub(pattern, "", content, flags=re.MULTILINE | re.IGNORECASE)
    
    return content.strip()

def creating_diffs_for_task(state: SoftwareDeveloperState):
    # Get current task information
    current_task = state.implementation_plan.tasks[state.current_task_idx]
    current_atomic_task = current_task.atomic_tasks[state.current_atomic_task_idx]
    file_path = current_task.file_path

    # check if file is new
    if not os.path.exists(file_path):
        # Ensure the directory exists before creating the file
        directory = os.path.dirname(file_path)
        if directory and not os.path.exists(directory):
            os.makedirs(directory, exist_ok=True)

        # Generate complete file in one go - no sectional generation
        payload = {
            "task": current_atomic_task.atomic_task,
            "additional_context": current_atomic_task.additional_context,
            "research": convert_tools_messages_to_ai_and_human(state.atomic_implementation_research),
            "file_path": file_path
        }

        try:
            new_file_content = invoke_with_backoff(create_new_file_runnable, payload)
        except (APITimeoutError, httpx.ConnectTimeout) as e:
            print(f"[LLM] File creation timeout for {file_path}; creating stub file.")
            new_file_content = f"# TODO: Failed to generate file content due to timeout\n# Error: {e}\n"

        # Clean the generated content
        new_file_content = clean_file_content(new_file_content)

        # Write the file
        with open(file_path, "w", encoding="utf-8") as file:
            file.write(new_file_content)
            file.flush()

        print(f" Created new file: {file_path} ({len(new_file_content)} chars)")

    else:
        # Get the diffs
        try:
            with open(file_path, "r", encoding="utf-8") as file:
                file_content = file.read()
        except UnicodeDecodeError:
            try:
                with open(file_path, "r", encoding="latin-1") as file:
                    file_content = file.read()
                print(f"[DEVELOPER] Warning: File {file_path} is not UTF-8 encoded, read with latin-1")
            except Exception as e:
                print(f"[DEVELOPER] Error: Cannot read file {file_path} as text: {e}")
                return  # Skip processing this file
        except Exception as e:
            print(f"[DEVELOPER] Unexpected error reading file {file_path}: {e}")
            return  # Skip processing this file

        # add line numbers
        lines = []
        for i, line in enumerate(file_content.splitlines(), start=1):
            lines.append(f"{i}| {line}")
        file_content = "\n".join(lines)

        payload = {
            "task": current_atomic_task.atomic_task,
            "additional_context": current_atomic_task.additional_context,
            "research": convert_tools_messages_to_ai_and_human(state.atomic_implementation_research),
            "file_path": file_path,
            "file_content": file_content,
            "output_format": JsonOutputParser(pydantic_object=Diffs).get_format_instructions()
        }

        try:
            diffs_tasks = invoke_with_backoff(extract_diff_runnable, payload)
        except (APITimeoutError, httpx.ConnectTimeout) as e:
            print(f"[LLM] Diff extraction timeout for {file_path}; skipping modifications.")
            return  # Skip modifications if we can't get diffs
        # Find all content between <code_change_request> and </code_change_request>
        blocks = re.findall(
            r"<code_change_request>(.*?)</code_change_request>", diffs_tasks, re.DOTALL
        )

        for block in blocks:
            # Use regex to extract the original code snippet and the task description.
            # The re.DOTALL flag allows the dot (.) to match newline characters.
            match = re.search(
                r"original_code_snippet:\s*(.*?)\s*edit_code_snippet:\s*(.*)",
                block,
                re.DOTALL,
            )
            if match:
                try:
                    with open(file_path, "r", encoding="utf-8") as f:
                        file_content = f.read()
                except UnicodeDecodeError:
                    try:
                        with open(file_path, "r", encoding="latin-1") as f:
                            file_content = f.read()
                        print(f"[DEVELOPER] Warning: File {file_path} is not UTF-8 encoded, read with latin-1")
                    except Exception as e:
                        print(f"[DEVELOPER] Error: Cannot read file {file_path} as text: {e}")
                        continue  # Skip this diff block
                except Exception as e:
                    print(f"[DEVELOPER] Unexpected error reading file {file_path}: {e}")
                    continue  # Skip this diff block

                original_code = match.group(1).strip()
                edited_code = match.group(2).strip()
                orig_lines = original_code.splitlines()
                
                # Check if we have valid lines to work with
                if not orig_lines:
                    print(f"Warning: Empty original code snippet for file {file_path}")
                    continue
                    
                # Extract line numbers safely
                try:
                    first_line = int(orig_lines[0].split("|")[0].strip())
                    last_line = int(orig_lines[-1].split("|")[0].strip())
                except (IndexError, ValueError) as e:
                    print(f"Warning: Could not parse line numbers from diff: {e}")
                    continue
                    
                new_content = file_content.splitlines()
                new_content = (
                    new_content[: first_line - 1]
                    + edited_code.splitlines()
                    + new_content[last_line:]
                )
                with open(file_path, "w", encoding="utf-8") as f:
                    f.write("\n".join(new_content))
                    f.flush()

# Create tool node
research_tool_node = ToolNode(search_tools + codemap_tools, messages_key="atomic_implementation_research")

# Create the workflow graph
workflow = StateGraph(SoftwareDeveloperState)

# Add nodes
workflow.add_node("start_implementing", start_implementing)
workflow.add_node("prepare_for_implementation", prepare_for_implementation)
workflow.add_node("proceed_to_next_atomic_task", proceed_to_next_atomic_task)
workflow.add_node("get_clear_implementation_plan_for_atomic_task", get_clear_implementation_plan_for_atomic_task)
workflow.add_node("research_tool_node", research_tool_node)
workflow.add_node("creating_diffs_for_task", creating_diffs_for_task)

# Add edges
# Reset the system and load the file from the context of atomic task (if not new file)
workflow.add_edge(START, "start_implementing")
# Read file content and reset previous implementation research
workflow.add_edge("start_implementing", "prepare_for_implementation")
# Go to research about how to implement the atomic task
workflow.add_edge("prepare_for_implementation", "get_clear_implementation_plan_for_atomic_task")
# Check if research is done or we should continue research
workflow.add_conditional_edges(
    "get_clear_implementation_plan_for_atomic_task",
    should_continue_implementation_research,
    {
        "should_continue_research": "research_tool_node",
        "implement_plan": "creating_diffs_for_task"
    }
)
# Go back from executing a research tool to research about implementation
workflow.add_edge("research_tool_node", "get_clear_implementation_plan_for_atomic_task")
# After the research lets apply the diffs
workflow.add_edge("creating_diffs_for_task", "proceed_to_next_atomic_task")
# If next atomic task exists rest and go back to research if not end as everything was implemented
workflow.add_conditional_edges(
    "proceed_to_next_atomic_task",
    is_implementation_complete,
    {
        "continue": "prepare_for_implementation",
        END: END
    }
)

# Initialize Redis checkpointer
# Use localhost Redis server (default: redis://localhost:6379/0)
# Make sure Redis server is running before using this
try:
    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    checkpointer = RedisSaver(redis_url=redis_url)
    print(f"[DEVELOPER] Redis checkpointer initialized at {redis_url}")
except Exception as e:
    print(f"[DEVELOPER] Warning: Could not initialize Redis checkpointer: {e}")
    print("[DEVELOPER] Continuing without checkpointing...")
    checkpointer = None

# Compile the workflow with checkpointer if available
if checkpointer:
    swe_developer = workflow.compile(checkpointer=checkpointer).with_config({"tags": ["developer-agent-v3"]})
else:
    swe_developer = workflow.compile().with_config({"tags": ["developer-agent-v3"]})