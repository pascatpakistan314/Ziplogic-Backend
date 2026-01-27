# agent/architect/graph.py
"""
Architect Agent Graph with Resilient LLM Configuration
- Single-model chain: ChatOpenAI(model="gpt-4o") with timeout and retries
- NO with_fallbacks, NO minimal/fake ImplementationPlan
- All reads/writes are FORCED to the project's folder returned by get_project_directory(state)
- Defensive conversion of tool messages to avoid None/attribute errors
"""

import json
import os
import time
from typing import List, TypedDict, Optional

from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, AIMessage, AnyMessage
from langgraph.constants import END, START
from langgraph.prebuilt import ToolNode
from pydantic import BaseModel, Field
from langgraph.graph import StateGraph

from agent.architect.state import SoftwareArchitectState
from agent.tools.search import search_tools
from agent.tools.codemap import codemap_tools
from agent.tools.write import get_files_structure
from helpers.prompts import markdown_to_prompt_template
from agent.common.entities import ImplementationPlan
from openai import APITimeoutError
import httpx

# Path utils (we will use get_project_directory and hard-force file paths into it)
from agent.utils.paths import get_project_directory


# ----------------------------
# Pydantic I/O Schemas
# ----------------------------
class ResearchStep(BaseModel):
    reasoning: str = Field(
        description="Why the research step is needed and how it helps the task"
    )
    hypothesis: str = Field(description="The hypothesis that needs to be researched")


class ResearchEvaluation(BaseModel):
    reasoning: str = Field(description="Why the research step is valid or not (1–3 sentences)")
    is_valid: bool = Field(description="Whether the research step is valid")


# ----------------------------
# Prompts (original pattern)
# ----------------------------
plan_next_step_prompt = markdown_to_prompt_template(
    "agent/architect/prompts/plan_next_step_prompt.md"
)
check_research_prompt = markdown_to_prompt_template(
    "agent/architect/prompts/check_research_already_explored.md"
)
conduct_research_prompt = markdown_to_prompt_template(
    "agent/architect/prompts/conduct_research_plan_prompt.md"
)
extract_implementation_prompt = markdown_to_prompt_template(
    "agent/architect/prompts/extract_implementation_plan.md"
)

# ----------------------------
# Resilient LLM Configuration
# ----------------------------
def build_architect_llm():
    """Build a ChatOpenAI instance with resilient settings for architect tasks."""
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

# ----------------------------
# Runnables (with resilient LLM)
# ----------------------------
plan_next_step_runnable = (
    plan_next_step_prompt
    | build_architect_llm().with_structured_output(ResearchStep)
)

check_research_runnable = (
    check_research_prompt
    | build_architect_llm().with_structured_output(ResearchEvaluation)
)

conduct_research_runnable = (
    conduct_research_prompt
    | build_architect_llm().bind_tools(search_tools + codemap_tools)
)

# Use .with_structured_output for robust parsing directly into ImplementationPlan
extract_implementation_runnable = (
    extract_implementation_prompt
    | build_architect_llm().with_structured_output(ImplementationPlan)
)

# Single tool node with the architect scratchpad key
tool_node = ToolNode(search_tools + codemap_tools, messages_key="implementation_research_scratchpad")


# ----------------------------
# Helpers
# ----------------------------
def _safe_msg_type(m) -> str:
    return getattr(m, "type", None) or getattr(m, "_type", None) or ""

def _has_tool_calls(m) -> bool:
    return hasattr(m, "tool_calls") and bool(getattr(m, "tool_calls"))

def _force_path_into_project(path: str, project_dir: str) -> str:
    """
    Guarantee the resulting path is under the given project_dir.
    - Absolute paths outside project_dir are rewritten into project_dir preserving filename.
    - Relative paths are resolved under project_dir.
    """
    path = str(path or "").strip()
    project_dir = os.path.abspath(project_dir)

    if not path:
        # default to project root (caller should still ensure a FILE path later)
        return project_dir

    if os.path.isabs(path):
        # If already inside project_dir, keep; else rewrite to project_dir/<basename>
        abs_path = os.path.abspath(path)
        try:
            common = os.path.commonpath([abs_path, project_dir])
        except Exception:
            common = ""
        if common == project_dir:
            return abs_path
        return os.path.join(project_dir, os.path.basename(abs_path))

    # relative → join into project
    return os.path.join(project_dir, path)

def convert_tools_messages_to_ai_and_human(implementation_research_scratchpad: List[AnyMessage]):
    """
    Convert tool messages to AI/Human messages so they can be fed into prompts
    without structured tool-call objects. Defensive against None/empty shapes.
    """
    if not implementation_research_scratchpad:
        return []

    messages: List[AnyMessage] = []
    for message in implementation_research_scratchpad:
        mtype = _safe_msg_type(message)
        if mtype == "ai":
            if _has_tool_calls(message):
                tc = message.tool_calls[0]
                tool_name = tc.get("name", "unknown_tool")
                tool_args = json.dumps(tc.get("args", {}))
                messages.append(AIMessage(content=f"I want to call the tool {tool_name} with the following arguments: {tool_args}"))
            else:
                messages.append(AIMessage(content=str(getattr(message, "content", ""))))
        elif mtype == "tool":
            tname = getattr(message, "name", "unknown_tool")
            tcontent = str(getattr(message, "content", ""))
            messages.append(HumanMessage(content=f"When executing Tool {tname}\nThe result was: {tcontent}"))
        else:
            messages.append(HumanMessage(content=str(getattr(message, "content", ""))))
    return messages


