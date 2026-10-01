"""Database engine and schema setup.

PostgreSQL (with pgvector) is the primary database - locally via
`docker compose up -d db`, and a hosted Postgres (Neon/Supabase) when
deployed. The schema is owned by Alembic migrations (migrations/), applied
automatically on startup, so a fresh database and a deployed one always end
up with identical tables.

SQLite remains supported as a zero-setup fallback (and for the test suite):
there the tables are created directly from the models.
"""
from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import BASE_DIR, get_settings

logger = logging.getLogger("financial_research_agent.storage")


class Base(DeclarativeBase):
    pass


settings = get_settings()
IS_SQLITE = settings.database_url.startswith("sqlite")

# Ensure sqlite data dir exists (e.g. sqlite:///./data/foo.db)
if settings.database_url.startswith("sqlite:///./"):
    db_path = Path(settings.database_url.replace("sqlite:///./", ""))
    db_path.parent.mkdir(parents=True, exist_ok=True)

engine = create_engine(
    settings.database_url,
    connect_args={"check_same_thread": False} if IS_SQLITE else {},
    # Hosted Postgres closes idle connections; pre-ping replaces dead ones
    # instead of failing the next request.
    pool_pre_ping=not IS_SQLITE,
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def run_migrations() -> None:
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(BASE_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BASE_DIR / "migrations"))
    cfg.attributes["configure_logger"] = False
    with engine.begin() as connection:
        cfg.attributes["connection"] = connection
        command.upgrade(cfg, "head")


def init_db() -> None:
    import app.storage.models  # noqa: F401 (registers tables on Base.metadata)

    if IS_SQLITE:
        Base.metadata.create_all(bind=engine)
        _sqlite_add_missing_columns()
    else:
        run_migrations()
        logger.info("Database schema is up to date (%s)", engine.url.render_as_string(hide_password=True))


def _sqlite_add_missing_columns() -> None:
    """create_all never alters existing tables; an older local SQLite file
    predates users.role, so add it in place."""
    columns = {c["name"] for c in inspect(engine).get_columns("users")}
    if "role" not in columns:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE users ADD COLUMN role VARCHAR DEFAULT 'viewer' NOT NULL"))


def get_session() -> Session:
    return SessionLocal()
