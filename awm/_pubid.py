"""Opaque public ids for rows whose table is shared by every tenant.

``transitions.id`` and ``entity_pending.id`` stay AUTOINCREMENT inside the file:
their order breaks ties between rows stamped at the same instant. Handing that
counter to a caller, though, tells it how many rows OTHER tenants wrote between
two of its own calls -- the side channel ``entities.py`` closes for entity and
merge ids with random ids. Every id that leaves awm for these tables is instead
``HMAC(file key, table:id)`` cut to 53 bits: stable for a row, unordered, and
without the per-file key it says nothing about the counter. Ids here are only
ever OUTPUT (nothing takes one back in), so the map need not be invertible.
"""

import hashlib
import hmac
import secrets
import sqlite3

_ID_MASK = 2 ** 53 - 1
_DDL = "CREATE TABLE IF NOT EXISTS awm_keys (name TEXT PRIMARY KEY, value TEXT NOT NULL)"
_NAME = "public-id"


def _read(db: sqlite3.Connection):
    try:
        row = db.execute("SELECT value FROM awm_keys WHERE name = ?", (_NAME,)).fetchone()
    except sqlite3.OperationalError:
        return None
    return None if row is None else bytes.fromhex(row[0])


def file_key(db: sqlite3.Connection) -> bytes:
    """This file's public-id key, created on first use (committed if no tx was open)."""
    key = _read(db)
    if key is not None:
        return key
    was_open = db.in_transaction
    db.execute(_DDL)
    db.execute("INSERT OR IGNORE INTO awm_keys(name, value) VALUES (?, ?)",
               (_NAME, secrets.token_hex(32)))
    if not was_open:
        db.commit()
    key = _read(db)
    if key is None:  # pragma: no cover - the insert above cannot lose
        raise sqlite3.OperationalError("awm_keys: public-id key missing after insert")
    return key


def public_id(key: bytes, table: str, internal: int) -> int:
    """The id a caller sees for row `internal` of `table`. Never 0."""
    mac = hmac.new(key, f"{table}:{int(internal)}".encode("ascii"), hashlib.sha256).digest()
    return (int.from_bytes(mac[:8], "big") & _ID_MASK) or 1
