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


def install_audit_log_immutability(connection, _):
    """
    Install a DB-level trigger on audit_logs so no application code
    can UPDATE or DELETE a record. Called once on engine connect.
    Idempotent — drops and recreates trigger on every startup.
    """
    connection.execute(text("DROP TRIGGER IF EXISTS prevent_audit_log_mutation"))
    connection.execute(text("""
        CREATE TRIGGER prevent_audit_log_mutation
        BEFORE UPDATE ON audit_logs
        FOR EACH ROW
        SIGNAL SQLSTATE '45000'
        SET MESSAGE_TEXT = 'audit_logs is immutable: UPDATE not allowed';
    """))
    connection.execute(text("DROP TRIGGER IF EXISTS prevent_audit_log_delete"))
    connection.execute(text("""
        CREATE TRIGGER prevent_audit_log_delete
        BEFORE DELETE ON audit_logs
        FOR EACH ROW
        SIGNAL SQLSTATE '45000'
        SET MESSAGE_TEXT = 'audit_logs is immutable: DELETE not allowed';
    """))


@event.listens_for(engine, "connect")
def on_connect(dbapi_connection, connection_record):
    install_audit_log_immutability(dbapi_connection, connection_record)
