"""Copy all PollEdge tables from one database to another (e.g. local SQLite -> hosted PostgreSQL)."""

from __future__ import annotations

import logging

from sqlalchemy import func, insert, select, text

from polledge.db import Base, make_engine
from polledge.errors import PollEdgeError
from polledge.i18n import _

log = logging.getLogger(__name__)

BATCH = 500


def copy_database(source_url: str, target_url: str, replace: bool = False) -> dict[str, int]:
    """Copy every table; the target must be empty unless replace=True (then it is wiped first)."""
    if source_url == target_url:
        raise PollEdgeError(_("Source and target database are the same"))
    src = make_engine(source_url)
    dst = make_engine(target_url)
    tables = Base.metadata.sorted_tables
    counts: dict[str, int] = {}
    with dst.begin() as conn:
        non_empty = [t.name for t in tables if conn.execute(select(func.count()).select_from(t)).scalar()]
        if non_empty and not replace:
            raise PollEdgeError(_("The target database already contains data ({tables}); use --replace to overwrite it",
                                  tables=", ".join(non_empty)))
        for table in reversed(tables):  # children first
            conn.execute(table.delete())
    with src.connect() as source, dst.begin() as target:
        for table in tables:  # parents first
            rows = [dict(r) for r in source.execute(select(table)).mappings()]
            for start in range(0, len(rows), BATCH):
                target.execute(insert(table), rows[start:start + BATCH])
            counts[table.name] = len(rows)
            log.info("Copied %d rows of %s", len(rows), table.name)
        if dst.dialect.name == "postgresql":
            _reset_sequences(target, tables)
    return counts


def _reset_sequences(conn, tables) -> None:
    """After inserting explicit ids, move PostgreSQL id sequences past the copied rows."""
    for table in tables:
        if "id" in table.c and table.c.id.autoincrement in (True, "auto") and table.c.id.primary_key:
            conn.execute(text(
                f"SELECT setval(pg_get_serial_sequence('{table.name}', 'id'), "
                f"COALESCE((SELECT MAX(id) FROM {table.name}), 0) + 1, false)"
            ))
