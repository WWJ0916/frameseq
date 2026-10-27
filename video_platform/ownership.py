"""Request ownership defaults to the existing operator for local CLI/worker tools."""

from contextvars import ContextVar
from contextlib import contextmanager

LEGACY_OWNER = "legacy-owner"
identity = ContextVar(
    "identity", default={"id": LEGACY_OWNER, "role": "admin", "name": "项目管理员"}
)


def owner_id():
    return identity.get()["id"]


def is_admin():
    return identity.get().get("role") == "admin"


@contextmanager
def as_user(user):
    token = identity.set(user)
    try:
        yield
    finally:
        identity.reset(token)


def add_owner_column(db, table):
    # Called inside a short transaction. Legacy records stay owned by the operator.
    if "owner_id" not in {
        row[1] for row in db.execute("PRAGMA table_info(" + table + ")")
    }:
        db.execute(
            "ALTER TABLE "
            + table
            + " ADD COLUMN owner_id TEXT NOT NULL DEFAULT 'legacy-owner'"
        )
