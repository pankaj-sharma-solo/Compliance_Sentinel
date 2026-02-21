"""
Orchestrator API routes.

POST /orchestrator/run          — start a workflow (returns immediately if interrupted)
POST /orchestrator/resume       — human submits decision, graph continues
GET  /orchestrator/status/{tid} — poll current state / pending review request
GET  /orchestrator/history/{tid}— full message history for a thread

Thread IDs are the HITL handshake — the same thread_id must be used
for run → status → resume → status → ...
"""
import uuid
import logging
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from langgraph.types import Command
from sentinel.database import get_db
from sentinel.checkpointer import get_checkpointer
from sentinel.agents.compliance_orchestrator import compliance_orchestrator
from sentinel.states.orchestrator_state import OrchestratorState
from sentinel.models.audit_log import AuditLog

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/orchestrator", tags=["Orchestrator"])


class RunRequest(BaseModel):
    user_message: str
    workflow_type: str | None = None  # policy_review | remediation | conversational
    db_connection_id: int | None = None
    thread_id: str | None = None  # provide to continue existing thread


class ResumeRequest(BaseModel):
    thread_id: str
    decision: str  # "approve" | "reject" | "confirm_gap" | "dismiss"
    feedback: str | None = None  # free text for "modify" decisions
    modified_data: dict | None = None  # if decision == "modify", send back changed data


def _get_config(thread_id: str) -> dict:
    """LangGraph config — thread_id scopes the checkpointer state."""
    return {"configurable": {"thread_id": thread_id}}


@router.post("/run")
async def run_orchestrator(req: RunRequest, db: Session = Depends(get_db)):
    """
    Start or continue an orchestrator workflow.

    If the graph hits interrupt() (HITL gate), it pauses and this endpoint
    returns immediately with status="interrupted" + the review payload.
    The frontend shows the review UI. Human submits to /resume.
    """
    thread_id = req.thread_id or str(uuid.uuid4())
    checkpointer = get_checkpointer()
    graph = compliance_orchestrator.compile(checkpointer=checkpointer)

    initial_state: OrchestratorState = {
        "messages": [{"role": "user", "content": req.user_message}],
        "todos": [],
        "files": {},
        "human_review_request": None,
        "human_decision": None,
        "human_feedback": None,
        "workflow_type": req.workflow_type,
        "db_connection_id": req.db_connection_id,
        "target_rule_ids": [],
        "scan_results": [],
        "violations_context": [],
        "remediation_plan": None,
        "errors": [],
        "langgraph_checkpoint_id": thread_id,
    }

    try:
        result = graph.invoke(initial_state, config=_get_config(thread_id))

        # Check if graph paused at interrupt()
        snapshot = graph.get_state(config=_get_config(thread_id))
        is_interrupted = bool(snapshot.next)  # non-empty means graph has pending nodes

        # Audit log
        audit = AuditLog(
            event_type="ORCHESTRATOR_RUN",
            entity_type="workflow",
            entity_id=thread_id,
            actor="user",
            detail={
                "workflow_type": req.workflow_type,
                "message": req.user_message[:200],
                "interrupted": is_interrupted,
            },
            langgraph_checkpoint_id=thread_id,
        )
        db.add(audit)
        db.commit()

        if is_interrupted:
            # Return the review request so frontend can show approval UI
            review_request = result.get("human_review_request")
            return {
                "thread_id": thread_id,
                "status": "interrupted",
                "pending_review": review_request,
                "todos": result.get("todos", []),
                "message": "Awaiting human review before proceeding",
            }

        # Completed without interruption
        final_message = result["messages"][-1].content if result.get("messages") else ""
        return {
            "thread_id": thread_id,
            "status": "completed",
            "response": final_message,
            "todos": result.get("todos", []),
            "errors": result.get("errors", []),
        }

    except Exception as e:
        logger.error("Orchestrator run failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/resume")
async def resume_orchestrator(req: ResumeRequest, db: Session = Depends(get_db)):
    """
    Resume a paused graph after human review.

    The graph was suspended at interrupt(). Calling this with the thread_id
    resumes from the exact pause point — the interrupt() call returns
    req.decision as its value.
    """
    checkpointer = get_checkpointer()
    graph = compliance_orchestrator.compile(checkpointer=checkpointer)

    # Verify thread exists and is paused
    snapshot = graph.get_state(config=_get_config(req.thread_id))
    if not snapshot or not snapshot.next:
        raise HTTPException(
            status_code=400,
            detail=f"Thread {req.thread_id} is not paused or does not exist"
        )

    # Build decision payload
    if req.decision == "modify" and req.modified_data:
        decision_payload = {"action": "modify", "data": req.modified_data}
    else:
        decision_payload = req.decision

    try:
        # Resume graph — Command(resume=...) feeds the decision back to interrupt()
        result = graph.invoke(
            Command(resume=decision_payload),
            config=_get_config(req.thread_id),
        )

        # Check if paused again (multi-step HITL)
        snapshot_after = graph.get_state(config=_get_config(req.thread_id))
        is_still_interrupted = bool(snapshot_after.next)

        # Audit the human decision
        audit = AuditLog(
            event_type="HUMAN_REVIEW_DECISION",
            entity_type="workflow",
            entity_id=req.thread_id,
            actor="human",
            detail={
                "decision": req.decision,
                "feedback": req.feedback,
                "still_interrupted": is_still_interrupted,
            },
            langgraph_checkpoint_id=req.thread_id,
        )
        db.add(audit)
        db.commit()

        if is_still_interrupted:
            review_request = result.get("human_review_request")
            return {
                "thread_id": req.thread_id,
                "status": "interrupted",
                "pending_review": review_request,
                "todos": result.get("todos", []),
                "message": "Next review gate reached",
            }

        final_message = result["messages"][-1].content if result.get("messages") else ""
        return {
            "thread_id": req.thread_id,
            "status": "completed",
            "response": final_message,
            "todos": result.get("todos", []),
            "errors": result.get("errors", []),
        }

    except Exception as e:
        logger.error("Orchestrator resume failed [%s]: %s", req.thread_id, e)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/status/{thread_id}")
def get_orchestrator_status(thread_id: str):
    """
    Poll the current state of an orchestrator thread.
    Used by the frontend to check if still running / interrupted / completed.
    """
    checkpointer = get_checkpointer()
    graph = compliance_orchestrator.compile(checkpointer=checkpointer)
    snapshot = graph.get_state(config=_get_config(thread_id))

    if not snapshot or not snapshot.values:
        raise HTTPException(status_code=404, detail="Thread not found")

    state = snapshot.values
    is_interrupted = bool(snapshot.next)

    return {
        "thread_id": thread_id,
        "status": "interrupted" if is_interrupted else "completed",
        "pending_review": state.get("human_review_request") if is_interrupted else None,
        "todos": state.get("todos", []),
        "errors": state.get("errors", []),
        "files": list(state.get("files", {}).keys()),
    }


@router.get("/history/{thread_id}")
def get_thread_history(thread_id: str):
    """
    Return full message history for a thread — for the UI chat view.
    """
    checkpointer = get_checkpointer()
    graph = compliance_orchestrator.compile(checkpointer=checkpointer)
    snapshot = graph.get_state(config=_get_config(thread_id))

    if not snapshot or not snapshot.values:
        raise HTTPException(status_code=404, detail="Thread not found")

    messages = snapshot.values.get("messages", [])
    return {
        "thread_id": thread_id,
        "messages": [
            {
                "role": getattr(m, "type", "unknown"),
                "content": m.content if hasattr(m, "content") else str(m),
            }
            for m in messages
        ],
    }
