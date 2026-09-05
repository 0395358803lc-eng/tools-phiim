from __future__ import annotations

import argparse
import socket
from pathlib import Path


def _available_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _run_smoke_backend(workspace: Path, ready_file: Path) -> None:
    import uvicorn

    from flow_story_studio.main import create_app
    from flow_story_studio.storage import ProjectStorage

    workspace = workspace.expanduser().resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    port = _available_port()
    app = create_app(
        ProjectStorage(workspace / "projects"),
        credential_root=workspace / "secrets",
        session_token="release-smoke-session",
    )
    ready_file.parent.mkdir(parents=True, exist_ok=True)
    ready_file.write_text(f"http://127.0.0.1:{port}", encoding="utf-8")
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        log_config=None,
        access_log=False,
    )


def main() -> None:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--smoke-backend", action="store_true")
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--ready-file", type=Path)
    args, _ = parser.parse_known_args()
    if args.smoke_backend:
        if args.workspace is None or args.ready_file is None:
            raise SystemExit("--smoke-backend requires --workspace and --ready-file")
        _run_smoke_backend(args.workspace, args.ready_file)
        return

    from flow_story_studio.desktop import run_desktop

    run_desktop()


if __name__ == "__main__":
    main()
