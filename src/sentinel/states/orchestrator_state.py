"""
State for the Compliance Orchestrator Deep Agent.
Extends the base states with todo tracking, HITL fields,
and cross-workflow context — mirrors DeepAgentState from reference.
"""
from typing import TypedDict, Annotated, Optional, Any
from langgraph.graph.message import add_messages
from pydantic import BaseModel


class Todo(BaseModel):
    content: str
    status: str = "pending"  # pending | in_progress | completed | blocked
    id: Optional[str] = None


class HumanReviewRequest(BaseModel):
    """Payload surfaced to the human when interrupt() fires."""
    review_type: str  # rule_commit | remediation_execute | policy_gap_confirm
    title: str
    description: str
    data: dict  # the object awaiting approval
    thread_id: str
    options: list[str] = ["approve", "reject", "modify"]


class OrchestratorState(TypedDict):
    """
    Full state for the Compliance Orchestrator.
    todos + files mirror the Deep Agent pattern from reference codebase.
    human_review_request is set before interrupt() fires.
    human_decision is populated on resume.
    """
    messages: Annotated[list, add_messages]
    todos: list[dict]  # [{content, status, id}]
    files: dict[str, str]  # virtual filesystem — write_file/read_file

    # HITL fields
    human_review_request: Optional[dict]  # HumanReviewRequest payload
    human_decision: Optional[str]  # "approve" | "reject" | "modify"
    human_feedback: Optional[str]  # free-text from human on modify/reject

    # Workflow context
    workflow_type: Optional[str]  # policy_review | remediation | conversational
    db_connection_id: Optional[int]
    target_rule_ids: list[str]
    scan_results: list[dict]
    violations_context: list[dict]
    remediation_plan: Optional[dict]
    errors: list[str]
    langgraph_checkpoint_id: Optional[str]
