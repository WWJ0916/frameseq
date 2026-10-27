"""Restore a verified private snapshot under lab; never replace live files."""

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import stat
import re
import subprocess
import sys
import zipfile

from video_platform.store import ROOT, DB_PATH, now

REPORT = ROOT / "logs/restore-report.json"


def sha(data):
    return hashlib.sha256(data).hexdigest()


def live_rows():
    result = {}
    for name, table in [
        ("tasks.sqlite3", "tasks"),
        ("cloud.sqlite3", "jobs"),
        ("storyboards.sqlite3", "boards"),
        ("projects.sqlite3", "projects"),
        ("projects.sqlite3", "versions"),
        ("projects.sqlite3", "exports"),
        ("auth.sqlite3", "users"),
        ("auth.sqlite3", "sessions"),
        ("auth.sqlite3", "invites"),
        ("auth.sqlite3", "attempts"),
        ("auth.sqlite3", "audit"),
    ]:
        path = ROOT / "data" / name
        if path.exists():
            with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
                if db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                    (table,),
                ).fetchone():
                    result[name + ":" + table] = db.execute(
                        f"SELECT * FROM {table} ORDER BY id"
                    ).fetchall()
    path = ROOT / "data/projects.sqlite3"
    if path.exists():
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='edits'").fetchone():
                result["projects.sqlite3:edits"] = db.execute(
                    "SELECT * FROM edits ORDER BY id"
                ).fetchall()
    path = ROOT / "data/auth.sqlite3"
    if path.exists():
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
            for table in (
                "memberships",
                "campaigns",
                "model_settings",
                "management_audit",
            ):
                if db.execute(
                    "SELECT 1 FROM sqlite_master WHERE name=?", (table,)
                ).fetchone():
                    result["auth.sqlite3:" + table] = db.execute(
                        "SELECT * FROM " + table + " ORDER BY id"
                    ).fetchall()
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--report-name", default=REPORT.name)
    args = parser.parse_args()
    if not re.fullmatch(r"[a-z0-9_-]{1,80}\.json", args.report_name):
        parser.error("Report must be a plain JSON filename under project logs.")
    report_path = ROOT / "logs" / args.report_name
    if report_path.exists():
        raise RuntimeError("Restore report exists; do not repeat automatically.")
    before = live_rows()
    summary = json.loads((ROOT / "logs/snapshot-latest.json").read_text())
    archive = Path(summary["archive"]).resolve()
    assert archive.is_relative_to((ROOT / "backups").resolve())
    manifest = json.loads((archive.parent / "manifest.json").read_text())
    assert sha(archive.read_bytes()) == manifest["archive_sha256"]
    assert shutil.disk_usage(ROOT).free > 256 * 1024**2
    target = (
        ROOT / "backups" / ("restore-check-" + now().replace(":", "").replace("+", "_"))
    )
    target.mkdir(mode=0o700, exist_ok=False)
    with zipfile.ZipFile(archive) as source:
        assert set(source.namelist()) == set(manifest["entries"])
        assert len(source.namelist()) == len(manifest["entries"])
        assert sum(entry.file_size for entry in source.infolist()) < 128 * 1024**2
        for entry in source.infolist():
            name = PurePosixPath(entry.filename)
            assert not name.is_absolute() and ".." not in name.parts
            assert "\\" not in entry.filename and ":" not in entry.filename
            assert not stat.S_ISLNK(entry.external_attr >> 16)
            destination = target.joinpath(*name.parts)
            assert destination.resolve().is_relative_to(target.resolve())
            data = source.read(entry)
            expected = manifest["entries"][entry.filename]
            assert len(data) == expected["bytes"] and sha(data) == expected["sha256"]
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
    # Import the restored source in a fresh CPU process. Redirect only that
    # process's HTTP artifact root and explicit store to the isolated copy.
    code = r"""
import hashlib, json, sqlite3, sys
from pathlib import Path
from fastapi.testclient import TestClient
import video_platform.api as api
from video_platform.store import TaskStore
from video_platform.cloud import CloudStore
from video_platform.storyboard import StoryboardStore
root = Path(sys.argv[1])
assert Path(api.__file__).resolve().is_relative_to(root)
api.ROOT = root
store = TaskStore(root / "data/tasks.sqlite3")
with store.connect() as db:
    assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    counts = dict(db.execute("SELECT status,count(*) FROM tasks GROUP BY status"))
tasks = store.list(limit=100)["items"]
successes = [task for task in tasks if task["status"] == "succeeded"]
# Internal fixture-only readback, explicitly bypass login without exposing a socket.
with TestClient(api.create_app(store,deployment={'auth_required':False})) as client:
    listing = client.get("/api/tasks")
    assert listing.status_code == 200 and listing.json()["total"] == len(tasks)
    checked = []
    for task in successes:
        path = root / "runs" / ("task-" + task["id"]) / "video.mp4"
        data = path.read_bytes()
        assert hashlib.sha256(data).hexdigest() == task["metrics"]["validation"]["sha256"]
        response = client.get(f"/api/tasks/{task['id']}/video", headers={"Range":"bytes=0-31"})
        assert response.status_code == 206 and response.content == data[:32]
        checked.append(task["id"])
    cloud = CloudStore(root).list()["items"] if (root / "data/cloud.sqlite3").exists() else []
    cloud_checked = []
    for task in cloud:
        if task["status"] != "succeeded":
            continue
        data = (root / "runs" / ("cloud-" + task["id"]) / "video.mp4").read_bytes()
        assert hashlib.sha256(data).hexdigest() == task["result"]["sha256"]
        response = client.get(f"/api/cloud/tasks/{task['id']}/video", headers={"Range":"bytes=0-31"})
        assert response.status_code == 206 and response.content == data[:32]
        cloud_checked.append(task["id"])
    boards = StoryboardStore(root).list() if (root / "data/storyboards.sqlite3").exists() else []
    for board in boards:
        assert client.get("/api/storyboards/" + board["id"]).json() == board
    if cloud:
        assert client.get("/api/library?limit=100").json()["total"] == len(tasks) + len(cloud)
    projects = client.get("/api/projects").json()["items"] if (root / "data/projects.sqlite3").exists() else []
    for project in projects:
        assert client.get("/api/projects/" + project["id"]).json() == project
    from video_platform.composition import ExportStore
    exported=[]
    for project in projects:
        for record in ExportStore(root).list(project['id']):
            if record['status']!='succeeded':continue
            data=(root/'runs'/('episode-'+record['id'])/'video.mp4').read_bytes()
            assert hashlib.sha256(data).hexdigest()==record['result']['sha256']
            response=client.get('/api/exports/'+record['id']+'/video',headers={'Range':'bytes=0-31'})
            assert response.status_code==206 and response.content==data[:32]
            exported.append(record['id'])
    from video_platform.edits import EditStore
    edited=[]
    for record in EditStore(root).list():
        if record['status']!='succeeded':continue
        data=(root/'runs'/('edit-'+record['id'])/'video.mp4').read_bytes()
        assert hashlib.sha256(data).hexdigest()==record['result']['sha256']
        response=client.get('/api/edits/'+record['id']+'/video',headers={'Range':'bytes=0-31'})
        assert response.status_code==206 and response.content==data[:32]
        edited.append(record['id'])
    database_integrity = {}
    for name in ["tasks.sqlite3", "cloud.sqlite3", "storyboards.sqlite3", "projects.sqlite3",'auth.sqlite3']:
        path = root / "data" / name
        if path.exists():
            with sqlite3.connect(path) as db:
                database_integrity[name] = db.execute("PRAGMA integrity_check").fetchone()[0]
                assert database_integrity[name] == "ok"
print(json.dumps({"restored_source_import":True,"sqlite_integrity":"ok",
 "task_status_counts":counts,"api_history_total":len(tasks),"verified_video_hashes_and_ranges":checked,
 "cloud_history_total":len(cloud),"cloud_video_hashes_and_ranges":cloud_checked,
 "storyboard_records":len(boards),"project_records":len(projects),"episode_exports_verified":exported,"clip_edits_verified":edited,"database_integrity":database_integrity}))
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(target)],
        cwd=target,
        capture_output=True,
        text=True,
        timeout=90,
        check=True,
    )
    restored = json.loads(result.stdout)
    assert restored["task_status_counts"] == manifest["task_status_counts"]
    assert (
        live_rows() == before
    ), "Live tasks changed during verification; review before claiming unchanged."
    report = {
        "status": "passed",
        "observed_at": now(),
        "snapshot": str(archive),
        "restored_directory": str(target),
        "verified_entries": len(manifest["entries"]),
        "checks": restored,
        "live_database_rows_unchanged": True,
        "scope": "Same-machine isolated source/SQLite/video readback via in-process HTTP; no live replacement, GPU run, new-machine install or external network service.",
    }
    report_path.write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
