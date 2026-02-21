from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker, DeclarativeBase
from sentinel.config import settings

engine = create_engine(
    settings.mysql_url,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20,
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    """FastAPI dependency — yields a DB session and closes it after request."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def install_audit_log_immutability(dbapi_connection, _):
    """
    Install DB-level triggers on audit_logs — immutability enforced at DB level.
    Uses raw DBAPI cursor since this runs on engine connect event.
    Idempotent — drops and recreates on every startup.
    """
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("DROP TRIGGER IF EXISTS prevent_audit_log_mutation")
        cursor.execute("""
            CREATE TRIGGER prevent_audit_log_mutation
            BEFORE UPDATE ON audit_logs
            FOR EACH ROW
            SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'audit_logs is immutable: UPDATE not allowed'
        """)
        cursor.execute("DROP TRIGGER IF EXISTS prevent_audit_log_delete")
        cursor.execute("""
            CREATE TRIGGER prevent_audit_log_delete
            BEFORE DELETE ON audit_logs
            FOR EACH ROW
            SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'audit_logs is immutable: DELETE not allowed'
        """)
        dbapi_connection.commit()
    finally:
        cursor.close()


@event.listens_for(engine, "connect")
def on_connect(dbapi_connection, connection_record):
    install_audit_log_immutability(dbapi_connection, connection_record)
