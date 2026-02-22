"""
Policy / PDF ingestion routes.
POST /policies/upload              — trigger full ingestion pipeline
GET  /policies/upload/{job_id}     — poll job status
GET  /policies/documents           — recent uploaded docs with rule counts
GET  /policies/rules               — list rules (filterable by status)
GET  /policies/rules/{id}          — rule detail
PATCH /policies/rules/{id}/approve — approve DRAFT rule (human review queue)
"""
import shutil
import os
import uuid
import logging
from datetime import datetime

from fastapi import APIRouter, Depends, UploadFile, File, HTTPException, BackgroundTasks
from sqlalchemy.orm import Session
from sqlalchemy import func
from sentinel.models.audit_log import AuditLog as AuditLogModel

from sentinel.database import get_db
from sentinel.models.rule import Rule, RuleStatus
from sentinel.models.ingestion_job import IngestionJob, IngestionJobStatus
from sentinel.models.audit_log import AuditLog
from sentinel.agents.ingestion_agent import build_ingestion_graph

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/policies", tags=["Policies"])

UPLOAD_DIR = "/tmp/compliance_uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)

ALLOWED_EXTENSIONS = {".pdf", ".docx", ".txt"}


# ── Background task: runs ingestion graph + updates job record ────────────────

def _run_ingestion(pdf_path: str, source_doc: str, job_id: str, db: Session):
    """
    Runs in background. Updates pdf_ingestion_jobs at each stage.
    """
    job = db.query(IngestionJob).filter_by(job_id=job_id).first()
    if not job:
        logger.error("Job %s not found in DB — aborting ingestion", job_id)
        return

    try:
        # ── Mark as EXTRACTING ────────────────────────────────────────────────
        job.status = IngestionJobStatus.EXTRACTING
        db.commit()

        graph = build_ingestion_graph(db)
        result = graph.invoke({
            "messages": [],
            "pdf_path": pdf_path,
            "source_doc": source_doc,
            "raw_chunks": [],
            "candidate_spans": [],
            "decomposed_rules": [],
            "persisted_rule_ids": [],
            "errors": [],
        })

        # ── Populate counts from graph result ─────────────────────────────────
        persisted_ids: list = result.get("persisted_rule_ids", [])
        errors: list        = result.get("errors", [])
        spans: list         = result.get("candidate_spans", [])
        decomposed: list    = result.get("decomposed_rules", [])

        # Determine final status
        draft_count = db.query(Rule).filter_by(
            source_doc=source_doc,
            status=RuleStatus.DRAFT
        ).count()

        final_status = (
            IngestionJobStatus.AWAITING_REVIEW if draft_count > 0
            else IngestionJobStatus.COMPLETED
        )

        job.status           = final_status
        job.candidate_spans  = len(spans)
        job.rules_decomposed = len(decomposed)
        job.rules_approved   = len(persisted_ids)
        job.error_detail     = "; ".join(errors) if errors else None
        job.completed_at     = datetime.utcnow()
        db.commit()

        logger.info(
            "Ingestion complete — job=%s persisted=%d errors=%d status=%s",
            job_id, len(persisted_ids), len(errors), final_status.value,
        )

    except Exception as e:
        logger.exception("Ingestion pipeline crashed for job %s: %s", job_id, e)
        try:
            job.status       = IngestionJobStatus.FAILED
            job.error_detail = str(e)
            job.completed_at = datetime.utcnow()
            db.commit()
        except Exception:
            pass  # DB itself may be unavailable
    finally:
        # Always clean up the temp file
        if os.path.exists(pdf_path):
            os.remove(pdf_path)


# ── POST /policies/upload ─────────────────────────────────────────────────────

