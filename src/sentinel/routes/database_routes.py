"""
Database connection registration and scanning routes.
POST /databases              — register a new DB connection
GET  /databases              — list registered connections
POST /databases/{id}/scan   — trigger manual scan
POST /databases/{id}/cdc-event — CDC webhook endpoint
"""
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from sqlalchemy.orm import Session
from pydantic import BaseModel
from sentinel.database import get_db
from sentinel.models.database_connection import DatabaseConnection, ScanMode
from sentinel.agents.schema_agent import build_schema_agent
from sentinel.agents.enforcement_agent import build_enforcement_graph
from sentinel.models.audit_log import AuditLog
import logging

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/databases", tags=["Databases"])


class RegisterDBRequest(BaseModel):
    name: str
    connection_string: str          # will be encrypted before storage in production
    db_type: str = "mysql"
    server_region: str
    scan_mode: str = "SCHEDULED"
    cron_expression: str | None = None
    owner_user_id: str | None = None


class CDCEventPayload(BaseModel):
    event_type: str                  # INSERT | UPDATE
    table_name: str
    changed_row_id: str | None = None
    changed_columns: list[str] = []


def _run_schema_mapping(db_conn: DatabaseConnection, db: Session):
    """Run schema classification agent for a newly registered DB."""
    agent = build_schema_agent(db_conn.connection_string_enc, db_conn.id)
    result = agent.invoke({
        "messages": [],
        "db_connection_id": db_conn.id,
        "connection_string": db_conn.connection_string_enc,
        "raw_schema_info": [],
        "schema_map": None,
        "errors": [],
    })
    if result.get("schema_map"):
        sm = result["schema_map"]
        # Convert to flat dict format for JSON storage
        schema_dict: dict = {}
        for classification in sm.classifications:
            schema_dict.setdefault(classification.table_name, {})[classification.column_name] = {
                "compliance_category": classification.compliance_category,
                "sensitivity": classification.sensitivity,
                "data_type": classification.data_type,
                "reason": classification.reason,
            }
        db_conn.schema_map = schema_dict
        db.commit()
        logger.info("Schema map built for DB connection %s", db_conn.id)


def _run_scan(db_conn: DatabaseConnection, db: Session, checkpoint_id: str | None = None):
    """Run enforcement scan for a DB connection — context isolated per the Deep Agent pattern."""
    graph = build_enforcement_graph(db)
    # Context isolation: state contains only this connection's data
    initial_state = {
        "messages": [{"role": "user", "content": f"Scan database connection {db_conn.id}"}],
        "db_connection_id": db_conn.id,
        "connection_string": db_conn.connection_string_enc,
        "server_region": db_conn.server_region or "",
        "schema_map": db_conn.schema_map or {},
        "relevant_rules": [],
        "scan_results": [],
        "violations_found": [],
        "errors": [],
        "langgraph_checkpoint_id": checkpoint_id,
    }
    result = graph.invoke(initial_state)
    from datetime import datetime
    db_conn.last_scanned_at = datetime.utcnow()
    db.commit()
    return result


@router.post("/")
def register_database(
    req: RegisterDBRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """Register a DB connection and trigger one-time schema classification."""
    conn = DatabaseConnection(
        name=req.name,
        connection_string_enc=req.connection_string,  # TODO: encrypt with Fernet before storing
        db_type=req.db_type,
        server_region=req.server_region,
        scan_mode=ScanMode(req.scan_mode),
        cron_expression=req.cron_expression,
        owner_user_id=req.owner_user_id,
    )
    db.add(conn)
    db.commit()
    db.refresh(conn)

    # One-time schema classification — LLM cost paid here, not at scan time
    background_tasks.add_task(_run_schema_mapping, conn, db)

    audit = AuditLog(
        event_type="DB_REGISTERED",
        entity_type="connection",
        entity_id=str(conn.id),
        actor=req.owner_user_id or "system",
        detail={"name": req.name, "region": req.server_region, "scan_mode": req.scan_mode},
    )
    db.add(audit)
    db.commit()

    return {"id": conn.id, "name": conn.name, "status": "registered", "schema_mapping": "queued"}


@router.get("/")
def list_databases(db: Session = Depends(get_db)):
    connections = db.query(DatabaseConnection).all()
    return [
        {
            "id": c.id,
            "name": c.name,
            "db_type": c.db_type,
            "server_region": c.server_region,
            "scan_mode": c.scan_mode.value,
            "schema_mapped": c.schema_map is not None,
            "last_scanned_at": str(c.last_scanned_at) if c.last_scanned_at else None,
        }
        for c in connections
    ]


@router.post("/{db_id}/scan")
def trigger_manual_scan(
    db_id: int,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """Manually trigger a compliance scan for a registered DB."""
    conn: DatabaseConnection | None = db.query(DatabaseConnection).filter_by(id = db_id).first()
    if not conn:
        raise HTTPException(status_code=404, detail="DB connection not found")
    if not conn.schema_map:
        raise HTTPException(status_code=400, detail="Schema map not yet built — registration in progress")
    background_tasks.add_task(_run_scan, conn, db)
    return {"db_id": db_id, "status": "scan_queued"}


@router.post("/{db_id}/cdc-event")
def receive_cdc_event(
    db_id: int,
    event: CDCEventPayload,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """
    CDC webhook — receives Debezium change events (INSERT/UPDATE only).
    Triggers enforcement scan for the changed table only.
    For high-risk DBs where real-time detection is required.
    """
    conn: DatabaseConnection | None = db.query(DatabaseConnection).filter_by(id = db_id).first()
    if not conn:
        raise HTTPException(status_code=404, detail="DB connection not found")
    if conn.scan_mode != ScanMode.CDC:
        raise HTTPException(status_code=400, detail="DB not configured for CDC scanning")

    logger.info("CDC event received for DB %s: %s on %s", db_id, event.event_type, event.table_name)
    # Targeted scan — inject only the relevant table into schema_map
    targeted_schema = {event.table_name: (conn.schema_map or {}).get(event.table_name, {})}
    conn_copy = DatabaseConnection(
        id=conn.id,
        connection_string_enc=conn.connection_string_enc,
        server_region=conn.server_region,
        schema_map=targeted_schema,
    )
    background_tasks.add_task(_run_scan, conn_copy, db)
    return {"status": "cdc_scan_queued", "table": event.table_name}
