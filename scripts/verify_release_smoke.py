"""CPU smoke of a newly bootstrapped, empty instance; no model/provider calls."""

import json
from fastapi.testclient import TestClient
from video_platform.api import create_app
from video_platform.config import ROOT
from video_platform.store import TaskStore


def main():
    if TaskStore().list()["total"] != 0:
        raise RuntimeError(
            "Use a fresh review instance, never an existing task database."
        )
    credentials = json.loads((ROOT / ".secrets/admin-bootstrap.json").read_text())
    with TestClient(create_app()) as client:
        assert client.get("/").status_code == 200
        assert client.get("/api/tasks").status_code == 401
        login = client.post(
            "/api/auth/login",
            json=dict(
                username=credentials["username"], password=credentials["password"]
            ),
        )
        login.raise_for_status()
        csrf = login.json()["csrf"]
        assert client.get("/studio").status_code == 200
        assert client.get("/api/library").json()["total"] == 0
        assert client.get("/api/public/demo").json()["status"] == "not_installed"
        assert all(
            not item["configured"]
            for item in client.get("/api/models").json()["models"]
        )
        client.post(
            "/api/auth/logout", headers={"X-CSRF-Token": csrf}
        ).raise_for_status()
        assert client.get("/api/tasks").status_code == 401
    report = dict(
        status="passed",
        empty_instance=True,
        anonymous_private_api=401,
        login_and_logout=True,
        models_configured=False,
        original_instance_data_used=False,
        new_generation_requests=0,
    )
    (ROOT / "logs/release-smoke.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