@router.post("/upload", status_code=202)
async def upload_policy_pdf(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    """Upload a compliance PDF and queue the ingestion pipeline."""
    ext = os.path.splitext(file.filename or "")[-1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type '{ext}'. Allowed: {ALLOWED_EXTENSIONS}",
        )

    job_id   = str(uuid.uuid4())
    pdf_path = os.path.join(UPLOAD_DIR, f"{job_id}_{file.filename}")

    # ── Save file to disk ─────────────────────────────────────────────────────
    with open(pdf_path, "wb") as f:
        shutil.copyfileobj(file.file, f)  # type: ignore[arg-type]

    # ── Create job record BEFORE queuing — frontend can poll immediately ───────
    job = IngestionJob(
        job_id     = job_id,
        filename   = file.filename,
        source_doc = file.filename,
        status     = IngestionJobStatus.QUEUED,
    )
    db.add(job)
    db.commit()

    # ── Queue background ingestion ────────────────────────────────────────────
    background_tasks.add_task(_run_ingestion, pdf_path, file.filename, job_id, db)

    return {
        "job_id"   : job_id,
        "status"   : "queued",
        "filename" : file.filename,
        "poll_url" : f"/policies/upload/{job_id}",
    }


# ── GET /policies/upload/{job_id} — job status polling ───────────────────────

@router.get("/upload/{job_id}")
def get_job_status(job_id: str, db: Session = Depends(get_db)):
    """Poll ingestion job progress. Frontend calls this every 2s."""
    job = db.query(IngestionJob).filter_by(job_id=job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    return {
        "job_id"          : job.job_id,
        "filename"        : job.filename,
        "status"          : job.status.value,
        "candidate_spans" : job.candidate_spans,
        "rules_decomposed": job.rules_decomposed,
        "rules_approved"  : job.rules_approved,
        "error_detail"    : job.error_detail,
        "started_at"      : job.started_at.isoformat() if job.started_at else None,
        "completed_at"    : job.completed_at.isoformat() if job.completed_at else None,
    }


# ── GET /policies/documents — recent uploads for UI list ─────────────────────

@router.get("/documents")
def get_recent_documents(limit: int = 3, db: Session = Depends(get_db)):
    """
    Returns recent source docs with rule counts — powers the
    'Recent Policy Documents' list in the frontend.
    """
    rows = (
        db.query(
            Rule.source_doc,
            func.max(Rule.effective_date).label("last_uploaded"),
            func.count(Rule.rule_id).label("rules_count"),
        )
        .group_by(Rule.source_doc)
        .order_by(func.max(Rule.effective_date).desc())
        .limit(limit)
        .all()
    )

    return [
        {
            "name" : row.source_doc,
            "date" : str(row.last_uploaded),
            "rules": row.rules_count,
        }
        for row in rows
    ]


# ── GET /policies/rules ───────────────────────────────────────────────────────

@router.get("/rules")
def list_rules(status: str = "ACTIVE", db: Session = Depends(get_db)):
    try:
        rule_status = RuleStatus(status)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid status '{status}'. Valid: {[s.value for s in RuleStatus]}",
        )

    rules = db.query(Rule).filter(Rule.status == rule_status).all()
    return [
        {
            "rule_id"         : r.rule_id,
            "rule_text"       : r.rule_text[:200],
            "source_doc"      : r.source_doc,
            "article_ref"     : r.article_ref,
            "status"          : r.status.value,
            "obligation_type" : r.obligation_type.value,
            "version"         : r.version,
        }
        for r in rules
    ]


# ── GET /policies/rules/{rule_id} ─────────────────────────────────────────────

@router.get("/rules/{rule_id}")
def get_rule(rule_id: str, db: Session = Depends(get_db)):
    rule = db.query(Rule).filter_by(rule_id=rule_id).first()
    if not rule:
        raise HTTPException(status_code=404, detail="Rule not found")

    return {
        "rule_id"             : rule.rule_id,
        "rule_text"           : rule.rule_text,
        "source_doc"          : rule.source_doc,
        "article_ref"         : rule.article_ref,
        "status"              : rule.status.value,
        "violation_conditions": rule.violation_conditions,
        "obligation_type"     : rule.obligation_type.value,
        "version"             : rule.version,
        "superseded_by"       : rule.superseded_by,
        "effective_date"      : str(rule.effective_date),
    }


# ── PATCH /policies/rules/{rule_id}/approve ───────────────────────────────────

@router.patch("/rules/{rule_id}/approve")
def approve_draft_rule(
    rule_id: str,
    actor: str = "system",
    db: Session = Depends(get_db),
):
    """Approve a DRAFT rule from the human review queue → ACTIVE."""
    rule = db.query(Rule).filter_by(rule_id=rule_id).first()
    if not rule:
        raise HTTPException(status_code=404, detail="Rule not found")
    if rule.status != RuleStatus.DRAFT:
        raise HTTPException(
            status_code=400,
            detail=f"Rule is '{rule.status.value}', expected 'DRAFT'",
        )

    rule.status = RuleStatus.ACTIVE
    audit = AuditLog(
        event_type  = "RULE_APPROVED",
        entity_type = "rule",
        entity_id   = rule_id,
        actor       = actor,
        detail      = {"previous_status": "DRAFT", "new_status": "ACTIVE"},
    )
    db.add(audit)
    db.commit()

    return {"rule_id": rule_id, "status": "ACTIVE"}


# PATCH /policies/rules/{rule_id} — update rule text
@router.patch("/rules/{rule_id}")
def update_rule(rule_id: str, body: dict, db: Session = Depends(get_db)):
    rule = db.query(Rule).filter_by(rule_id=rule_id).first()
    if not rule:
        raise HTTPException(status_code=404, detail="Rule not found")
    if "rule_text" in body:
        rule.rule_text = body["rule_text"]
    db.commit()
    audit = AuditLog(event_type="RULE_UPDATED", entity_type="rule",
                     entity_id=rule_id, actor="admin",
                     detail={"field": "rule_text"})
    db.add(audit); db.commit()
    return {"rule_id": rule_id, "status": "updated"}


# PATCH /policies/rules/{rule_id}/deprecate — mark as stale
@router.patch("/rules/{rule_id}/deprecate")
def deprecate_rule(rule_id: str, db: Session = Depends(get_db)):
    rule = db.query(Rule).filter_by(rule_id=rule_id).first()
    if not rule:
        raise HTTPException(status_code=404, detail="Rule not found")
    rule.status = RuleStatus.DEPRECATED
    audit = AuditLog(event_type="RULE_DEPRECATED", entity_type="rule",
                     entity_id=rule_id, actor="admin",
                     detail={"previous_status": rule.status.value})
    db.add(audit); db.commit()
    return {"rule_id": rule_id, "status": "DEPRECATED"}


# GET /policies/audit-log — for the Audit Log tab
@router.get("/audit-log")
def get_audit_log(entity_type: str = "rule", limit: int = 50, db: Session = Depends(get_db)):
    logs = (db.query(AuditLog)
              .filter_by(entity_type=entity_type)
              .order_by(AuditLog.created_at.desc())
              .limit(limit).all())
    return [
        {"id": l.id, "timestamp": l.created_at.isoformat(),
         "actor": l.actor, "event_type": l.event_type,
         "entity_id": l.entity_id, "detail": l.detail}
        for l in logs
    ]


@router.get("/audit-log")
def get_audit_log(
    entity_type: str | None = None,
    event_type : str | None = None,
    limit      : int        = 50,
    db         : Session    = Depends(get_db),
):
    query = db.query(AuditLogModel).order_by(AuditLogModel.created_at.desc())
    if entity_type:
        query = query.filter_by(entity_type=entity_type)
    if event_type:
        query = query.filter_by(event_type=event_type)
    logs = query.limit(limit).all()
    return [
        {
            "id"         : l.id,
            "timestamp"  : l.created_at.isoformat(),
            "actor"      : l.actor,
            "event_type" : l.event_type,
            "entity_type": l.entity_type,
            "entity_id"  : l.entity_id,
            "detail"     : l.detail,
        }
        for l in logs
    ]

