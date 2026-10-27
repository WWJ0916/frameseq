"""Private lab snapshot: explicit source/evidence allowlist and SQLite backup."""

import fcntl
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import zipfile

from video_platform.store import ROOT, DB_PATH, TaskStore, now
from video_platform.cloud import CloudStore


def digest(data):
    return hashlib.sha256(data).hexdigest()


def backup_database(source_path, destination, table):
    with sqlite3.connect(f"file:{source_path}?mode=ro", uri=True) as source:
        source.execute("BEGIN")
        tables = [
            r[0]
            for r in source.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        expected = {
            name: source.execute(
                'SELECT * FROM "' + name.replace('"', '""') + '" ORDER BY id'
            ).fetchall()
            for name in tables
        }
        with sqlite3.connect(destination) as dest:
            source.backup(dest)
    with sqlite3.connect(f"file:{destination}?mode=ro", uri=True) as restored:
        integrity = restored.execute("PRAGMA integrity_check").fetchone()[0]
        actual = {
            name: restored.execute(
                'SELECT * FROM "' + name.replace('"', '""') + '" ORDER BY id'
            ).fetchall()
            for name in tables
        }
    assert integrity == "ok" and actual == expected, (
        "SQLite restore check failed: " + table
    )
    return {
        "integrity": integrity,
        "rows_match": True,
        "rows": len(actual[table]),
        "all_tables_match": True,
        "tables": {name: len(rows) for name, rows in actual.items()},
    }


def main():
    lock = (ROOT / "logs/snapshot.lock").open("a")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    store = TaskStore()
    assert (
        not store.running() and not store.has_queued()
    ), "Snapshot after the queue drains."
    projects_db = ROOT / "data/projects.sqlite3"
    if projects_db.exists():
        with sqlite3.connect(projects_db) as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='edits'").fetchone():
                assert not db.execute(
                    "SELECT 1 FROM edits WHERE status IN ('queued','running')"
                ).fetchone(), "Wait for clip editing before snapshot."
    from video_platform.inference import project_inference_active

    assert (
        not project_inference_active()
    ), "Wait for independent diagnostic model processes too."
    if (ROOT / "data/cloud.sqlite3").exists():
        assert not any(
            task["status"] in {"queued", "running", "waiting", "unknown"}
            for task in CloudStore().list()["items"]
        ), "Resolve active cloud tasks before snapshot."
    assert shutil.disk_usage(ROOT).free > 256 * 1024**2, "Insufficient backup headroom."
    stamp = now().replace(":", "").replace("+", "_")
    target = ROOT / "backups" / stamp
    target.mkdir(parents=True, mode=0o700, exist_ok=False)
    db_copy = target / "tasks.sqlite3"
    databases = {"data/tasks.sqlite3": backup_database(DB_PATH, db_copy, "tasks")}
    with sqlite3.connect(f"file:{db_copy}?mode=ro", uri=True) as restored:
        integrity = restored.execute("PRAGMA integrity_check").fetchone()[0]
        status_counts = dict(
            restored.execute("SELECT status,count(*) FROM tasks GROUP BY status")
        )
    for filename, table in [
        ("cloud.sqlite3", "jobs"),
        ("storyboards.sqlite3", "boards"),
        ("projects.sqlite3", "projects"),
        ("auth.sqlite3", "users"),
    ]:
        source = ROOT / "data" / filename
        if source.exists():
            databases["data/" + filename] = backup_database(
                source, target / filename, table
            )
    files = []
    for name in [
        "README.md",
        "PROJECT_LOG.md",
        "EXPERIMENT_STATE.json",
        ".gitignore",
        "requirements-inference.txt",
        "requirements-platform.txt",
    ]:
        files.append(ROOT / name)
    for directory in [
        "video_platform",
        "scripts",
        "tests",
        "web",
        "docs",
        "runs",
        "logs",
        "agent_runs",
    ]:
        files.extend(
            p
            for p in (ROOT / directory).rglob("*")
            if p.is_file()
            and "__pycache__" not in p.parts
            and p.suffix not in {".pyc", ".lock", ".pid"}
        )
    assert not any(
        p.is_symlink() for p in files
    ), "Do not follow symlinks into external files."
    assert (
        sum(p.stat().st_size for p in files) < 128 * 1024**2
    ), "Review unexpectedly large evidence first."
    manifest = {
        "created_at": now(),
        "status": "running",
        "scope": "source, experiment evidence, agent traces, consistent SQLite backup; no weights, environment, caches or SSH credentials",
        "sqlite_integrity": integrity,
        "sqlite_rows_match": True,
        "databases": databases,
        "task_status_counts": status_counts,
        "entries": {},
    }
    archive = target / "project.zip"
    with zipfile.ZipFile(archive, "x", compression=zipfile.ZIP_DEFLATED) as output:
        for path in sorted(files):
            name = path.relative_to(ROOT).as_posix()
            data = path.read_bytes()
            output.writestr(name, data)
            manifest["entries"][name] = {"bytes": len(data), "sha256": digest(data)}
        for name in databases:
            data = (target / Path(name).name).read_bytes()
            output.writestr(name, data)
            manifest["entries"][name] = {"bytes": len(data), "sha256": digest(data)}
    with zipfile.ZipFile(archive) as check:
        assert check.testzip() is None
        assert set(check.namelist()) == set(manifest["entries"])
        for name, entry in manifest["entries"].items():
            assert digest(check.read(name)) == entry["sha256"], name
    manifest.update(
        status="passed",
        archive=str(archive),
        archive_bytes=archive.stat().st_size,
        archive_sha256=digest(archive.read_bytes()),
        verified_entries=len(manifest["entries"]),
    )
    (target / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2)
    )
    summary = {key: value for key, value in manifest.items() if key != "entries"}
    (ROOT / "logs/snapshot-latest.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2)
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
