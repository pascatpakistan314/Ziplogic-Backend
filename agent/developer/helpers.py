"""
Helper functions for the developer agent to manage state and prevent recursion issues.
"""
from typing import List
from agent.common.entities import ImplementationPlan
from agent.developer.state import SoftwareDeveloperState


def get_atomic_task_key(state: SoftwareDeveloperState) -> str:
    """
    Generate a unique key for the current atomic task.

    Args:
        state: The current developer state

    Returns:
        A string key in the format "task_idx:atomic_idx"
    """
    ct = state.current_task_idx
    ca = state.current_atomic_task_idx
    return f"{ct}:{ca}"


def bump_step_counter(state: SoftwareDeveloperState) -> int:
    """
    Increment and return the step counter for the current atomic task.

    Args:
        state: The current developer state

    Returns:
        The new step count for this atomic task
    """
    key = get_atomic_task_key(state)
    current_count = state.step_counter.get(key, 0)
    new_count = current_count + 1
    state.step_counter[key] = new_count
    return new_count


def get_step_count(state: SoftwareDeveloperState) -> int:
    """
    Get the current step count for the current atomic task.

    Args:
        state: The current developer state

    Returns:
        The current step count (0 if not tracked yet)
    """
    key = get_atomic_task_key(state)
    return state.step_counter.get(key, 0)


def split_plan_by_task(plan: ImplementationPlan, max_atomic_per_batch: int = 10) -> List[ImplementationPlan]:
    """
    Splits a plan into a list of smaller plans ('batches').

    This logic groups all atomic tasks for a single main task together.
    It's safer than splitting one file's work across multiple batches.

    Args:
        plan: The full implementation plan to split
        max_atomic_per_batch: Maximum atomic tasks per batch (currently unused,
                              but available for future granular splitting)

    Returns:
        A list of smaller ImplementationPlan objects, one per main task
    """
    batches = []

    # Group all atomic tasks for each main task together
    for task in plan.tasks:
        # Create a new, separate plan for this single task
        # This keeps all atomic tasks for one file together
        partial_plan = plan.model_copy(deep=True)
        partial_plan.tasks = [task]  # Only this one main task
        batches.append(partial_plan)

    return batches
