"""Reviewer State - Simple Claude Code style"""

from typing import Annotated, List, Optional
from pydantic import BaseModel, Field
from langchain_core.messages import AnyMessage
from langgraph.graph import add_messages


class ProposedEdit(BaseModel):
    """Represents a proposed fix"""
    edit_type: str  # replace_range|search_replace|insert_lines
    file_path: str
    start_line: Optional[int] = None
    end_line: Optional[int] = None
    search_pattern: Optional[str] = None
    replacement: Optional[str] = None
    new_content: Optional[str] = None
    after_line: Optional[int] = None
    reasoning: str


class AppliedEdit(BaseModel):
    """Record of an applied edit"""
    edit: ProposedEdit
    success: bool
    message: str


class CodeReviewerState(BaseModel):
    """State for code reviewer agent"""
    # Inputs
    files_to_review: List[str] = Field(default_factory=list, description="Files to review")
    workspace_path: Optional[str] = Field(None, description="Workspace/project directory path")

    # Scratchpad for finding issues with tools
    review_scratchpad: Annotated[list[AnyMessage], add_messages] = Field(
        default_factory=list,
        description="Messages while finding issues"
    )

    # Fix proposals
    proposed_edits: List[ProposedEdit] = Field(default_factory=list, description="Proposed fixes")

    # Applied fixes
    applied_edits: List[AppliedEdit] = Field(default_factory=list, description="Applied edits log")

    # Config
    auto_apply: bool = Field(default=True, description="Auto-apply fixes or just propose")
