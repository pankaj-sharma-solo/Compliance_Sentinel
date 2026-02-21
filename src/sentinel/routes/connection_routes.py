# sentinel/routers/connections_routes.py
"""
Database connection management routes.
GET  /connections          — list all registered DB connections
GET  /connections/{id}     — single connection detail
POST /connections          — register a new DB connection
PATCH /connections/{id}    — update connection config
DELETE /connections/{id}   — remove connection
"""
import logging
from datetime import datetime
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional
from sentinel.database import get_db
from sentinel.models.database_connection import DatabaseConnection, ScanMode

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/connections", tags=["Connections"])


# ── Pydantic schemas ──────────────────────────────────────────────────────────

class ConnectionCreate(BaseModel):
    name: str
    connection_string_enc: str
    db_type: str = "mysql"
    server_region: Optional[str] = None
    scan_mode: ScanMode = ScanMode.SCHEDULED
    cron_expression: Optional[str] = None
    owner_user_id: Optional[str] = None

class ConnectionUpdate(BaseModel):
    name: Optional[str] = None
    scan_mode: Optional[ScanMode] = None
    cron_expression: Optional[str] = None
    server_region: Optional[str] = None


# ── Serializer helper ─────────────────────────────────────────────────────────

def _serialize(c: DatabaseConnection) -> dict:
    return {
        "id"              : c.id,
        "name"            : c.name,
        "db_type"         : c.db_type,
        "server_region"   : c.server_region,
        "scan_mode"       : c.scan_mode.value,
        "cron_expression" : c.cron_expression,
        "schema_mapped"   : bool(c.schema_mapped),
        "owner_user_id"   : c.owner_user_id,
        "last_scanned_at" : c.last_scanned_at.isoformat() if c.last_scanned_at else None,
        "created_at"      : c.created_at.isoformat() if c.created_at else None,
    }


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("")
def list_connections(db: Session = Depends(get_db)):
    connections = db.query(DatabaseConnection).order_by(DatabaseConnection.created_at.desc()).all()
    return [_serialize(c) for c in connections]


@router.get("/{connection_id}")
def get_connection(connection_id: int, db: Session = Depends(get_db)):
    conn: DatabaseConnection | None = db.query(DatabaseConnection).filter_by(id=connection_id).first()
    if not conn:
        raise HTTPException(status_code=404, detail="Connection not found")
    return _serialize(conn)


@router.post("", status_code=201)
def create_connection(body: ConnectionCreate, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    conn = DatabaseConnection(**body.model_dump())
    db.add(conn)
    db.commit()
    db.refresh(conn)

    background_tasks.add_task(_run_schema_mapping, conn.id, db)
    logger.info("Registered new DB connection: %s (id=%s)", conn.name, conn.id)
    return _serialize(conn)


@router.patch("/{connection_id}")
def update_connection(connection_id: int, body: ConnectionUpdate, db: Session = Depends(get_db)):
    conn: DatabaseConnection | None = db.query(DatabaseConnection).filter_by(id=connection_id).first()
    if not conn:
        raise HTTPException(status_code=404, detail="Connection not found")
    for field, value in body.model_dump(exclude_none=True).items():
        setattr(conn, field, value)
    db.commit()
    return _serialize(conn)


@router.delete("/{connection_id}", status_code=204)
def delete_connection(connection_id: int, db: Session = Depends(get_db)):
    conn = db.query(DatabaseConnection).filter_by(id=connection_id).first()
    if not conn:
        raise HTTPException(status_code=404, detail="Connection not found")
    db.delete(conn)
    db.commit()


@router.post("/{connection_id}/map-schema", status_code=202)
def trigger_schema_mapping(
    connection_id: int,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    conn = db.query(DatabaseConnection).filter_by(id=connection_id).first()
    if not conn:
        raise HTTPException(status_code=404, detail="Connection not found")

    background_tasks.add_task(_run_schema_mapping, connection_id, db)
    return {"status": "schema_mapping_queued", "connection_id": connection_id}


def _run_schema_mapping(connection_id: int, db: Session):
    conn = db.query(DatabaseConnection).filter_by(id=connection_id).first()
    if not conn:
        return

    try:
        from sentinel.agents.schema_agent import build_schema_agent
        from sentinel.states.state import SchemaMappingState

        graph = build_schema_agent(conn.connection_string_enc, connection_id)

        result = graph.invoke({
            "messages": [],
            "connection_string" : conn.connection_string_enc,
            "db_connection_id"  : connection_id,
            "raw_schema_info"   : [],
            "schema_map"        : None,
            "errors"            : [],
        })

        schema_map = result.get("schema_map")
        errors     = result.get("errors", [])

        if errors:
            logger.warning("Schema mapping had errors for connection %s: %s", connection_id, errors)

        if schema_map is None:
            logger.error("Schema mapping returned no schema_map for connection %s", connection_id)
            return

        # ── Convert SchemaMap → JSON dict for MySQL storage ───────────────
        # Structure: { table_name: { column_name: { category, sensitivity, reason, data_type } } }
        schema_json: dict = {}
        for cls in schema_map.classifications:
            schema_json.setdefault(cls.table_name, {})[cls.column_name] = {
                "compliance_category": cls.compliance_category,
                "sensitivity"        : cls.sensitivity,
                "reason"             : cls.reason,
                "data_type"          : cls.data_type,
            }

        conn.schema_map    = schema_json
        conn.schema_mapped = 1
        db.commit()

        logger.info(
            "Schema mapping complete for connection '%s' — %d tables, %d columns classified",
            conn.name,
            len(schema_json),
            sum(len(cols) for cols in schema_json.values()),
        )

    except Exception as e:
        logger.error("Schema mapping crashed for connection %s: %s", connection_id, e)
        # schema_mapped stays 0 — UI shows clickable 'Unmapped' badge
