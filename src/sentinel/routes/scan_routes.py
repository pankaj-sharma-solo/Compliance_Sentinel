# GET  /scans/threads                          — list threads (newest first)
# GET  /scans/threads/{thread_id}/violations   — violations for a thread
# POST /scans/trigger                          — launch manual scan
# PATCH /scans/threads/{thread_id}/cancel      — cancel running scan
import uuid
import logging
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from sqlalchemy.orm import Session
from sentinel.database import get_db
from sentinel.models.thread import OrchestratorThread, ThreadStatus
from sentinel.models.violation import Violation
from sentinel.models.database_connection import DatabaseConnection
from sentinel.services.audit_service import log_event

logger = logging.getLogger(__name__)


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
def trigger_scan(
    body            : dict,
    background_tasks: BackgroundTasks,
    db              : Session = Depends(get_db),
):
    connection_id = body.get("db_connection_id")
    if not connection_id:
        raise HTTPException(status_code=400, detail="db_connection_id is required")

    conn = db.query(DatabaseConnection).filter_by(id=connection_id).first()
    if not conn:
        raise HTTPException(status_code=404, detail="Database connection not found")

    if not conn.schema_mapped:
        raise HTTPException(status_code=422, detail="Schema not mapped yet — run schema mapping first")

    thread = OrchestratorThread(
        thread_id        = str(uuid.uuid4()),
        workflow_type    = body.get("workflow_type", "policy_review"),
        db_connection_id = connection_id,
        status           = ThreadStatus.RUNNING,
        actor            = body.get("actor", "manual"),
        started_at       = datetime.utcnow(),
    )
    db.add(thread)
    db.commit()

    log_event(db, "SCAN_STARTED", "workflow", thread.thread_id,
              actor=thread.actor,
              detail={"db_connection_id": str(connection_id), "workflow_type": thread.workflow_type})

    background_tasks.add_task(_run_enforcement_scan, thread.thread_id, conn.id, db)

    return {"thread_id": thread.thread_id, "status": "RUNNING"}


def _run_enforcement_scan(thread_id: str, connection_id: int, db: Session):
    """
    Background task — wires DatabaseConnection → ScanState → enforcement graph.
    Updates OrchestratorThread status on completion or failure.
    """
    thread = db.query(OrchestratorThread).filter_by(thread_id=thread_id).first()
    conn   = db.query(DatabaseConnection).filter_by(id=connection_id).first()

    if not thread or not conn:
        logger.error("Scan aborted — thread or connection not found: %s", thread_id)
        return

    try:
        from sentinel.agents.enforcement_agent import build_enforcement_graph
        from sentinel.states.state import ScanState

        graph = build_enforcement_graph(db)

        initial_state: ScanState = {
            "messages"               : [],
            "db_connection_id"       : connection_id,
            "connection_string"      : conn.connection_string_enc,
            "server_region"          : conn.server_region or "",
            "schema_map"             : conn.schema_map or {},
            "relevant_rules"         : [],
            "violations_found"       : [],
            "scan_results"           : [],
            "errors"                 : [],
            "langgraph_checkpoint_id": thread_id,
        }

        result = graph.invoke(initial_state, config={"configurable": {"thread_id": thread_id}})

        errors       = result.get("errors", [])
        scan_results = result.get("scan_results", [])

        # ── Update thread status ──────────────────────────────────────────
        thread.status       = ThreadStatus.FAILED if errors and not scan_results else ThreadStatus.COMPLETED
        thread.completed_at = datetime.utcnow()
        thread.final_response = (
            f"Scan complete. {len(scan_results)} violations persisted."
            + (f" Errors: {'; '.join(errors[:3])}" if errors else "")
        )
        if errors and not scan_results:
            thread.error_detail = "; ".join(errors[:5])

        db.commit()

        # ── Update last_scanned_at on connection ──────────────────────────
        conn.last_scanned_at = datetime.utcnow()
        db.commit()

        log_event(db, "SCAN_COMPLETED", "workflow", thread_id,
                  actor=thread.actor,
                  detail={
                      "violations_found": str(len(scan_results)),
                      "errors"          : str(len(errors)),
                  })

        logger.info("Scan %s complete — %d violations, %d errors", thread_id, len(scan_results), len(errors))

    except Exception as e:
        logger.error("Enforcement scan crashed for thread %s: %s", thread_id, e)
        thread.status       = ThreadStatus.FAILED
        thread.completed_at = datetime.utcnow()
        thread.error_detail = str(e)
        db.commit()

        log_event(db, "SCAN_FAILED", "workflow", thread_id,
                  actor="system", detail={"error": str(e)[:256]})


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
