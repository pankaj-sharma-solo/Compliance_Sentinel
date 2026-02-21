import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy.orm import Session
from sentinel.database import engine, SessionLocal
from sentinel.models.database_connection import DatabaseConnection  # ensure tables created
from sentinel.database import Base
from sentinel.config import settings
from sentinel.routes.policy_routes import router as policy_router
from sentinel.routes.database_routes import router as db_router, _run_scan
from sentinel.routes.violation_routes import router as violation_router
from sentinel.models.database_connection import ScanMode
import uvicorn

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

scheduler = BackgroundScheduler()


def _scheduled_scan_job(db_connection_id: int):
    """Job executed by APScheduler at cron time — mirrors the ambient agent pattern from reference codebase."""
    db: Session = SessionLocal()
    try:
        conn = db.query(DatabaseConnection).filter(DatabaseConnection.id == db_connection_id).first()
        if conn and conn.schema_map:
            logger.info("[Scheduler] Triggering scan for DB connection %s", db_connection_id)
            _run_scan(conn, db)
        else:
            logger.warning("[Scheduler] Skipping scan for DB %s — schema map not ready", db_connection_id)
    except Exception as e:
        logger.error("[Scheduler] Scan failed for DB %s: %s", db_connection_id, e)
    finally:
        db.close()


def _load_scheduled_connections():
    """On startup, register APScheduler jobs for all SCHEDULED DB connections."""
    db: Session = SessionLocal()
    try:
        connections = db.query(DatabaseConnection).filter(
            DatabaseConnection.scan_mode == ScanMode.SCHEDULED
        ).all()
        for conn in connections:
            cron = conn.cron_expression or settings.default_scan_cron
            parts = cron.split()
            if len(parts) == 5:
                minute, hour, day, month, day_of_week = parts
                scheduler.add_job(
                    _scheduled_scan_job,
                    trigger="cron",
                    minute=minute,
                    hour=hour,
                    day=day,
                    month=month,
                    day_of_week=day_of_week,
                    id=f"scan_db_{conn.id}",
                    name=f"Compliance scan: {conn.name}",
                    args=[conn.id],
                    replace_existing=True,
                )
                logger.info("[Scheduler] Registered scan job for DB %s (%s)", conn.id, cron)
        logger.info("[Scheduler] Loaded %d scheduled scan jobs", len(connections))
    finally:
        db.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    Base.metadata.create_all(bind=engine)
    logger.info("Database tables created/verified")
    _load_scheduled_connections()
    scheduler.start()
    logger.info("APScheduler started — %d jobs registered", len(scheduler.get_jobs()))
    yield
    # Shutdown
    scheduler.shutdown(wait=False)
    logger.info("APScheduler stopped")


app = FastAPI(
    title="Compliance Sentinel",
    description="AI-Native Governance Platform — LangGraph + FastAPI + Qdrant + MySQL",
    version="1.0.0",
    lifespan=lifespan,
)

app.include_router(policy_router)
app.include_router(db_router)
app.include_router(violation_router)


@app.get("/health")
def health():
    return {"status": "ok", "scheduler_jobs": len(scheduler.get_jobs())}


def start():
    """Entry point for poetry run start"""
    uvicorn.run(app, host="0.0.0.0", port=8000, reload=True)