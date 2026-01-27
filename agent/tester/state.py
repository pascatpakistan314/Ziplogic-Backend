"""Tester State - Enhanced to support full test pipeline"""
from typing import Annotated, Optional
from pydantic import BaseModel, Field
from langchain_core.messages import AnyMessage
from langgraph.graph import add_messages
from agent.common.entities import ImplementationPlan

class TestCase(BaseModel):
    """Represents a single test case"""
    test_name: str
    test_type: str = Field("unit", description="unit|integration|e2e")
    file_path: str
    function_under_test: str | None = None
    test_code: str

class TestResult(BaseModel):
    """Represents the result of running a test case"""
    test_name: str
    passed: bool
    execution_time: float | None = None
    error_message: str | None = None

class SoftwareTesterState(BaseModel):
    """State for tester - enhanced to support full test pipeline"""
    implementation_plan: Optional[ImplementationPlan] = Field(None, description="The implementation plan to test")
    test_plan: Optional[str] = Field(None, description="High-level test plan / strategy")
    test_strategy: Optional[str] = Field(None, description="Structured test strategy output")
    test_cases: Optional[str] = Field(None, description="Generated test cases")
    test_results: Optional[str] = Field(None, description="Raw execution results")
    coverage_report: Optional[str] = Field(None, description="Coverage/quality analysis report")
    testing_scratchpad: Annotated[list[AnyMessage], add_messages] = Field(default_factory=list, description="Testing research messages")