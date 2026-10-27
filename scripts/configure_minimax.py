"""Interactive server-side credential setup. Does not contact any API."""

import argparse
import getpass
import json
import os
from pathlib import Path
import stat
import sys

from video_platform.config import ROOT

BASE_URLS = {"cn": "https://api.minimax.cn", "global": "https://api.minimax.io"}


def save_key(root, region, key):
    if (
        region not in BASE_URLS
        or not key
        or any(character.isspace() for character in key)
    ):
        raise ValueError(
            "Invalid region or key format; the key was not printed or saved."
        )
    directory = root / ".secrets"
    if directory.is_symlink():
        raise ValueError("Credential directory must not be a symlink.")
    directory.mkdir(mode=0o700, exist_ok=True)
    if (
        directory.stat().st_uid != os.getuid()
        or stat.S_IMODE(directory.stat().st_mode) != 0o700
    ):
        raise ValueError(
            "Credential directory must belong to this account with mode 700."
        )
    path = directory / "minimax.json"
    # Refuse overwrite and symlinks. A repeated setup cannot silently replace
    # an existing credential, and the token never enters shell arguments.
    descriptor = os.open(
        path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
    )
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(
            {
                "provider": "minimax",
                "region": region,
                "base_url": BASE_URLS[region],
                "api_key": key,
            },
            stream,
        )
        stream.flush()
        os.fsync(stream.fileno())
    return path


def check_configuration(root):
    directory = root / ".secrets"
    path = directory / "minimax.json"
    if not path.exists() and not path.is_symlink():
        return {"configured": False, "network_request_made": False}
    if directory.is_symlink() or path.is_symlink():
        raise ValueError("Refusing symlink credential path.")
    for target, mode in [(directory, 0o700), (path, 0o600)]:
        if (
            target.stat().st_uid != os.getuid()
            or stat.S_IMODE(target.stat().st_mode) != mode
        ):
            raise ValueError("Credential ownership or permissions require review.")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ValueError("Cannot read valid credential configuration.") from None
    region = data.get("region")
    if (
        region not in BASE_URLS
        or data.get("base_url") != BASE_URLS[region]
        or not data.get("api_key")
    ):
        raise ValueError("Incomplete credential configuration.")
    # Do not return the key, its prefix, suffix or length.
    return {
        "configured": True,
        "region": region,
        "network_request_made": False,
        "api_access_verified": False,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--region", choices=BASE_URLS)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        print(json.dumps(check_configuration(ROOT), ensure_ascii=False))
        return
    if not args.region:
        parser.error("Select --region cn or --region global to match your account.")
    if not sys.stdin.isatty() or not sys.stderr.isatty():
        raise ValueError(
            "Use an interactive SSH terminal; do not pipe the key into this script."
        )
    key = getpass.getpass("API Key (hidden, not sent to chat): ")
    save_key(ROOT, args.region, key.strip())
    print("Saved in this project's private lab directory. No API request was made.")
    print(
        "Configuration is not proof of API permission, balance, or successful generation."
    )


if __name__ == "__main__":
    try:
        main()
    except FileExistsError:
        print("Configuration already exists; it was not overwritten.", file=sys.stderr)
        sys.exit(1)
    except (ValueError, OSError) as error:
        # Our own validation messages contain no credential contents.
        print(
            f"Setup failed: {type(error).__name__}. Review region, path and permissions.",
            file=sys.stderr,
        )
        sys.exit(1)