# ----------------------------
# Node Functions
# ----------------------------
class ComeUpWithResearchNextStepOutput(TypedDict):
    research_next_step: str
    implementation_research_scratchpad: List[AnyMessage]

def come_up_with_research_next_step(state: SoftwareArchitectState) -> ComeUpWithResearchNextStepOutput:
    """
    Generate the next research step using the specific PROJECT directory (not workspace root).
    """
    project_dir = str(get_project_directory(state))
    response = plan_next_step_runnable.invoke({
        "task_description": state.task_description,
        "workspace_path": state.workspace_path or project_dir,
        "implementation_research_scratchpad": state.implementation_research_scratchpad,
        "codebase_structure": get_files_structure.invoke({"directory": project_dir}),
    })
    hypothesis = response.hypothesis or "Clarify next concrete research step for the task."
    reasoning = response.reasoning or "No additional reasoning provided."

    return {
        "research_next_step": hypothesis,
        "implementation_research_scratchpad": [
            AIMessage(content=f"My next thing I need to check is: {hypothesis}\nThis is why I think it is useful: {reasoning}")
        ],
    }


class CheckResearchStepOutput(TypedDict):
    is_valid_research_step: bool
    implementation_research_scratchpad: List[AnyMessage]

def check_research_step(state: SoftwareArchitectState) -> CheckResearchStepOutput:
    project_dir = str(get_project_directory(state))
    response = check_research_runnable.invoke({
        "task_description": state.task_description,
        "implementation_research_scratchpad": state.implementation_research_scratchpad
    })
    if not response.is_valid:
        return {
            "is_valid_research_step": False,
            "implementation_research_scratchpad": [
                HumanMessage(content="The research path is not valid, here is why: " + (response.reasoning or ""))
            ],
        }
    return {
        "is_valid_research_step": True,
        "implementation_research_scratchpad": [
            HumanMessage(content="The research path is valid, start conducting the research")
        ],
    }

def conduct_research(state: SoftwareArchitectState):
    """
    Conduct research with tools; all structure is read from the PROJECT directory.
    """
    project_dir = str(get_project_directory(state))
    response = conduct_research_runnable.invoke({
        "task_description": state.task_description,
        "workspace_path": state.workspace_path or project_dir,
        "research_next_step": state.research_next_step or "",
        "implementation_research_scratchpad": state.implementation_research_scratchpad,
        "codebase_structure": get_files_structure.invoke({"directory": project_dir}),
    })
    return {"implementation_research_scratchpad": [response]}

def extract_implementation_plan(state: SoftwareArchitectState):
    """
    Convert research findings into an ImplementationPlan (no fallback).
    Additionally, FORCE every task.file_path to live inside the PROJECT directory.
    """
    project_dir = str(get_project_directory(state))
    research_findings = convert_tools_messages_to_ai_and_human(state.implementation_research_scratchpad)

    plan = extract_implementation_runnable.invoke({
        "task_description": state.task_description,
        "workspace_path": state.workspace_path or project_dir,
        "research_findings": research_findings,
        "codebase_structure": get_files_structure.invoke({"directory": project_dir}),
    })

    # Force all file paths into the PROJECT directory and ensure they look like file targets
    for t in getattr(plan, "tasks", []) or []:
        coerced = _force_path_into_project(t.file_path, project_dir)
        t.file_path = coerced
        # Basic validation: the file path should not be the project directory itself
        if os.path.abspath(t.file_path) == os.path.abspath(project_dir):
            raise ValueError(f"Invalid file_path for task '{t.logical_task}': got project dir; must be a file path.")
    return {"implementation_plan": plan}


# ----------------------------
# Routers
# ----------------------------
def should_call_tool(state: SoftwareArchitectState):
    if not state.implementation_research_scratchpad:
        return "implement_plan"
    last_message = state.implementation_research_scratchpad[-1]
    if _has_tool_calls(last_message) or _safe_msg_type(last_message) == "tool":
        return "should_call_tool"
    return "implement_plan"

def should_conduct_research(state: SoftwareArchitectState):
    return "plan_is_valid" if state.is_valid_research_step else "plan_is_not_valid"


# ----------------------------
# Graph Wiring
# ----------------------------
class SoftwareArchitectInput(TypedDict):
    task_description: str
    implementation_research_scratchpad: List[AnyMessage]

class SoftwareArchitectOutput(TypedDict):
    implementation_plan: Optional[ImplementationPlan]

workflow = StateGraph(
    SoftwareArchitectState,
    input=SoftwareArchitectInput,
    output=SoftwareArchitectOutput,
)

workflow.add_node("come_up_with_research_next_step", come_up_with_research_next_step)
workflow.add_node("check_research_step", check_research_step)
workflow.add_node("conduct_research", conduct_research)
workflow.add_node("extract_implementation_plan", extract_implementation_plan)
workflow.add_node("tools", tool_node)

workflow.add_edge(START, "come_up_with_research_next_step")
workflow.add_edge("come_up_with_research_next_step", "check_research_step")

workflow.add_conditional_edges(
    "check_research_step",
    should_conduct_research,
    {
        "plan_is_valid": "conduct_research",
        "plan_is_not_valid": "come_up_with_research_next_step",
    },
)

workflow.add_conditional_edges(
    "conduct_research",
    should_call_tool,
    {
        "should_call_tool": "tools",
        "implement_plan": "extract_implementation_plan",
    },
)

workflow.add_edge("tools", "conduct_research")
workflow.add_edge("extract_implementation_plan", END)

swe_architect = workflow.compile().with_config({"tags": ["research-agent-gpt5"]})
