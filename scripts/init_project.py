"""Create per-project directories without installing packages or downloading weights."""

from video_platform.config import ROOT


def main():
    for name in ["data", "logs", "runs", "models", "cache", "tmp", "agent_runs"]:
        (ROOT / name).mkdir(parents=True, exist_ok=True)
    print("Project directories ready. No models downloaded; no cloud request made.")


if __name__ == "__main__":
    main()
