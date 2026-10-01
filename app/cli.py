"""VeriFi management commands.

    python -m app.cli status                  what the database holds
    python -m app.cli sync [--all]            fetch new filings (stale companies, or --all)
    python -m app.cli make-admin EMAIL        let a user upload and edit data
    python -m app.cli import-sqlite [PATH]    copy an old SQLite database into the current one
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from sqlalchemy import create_engine, func, inspect, select

from app.config import BASE_DIR
from app.storage.database import Base, engine, get_session, init_db
from app.storage.models import CompanyORM, DocumentORM, FinancialFactORM, IngestionRunORM, UserORM


def cmd_status(_args) -> None:
    db = get_session()
    try:
        print(f"Database: {engine.url.render_as_string(hide_password=True)}")
        for label, model in (("companies", CompanyORM), ("documents", DocumentORM), ("financial facts", FinancialFactORM),
                             ("users", UserORM), ("ingestion runs", IngestionRunORM)):
            print(f"  {label:16} {db.scalar(select(func.count()).select_from(model)):>6}")
        print("\nCompanies (last checked against the source):")
        for c in db.scalars(select(CompanyORM).order_by(CompanyORM.symbol)):
            docs = db.scalar(select(func.count()).select_from(DocumentORM).where(DocumentORM.company_id == c.company_id))
            latest = db.scalar(select(func.max(DocumentORM.period_end)).where(DocumentORM.company_id == c.company_id))
            print(f"  {c.symbol:11} {docs:>3} documents, latest quarter {latest}, synced {c.last_synced_at:%Y-%m-%d %H:%M}" if c.last_synced_at
                  else f"  {c.symbol:11} {docs:>3} documents, never synced")
    finally:
        db.close()


def cmd_sync(args) -> None:
    from app.datastore import sync
    from app.industry.universe import get_industry, list_industries

    for summary in list_industries():
        industry = get_industry(summary.id)
        missing, stale = sync.freshness(industry.companies)
        todo = industry.companies if args.all else missing + stale
        if not todo:
            print(f"{industry.name}: everything is up to date.")
            continue
        print(f"{industry.name}: syncing {len(todo)} companies...")
        warnings = sync.refresh(todo, industry.id)
        for w in warnings:
            print("  ! " + w)
    cmd_status(args)


def cmd_make_admin(args) -> None:
    db = get_session()
    try:
        user = db.scalar(select(UserORM).where(UserORM.email == args.email.strip().lower()))
        if user is None:
            sys.exit(f"No account with email {args.email}. Create it on the sign-up page first.")
        user.role = "admin"
        db.commit()
        print(f"{user.name} <{user.email}> is now an admin.")
    finally:
        db.close()


def cmd_import_sqlite(args) -> None:
    """Copies every row of an older SQLite database into the current
    database, skipping rows that already exist (safe to re-run)."""
    path = Path(args.path)
    if not path.exists():
        sys.exit(f"No SQLite file at {path}")
    source = create_engine(f"sqlite:///{path}")
    source_tables = set(inspect(source).get_table_names())
    db = get_session()
    try:
        for table in Base.metadata.sorted_tables:
            if table.name not in source_tables:
                continue
            pk = [c.name for c in table.primary_key.columns]
            existing = {tuple(r) for r in db.execute(select(*[table.c[k] for k in pk]))}
            with source.connect() as conn:
                source_cols = {c["name"] for c in inspect(source).get_columns(table.name)}
                rows = [dict(r._mapping) for r in conn.execute(select(*[table.c[c] for c in table.c.keys() if c in source_cols]))]
            new = [r for r in rows if tuple(r[k] for k in pk) not in existing]
            if table.name == "users":
                for r in new:
                    r.setdefault("role", "viewer")
            if new:
                db.execute(table.insert(), new)
            print(f"  {table.name:22} {len(new):>5} copied, {len(rows) - len(new):>5} already present")
        db.commit()
    finally:
        db.close()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m app.cli", description="VeriFi management commands")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="show what the database holds").set_defaults(func=cmd_status)
    p = sub.add_parser("sync", help="fetch new filings from the source")
    p.add_argument("--all", action="store_true", help="re-check every company, not only stale ones")
    p.set_defaults(func=cmd_sync)
    p = sub.add_parser("make-admin", help="give a user admin rights")
    p.add_argument("email")
    p.set_defaults(func=cmd_make_admin)
    p = sub.add_parser("import-sqlite", help="copy an old SQLite database into the current database")
    p.add_argument("path", nargs="?", default=str(BASE_DIR / "data" / "financial_research_agent.db"))
    p.set_defaults(func=cmd_import_sqlite)

    args = parser.parse_args(argv)
    init_db()
    args.func(args)


if __name__ == "__main__":
    main()
