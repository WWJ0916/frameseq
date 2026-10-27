"""Prepare private operator access once; never print the generated password."""

import argparse, json, os, secrets
from video_platform.store import ROOT, now
from video_platform.auth import AuthStore


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--enable", action="store_true")
    p.add_argument("--username", default="operator")
    args = p.parse_args()
    from scripts.init_project import main as init_directories

    init_directories()
    store = AuthStore(ROOT)
    directory = ROOT / ".secrets"
    directory.mkdir(mode=0o700, exist_ok=True)
    os.chmod(directory, 0o700)
    credential = directory / "admin-bootstrap.json"
    with store.connect() as db:
        exists = db.execute("SELECT username FROM users WHERE role='admin'").fetchone()
    username = exists["username"] if exists else args.username
    if not exists:
        password = secrets.token_urlsafe(24)
        store.create_user(username, password, "项目管理员", admin=True)
        with credential.open("x") as f:
            json.dump(
                dict(
                    username=username,
                    password=password,
                    created_at=now(),
                    note="Initial private operator credential; never put this in README or chat.",
                ),
                f,
            )
        os.chmod(credential, 0o600)
    if args.enable:
        config = directory / "deployment.json"
        if config.exists():
            old = json.loads(config.read_text())
            assert old.get("public_origin") in {
                None,
                "",
            }, "Review live public config before changing."
        config.write_text(
            json.dumps(
                dict(
                    auth_required=True,
                    public_origin=None,
                    allowed_hosts=[],
                    mode="invite_preview",
                    public_network_binding=False,
                ),
                indent=2,
            )
        )
        os.chmod(config, 0o600)
    print(
        json.dumps(
            dict(
                operator_exists=True,
                username=username,
                credential_file_exists=credential.exists(),
                auth_required=args.enable,
                public_binding=False,
            ),
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
