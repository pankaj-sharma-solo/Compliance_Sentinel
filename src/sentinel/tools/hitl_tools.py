"""
Human-in-the-Loop tools using LangGraph interrupt().

interrupt() PAUSES the graph at the exact call site.
State is saved to the checkpointer (SqliteSaver).
The graph resumes when the human POSTs a decision via the API.

Three interrupt points in Compliance Sentinel:
  1. rule_commit     — before writing a decomposed rule to MySQL
  2. remediation_execute — before executing a SQL remediation
  3. policy_gap_confirm  — before flagging a gap in a policy review
"""
from typing import Annotated
from langchain_core.tools import InjectedToolCallId, tool
from langchain_core.messages import ToolMessage
from langgraph.types import Command, interrupt
from langgraph.prebuilt import InjectedState
from sentinel.states.orchestrator_state import OrchestratorState
import json


@tool
def request_rule_commit_approval(
    rule_data: dict,
    similarity_score: float,
    existing_rule_id: str,
    tool_call_id: Annotated[str, InjectedToolCallId],
    state: Annotated[OrchestratorState, InjectedState],
) -> Command:
    """
    HITL Gate 1 — Rule commit approval.

    Called before writing a newly decomposed rule (or superseding an existing one)
    to MySQL. The human sees the rule text, violation_conditions, and similarity
    score against the existing rule before it becomes authoritative.

    Graph PAUSES here. Resumes when human POSTs approve/reject/modify.
    """
    review_payload = {
        "review_type": "rule_commit",
        "title": f"Review Rule Before Commit: {rule_data.get('rule_id')}",
        "description": (
            f"Similarity to existing rule '{existing_rule_id}': {similarity_score:.3f}. "
            f"Action: {'supersede existing' if existing_rule_id else 'insert new'}. "
            f"Review the violation_conditions before this rule becomes active for scanning."
        ),
        "data": rule_data,
        "options": ["approve", "reject", "modify"],
    }

    # Save review request to state so API can surface it
    state_update = Command(
        update={
            "human_review_request": review_payload,
            "messages": [
                ToolMessage(
                    f"⏸ AWAITING HUMAN REVIEW: Rule commit for {rule_data.get('rule_id')}",
                    tool_call_id=tool_call_id,
                )
            ],
        }
    )

    # ── Graph pauses here ──
    # State is checkpointed. API returns to caller.
    # Graph resumes when human calls /orchestrator/resume with thread_id + decision.
    decision = interrupt(review_payload)

    # After resume — decision is "approve" | "reject" | {"action": "modify", "data": {...}}
    if isinstance(decision, dict) and decision.get("action") == "modify":
        modified_data = decision.get("data", rule_data)
        return Command(
            update={
                "human_decision": "modify",
                "human_feedback": json.dumps(modified_data),
                "messages": [ToolMessage(f"Rule modified by human, proceeding with changes", tool_call_id=tool_call_id)],
            }
        )
    elif decision == "approve":
        return Command(
            update={
                "human_decision": "approve",
                "messages": [ToolMessage(f"✓ Rule approved by human", tool_call_id=tool_call_id)],
            }
        )
    else:
        return Command(
            update={
                "human_decision": "reject",
                "errors": state.get("errors", []) + [f"Rule {rule_data.get('rule_id')} rejected by human"],
                "messages": [ToolMessage(f"✗ Rule rejected by human", tool_call_id=tool_call_id)],
            }
        )


@tool
def request_remediation_approval(
    violation_id: int,
    remediation_plan: dict,
    tool_call_id: Annotated[str, InjectedToolCallId],
    state: Annotated[OrchestratorState, InjectedState],
) -> Command:
    """
    HITL Gate 2 — Remediation execution approval.

    Called before executing any remediation SQL or schema change.
    The human sees the exact SQL that will run, what violation it fixes,
    and the risk level before anything touches the target database.

    Graph PAUSES here.
    """
    review_payload = {
        "review_type": "remediation_execute",
        "title": f"Approve Remediation for Violation #{violation_id}",
        "description": (
            f"The following change will be applied to the target database. "
            f"Risk: {remediation_plan.get('risk_level', 'MEDIUM')}. "
            f"This action is irreversible without a rollback plan."
        ),
        "data": remediation_plan,
        "options": ["approve", "reject"],
    }

    decision = interrupt(review_payload)

    if decision == "approve":
        return Command(
            update={
                "human_decision": "approve",
                "remediation_plan": {**remediation_plan, "approved": True},
                "messages": [ToolMessage(f"✓ Remediation approved", tool_call_id=tool_call_id)],
            }
        )
    else:
        return Command(
            update={
                "human_decision": "reject",
                "messages": [ToolMessage(f"✗ Remediation rejected — violation stays OPEN", tool_call_id=tool_call_id)],
            }
        )


@tool
def request_policy_gap_confirmation(
    gap_summary: dict,
    tool_call_id: Annotated[str, InjectedToolCallId],
    state: Annotated[OrchestratorState, InjectedState],
) -> Command:
    """
    HITL Gate 3 — Policy gap confirmation.

    During a policy review workflow, before flagging a compliance gap
    as a formal finding. The human validates whether the agent's
    assessment is accurate before it's written to the audit log.

    Graph PAUSES here.
    """
    review_payload = {
        "review_type": "policy_gap_confirm",
        "title": "Confirm Compliance Gap Finding",
        "description": (
            f"The agent has identified a potential compliance gap. "
            f"Review before this is recorded as a formal finding."
        ),
        "data": gap_summary,
        "options": ["confirm_gap", "dismiss", "needs_more_info"],
    }

    decision = interrupt(review_payload)

    return Command(
        update={
            "human_decision": str(decision),
            "messages": [ToolMessage(f"Gap assessment: {decision}", tool_call_id=tool_call_id)],
        }
    )
