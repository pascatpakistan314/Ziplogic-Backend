from typing import Annotated, Optional
from pydantic import BaseModel, Field
from langchain_core.messages import AnyMessage
from langgraph.graph import add_messages
from agent.common.entities import ImplementationPlan

# Check Pydantic version and import appropriate config
try:
    from pydantic import ConfigDict
    V2 = True
except ImportError:
    V2 = False

class SoftwareArchitectState(BaseModel):
    """State for the software architect agent that preserves all fields"""
    
    # Configure to allow extra fields (prevents dropping unknown keys)
    if V2:
        model_config = ConfigDict(extra="allow")  # Pydantic v2: keep unknown keys
    else:
        class Config:
            extra = "allow"  # Pydantic v1: keep unknown keys
    
    # Core fields
    task_description: str = Field(..., description="The user's high-level task/goal")
    research_next_step: Optional[str] = Field(None, description="The next research step to be conducted")
    implementation_plan: Optional[ImplementationPlan] = Field(None, description="The implementation plan to be executed")
    implementation_research_scratchpad: Annotated[list[AnyMessage], add_messages] = Field(
        default_factory=list,
        description="The scratchpad for implementation research"
    )
    is_valid_research_step: Optional[bool] = Field(None, description="Whether the research step is valid")
    workspace_path: Optional[str] = Field(None, description="The workspace directory path for file operations")