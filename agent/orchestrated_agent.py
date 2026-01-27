"""
Sequential SWE Agent Workflow: Architect → Developer → Reviewer
"""

import os
from agent.architect.graph_enhanced import swe_architect
from agent.common.entities import ImplementationPlan
from agent.developer.graph import swe_developer
from agent.developer.helpers import split_plan_by_task
from agent.reviewer.graph import swe_reviewer
from pydantic import BaseModel, Field
from langchain_core.messages import AnyMessage
from langgraph.graph import add_messages, StateGraph, START, END
from redis import asyncio as aioredis # Import the async redis client
from typing import Annotated, Optional, List


class AgentState(BaseModel):
    task_description: str = Field(..., description="The main task to accomplish")
    implementation_research_scratchpad: Annotated[list[AnyMessage], add_messages] = Field(default_factory=list)
    implementation_plan: Optional[ImplementationPlan] = Field(None, description="The implementation plan to be executed")

    # Shared workspace/project path (set from frontend via simple_api.py)
    workspace_path: str = Field(default="./workspace_repo", description="Path to workspace/project directory")

    # Reviewer state fields
    files_to_review: List[str] = Field(default_factory=list, description="Files to review (auto-detected if empty)")
    review_scratchpad: Annotated[list[AnyMessage], add_messages] = Field(default_factory=list, description="Reviewer messages")
    proposed_edits: List = Field(default_factory=list, description="Proposed fixes from reviewer")
    applied_edits: List = Field(default_factory=list, description="Applied edits from reviewer")
    auto_apply: bool = Field(default=True, description="Auto-apply reviewer fixes")


def architect_wrapper(state: AgentState):
    """Wrap architect invocation to map AgentState → ArchitectState → AgentState"""
    architect_result = swe_architect.invoke({
        "task_description": state.task_description,
        "implementation_research_scratchpad": state.implementation_research_scratchpad,
        "workspace_path": state.workspace_path
    })

    # Map architect output back to AgentState
    return {
        "implementation_plan": architect_result.get("implementation_plan"),
        "implementation_research_scratchpad": architect_result.get("implementation_research_scratchpad", [])
    }


def developer_wrapper(state: AgentState):
    """
    Wrap developer invocation to map AgentState → DeveloperState → AgentState.

    NEW: Splits the full plan into small batches and invokes
    the developer graph ONCE PER BATCH to prevent recursion issues.
    """
    # Validate that we have an implementation plan
    if state.implementation_plan is None:
        raise ValueError("Developer requires implementation_plan from architect, but it is None")

    # Split the giant plan into a list of small, manageable plans
    batches = split_plan_by_task(state.implementation_plan)

    print(f"[DEVELOPER WRAPPER] Splitting {len(state.implementation_plan.tasks)} tasks into {len(batches)} batches.")

    # Loop over each small batch and run the developer graph
    for i, batch_plan in enumerate(batches):
        print(f"--- Running Developer Batch {i+1}/{len(batches)} ---")

        # Calculate the dynamic recursion limit for *this batch only*
        total_atomic = sum(len(t.atomic_tasks) for t in batch_plan.tasks)
        rec_limit = max(60, total_atomic * 8 + 10)  # Formula: safe buffer per atomic task

        try:
            _ = swe_developer.invoke(
                {
                    "implementation_plan": batch_plan,
                    "atomic_implementation_research": [],  # Start each batch clean
                    "workspace_path": state.workspace_path,
                },
                config={"recursion_limit": rec_limit},
            )
            print(f"--- Batch {i+1}/{len(batches)} Succeeded ---")

        except Exception as e:
            # If one batch fails, log it and decide to stop
            print(f"--- FATAL: Batch {i+1}/{len(batches)} FAILED ---")
            print(f"Error: {e}")
            # This is where you would add checkpointing (future enhancement) to resume later
            raise ValueError(f"Developer failed on batch {i+1}/{len(batches)}. See logs above for details.")

    # Developer doesn't need to return anything to AgentState
    # Files are written directly to disk
    return {}


def reviewer_wrapper(state: AgentState):
    """Wrap reviewer invocation to map AgentState → ReviewerState → AgentState"""
    reviewer_result = swe_reviewer.invoke({
        "files_to_review": state.files_to_review,
        "review_scratchpad": state.review_scratchpad,
        "workspace_path": state.workspace_path,
        "auto_apply": state.auto_apply
    })

    # Map reviewer output back to AgentState
    return {
        "review_scratchpad": reviewer_result.get("review_scratchpad", []),
        "proposed_edits": reviewer_result.get("proposed_edits", []),
        "applied_edits": reviewer_result.get("applied_edits", [])
    }


def should_continue_to_developer(state: AgentState):
    """
    Check if the architect successfully created an implementation plan.
    If not, end the graph to prevent the developer from crashing.
    """
    if state.implementation_plan is None:
        print("Architect failed to produce a valid implementation plan. Ending graph.")
        return "end_graph"
    return "continue"


def create_workflow_graph():
    """Create and return the workflow graph: Architect → Developer → Reviewer"""
    graph_builder = StateGraph(AgentState)

    # Add nodes with wrapper functions
    graph_builder.add_node("swe_architect", architect_wrapper)
    graph_builder.add_node("swe_developer", developer_wrapper)
    graph_builder.add_node("swe_reviewer", reviewer_wrapper)

    # Add edges for the workflow
    graph_builder.add_edge(START, "swe_architect")

    # Add a conditional edge to check for a valid plan
    graph_builder.add_conditional_edges(
        "swe_architect",
        should_continue_to_developer,
        {
            "continue": "swe_developer",
            "end_graph": END,
        }
    )

    graph_builder.add_edge("swe_developer", "swe_reviewer")
    graph_builder.add_edge("swe_reviewer", END)

    return graph_builder



# Initialize checkpointer for the orchestrated workflow
# Try Redis first, fall back to MemorySaver if Redis is not available
orchestrated_checkpointer = None

# Check if Redis should be used (only if REDIS_URL is explicitly set or Redis is running)
redis_url = os.environ.get("REDIS_URL")
use_redis = redis_url is not None

if use_redis:
    try:
        from langgraph.checkpoint.redis import AsyncRedisSaver
        from redis import asyncio as aioredis

        # AsyncRedisSaver requires an async redis client instance
        redis_client = aioredis.from_url(redis_url)

        orchestrated_checkpointer = AsyncRedisSaver(redis_client=redis_client)
        print(f"[ORCHESTRATED_AGENT] AsyncRedisSaver initialized successfully at {redis_url}")
    except Exception as e:
        print(f"[ORCHESTRATED_AGENT] Warning: Could not initialize Redis checkpointer: {e}")
        orchestrated_checkpointer = None

# Fall back to MemorySaver if Redis is not available
if orchestrated_checkpointer is None:
    from langgraph.checkpoint.memory import MemorySaver
    orchestrated_checkpointer = MemorySaver()
    print("[ORCHESTRATED_AGENT] Using MemorySaver for checkpointing")

# Compile the workflow with checkpointer if available
if orchestrated_checkpointer:
    swe_agent = create_workflow_graph().compile(checkpointer=orchestrated_checkpointer).with_config({
        "tags": ["agent-v1"]
    })
else:
    swe_agent = create_workflow_graph().compile().with_config({
        "tags": ["agent-v1"]
    })

# Alias for compatibility with simple_api.py
orchestrated_swe_agent_compatible = swe_agent