# GET  /scans/threads                          — list threads (newest first)
# GET  /scans/threads/{thread_id}/violations   — violations for a thread
# POST /scans/trigger                          — launch manual scan
# PATCH /scans/threads/{thread_id}/cancel      — cancel running scan
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import func
from sentinel.database import get_db
from sentinel.models.thread import OrchestratorThread
from sentinel.models.violation import Violation

router = APIRouter(prefix="/scans", tags=["Scans"])

@router.get("/threads")
def list_threads(limit: int = 20, db: Session = Depends(get_db)):
    threads = (
        db.query(OrchestratorThread)
        .order_by(OrchestratorThread.started_at.desc())
        .limit(limit).all()
    )
    return [
        {
            "thread_id"          : t.thread_id,
            "db_connection_id"   : t.db_connection_id,
            "db_connection_name" : t.db_connection.name if t.db_connection else "Unknown",
            "workflow_type"      : t.workflow_type,
            "status"             : t.status.value,
            "started_at"         : t.started_at.isoformat(),
            "completed_at"       : t.completed_at.isoformat() if t.completed_at else None,
            "interrupted_at"     : t.interrupted_at.isoformat() if t.interrupted_at else None,
            "error_detail"       : t.error_detail,
            "violation_count"    : db.query(Violation).filter_by(
                                     db_connection_id=t.db_connection_id
                                   ).count(),
            "critical_count"     : db.query(Violation).filter_by(
                                     db_connection_id=t.db_connection_id, severity="CRITICAL"
                                   ).count(),
            "high_count"         : db.query(Violation).filter_by(
                                     db_connection_id=t.db_connection_id, severity="HIGH"
                                   ).count(),
        }
        for t in threads
    ]

@router.get("/threads/{thread_id}/violations")
def thread_violations(thread_id: str, db: Session = Depends(get_db)):
    thread = db.query(OrchestratorThread).filter_by(thread_id=thread_id).first()
    if not thread:
        raise HTTPException(status_code=404, detail="Thread not found")
    rows = db.query(Violation).filter_by(db_connection_id=thread.db_connection_id).all()
    return [
        {
            "id"               : v.id,
            "rule_id"          : v.rule_id,
            "table_name"       : v.table_name,
            "column_name"      : v.column_name,
            "severity"         : v.severity.value,
            "condition_matched": v.condition_matched,
            "detected_at"      : v.detected_at.isoformat(),
        }
        for v in rows
    ]

@router.post("/trigger", status_code=202)
def trigger_scan(body: dict, db: Session = Depends(get_db)):
    import uuid
    from datetime import datetime
    from sentinel.models.thread import ThreadStatus
    thread = OrchestratorThread(
        thread_id       = str(uuid.uuid4()),
        workflow_type   = body.get("workflow_type", "policy_review"),
        db_connection_id= body["db_connection_id"],
        status          = ThreadStatus.RUNNING,
        actor           = "manual",
        started_at      = datetime.utcnow(),
    )
    db.add(thread); db.commit()
    # TODO: kick off actual LangGraph scan agent here
    return {"thread_id": thread.thread_id, "status": "RUNNING"}

@router.patch("/threads/{thread_id}/cancel")
def cancel_scan(thread_id: str, db: Session = Depends(get_db)):
    thread = db.query(OrchestratorThread).filter_by(thread_id=thread_id).first()
    if not thread:
        raise HTTPException(status_code=404, detail="Thread not found")
    from sentinel.models.thread import ThreadStatus
    from datetime import datetime
    thread.status = ThreadStatus.CANCELLED
    thread.completed_at = datetime.utcnow()
    db.commit()
    return {"thread_id": thread_id, "status": "CANCELLED"}
