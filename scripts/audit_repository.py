"""Inspect a source tree without echoing suspected credential contents."""

import argparse, hashlib, json, re, subprocess
from pathlib import Path

BLOCKED = {
    ".local-ssh",
    ".secrets",
    "sources",
    "data",
    "runs",
    "logs",
    "backups",
    "models",
    "agent_runs",
    ".access-backup",
}

# One already approved original episode; other large files still fail the audit.
APPROVED_MEDIA = {
    "preview/assets/episode-demo.mp4": (
        27743489,
        "a9300d0c468e819d3675b670e49877f540a3eb9eae6fcbf7a13aa20a81b3f3e4",
    )
}
PATTERNS = {
    "private_key": re.compile(r"-----BEGIN (?:OPENSSH |RSA |EC )?PRIVATE KEY-----"),
    "provider_key": re.compile(r"sk-" + r"api-[A-Za-z0-9_-]{32,}"),
    "personal_absolute_path": re.compile(r"(?:/" + r"lab/|[A-Z]:[/\\]Users[/\\])"),
    "embedded_secret": re.compile(
        r"""["'](?:api_key|password)["']\s*:\s*["'](?!fixture|dummy|test|example|your|Initial)[A-Za-z0-9_-]{28,}["']"""
    ),
}


def inspect(root, tracked=False):
    findings = []
    checked = 0
    if tracked:
        result = subprocess.run(
            ["git", "ls-files", "-z"], cwd=root, capture_output=True, check=True
        )
        files = [root / name for name in result.stdout.decode().split("\0") if name]
    else:
        files = root.rglob("*")
    for path in files:
        relative = path.relative_to(root)
        if ".git" in relative.parts or "__pycache__" in relative.parts:
            continue
        if any(part in BLOCKED for part in relative.parts):
            if path.is_file():
                findings.append(
                    dict(path=relative.as_posix(), rule="private_artifact_directory")
                )
            continue
        if not path.is_file():
            continue
        if path.is_symlink():
            findings.append(dict(path=relative.as_posix(), rule="symlink"))
            continue
        if path.suffix in {".sqlite3", ".pem", ".key", ".pt", ".safetensors"}:
            findings.append(
                dict(path=relative.as_posix(), rule="private_or_large_binary")
            )
            continue
        if path.stat().st_size > 5 * 1024**2:
            approved = APPROVED_MEDIA.get(relative.as_posix())
            if (
                not approved
                or (path.stat().st_size, hashlib.sha256(path.read_bytes()).hexdigest())
                != approved
            ):
                findings.append(dict(path=relative.as_posix(), rule="large_file"))
        if path.suffix not in {
            ".py",
            ".js",
            ".mjs",
            ".html",
            ".css",
            ".md",
            ".txt",
            ".yml",
            ".yaml",
            ".json",
            ".toml",
            ".sh",
        }:
            continue
        checked += 1
        text = path.read_text(encoding="utf-8-sig")
        for rule, pattern in PATTERNS.items():
            if pattern.search(text):
                findings.append(dict(path=relative.as_posix(), rule=rule))
    if not checked:
        findings.append(dict(path=".", rule="no_source_files_checked"))
    return dict(
        status="passed" if not findings else "failed",
        text_files_checked=checked,
        findings=findings,
        limitation="Pattern and allowlist checks do not replace code review or a dedicated secret scanner.",
    )


def main():
    p = argparse.ArgumentParser()
    p.add_argument("root", nargs="?", type=Path, default=Path.cwd())
    p.add_argument("--tracked", action="store_true")
    a = p.parse_args()
    report = inspect(a.root.resolve(), a.tracked)
    print(json.dumps(report, indent=2))
    raise SystemExit(bool(report["findings"]))


if __name__ == "__main__":
    main()
