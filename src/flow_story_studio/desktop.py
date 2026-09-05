"""Native Windows launcher for TH Media."""

# ruff: noqa: E501

from __future__ import annotations

import http.client
import os
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from platformdirs import user_data_dir

from .logging_config import configure_logging, get_logger
from .workspace_lock import WorkspaceLock, WorkspaceLockError

LOGGER = get_logger("desktop")

WORKSPACE_GATE_HTML = """<!doctype html>
<html lang="vi"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>TH Media · Chọn thư mục</title><style>
:root{color-scheme:dark;font-family:Inter,Segoe UI,sans-serif;background:#071018;color:#eef7fb}
*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;background:radial-gradient(circle at 50% 12%,#12384a 0,#0c1a25 40%,#050a0f 78%)}
.card{width:min(650px,calc(100vw - 36px));padding:34px;border:1px solid #343b46;border-radius:16px;background:#11151bdc;box-shadow:0 28px 90px #000b}
.brand{display:flex;gap:12px;align-items:center;color:#67e8f9;font-size:11px;letter-spacing:.16em}.mark{width:36px;height:36px;border:1px solid #49cfe6;border-radius:9px;display:grid;place-items:center;font-size:17px}
.step{margin-top:30px;color:#8f99a8;font:700 10px ui-monospace,Consolas,monospace;letter-spacing:.14em}h1{font-size:27px;line-height:1.2;margin:9px 0 12px}p{color:#9aa3af;font-size:13px;line-height:1.65;margin:0 0 23px}
.flow{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin:0 0 25px}.flow div{border:1px solid #2c323c;border-radius:8px;padding:11px;color:#727c8a;font-size:10px}.flow b{display:block;color:#dfe4eb;margin-bottom:5px}.flow .active{border-color:#2e7288;background:#0c232d}.flow .active b{color:#67e8f9}
button{width:100%;border:0;border-radius:8px;background:linear-gradient(135deg,#67e8f9,#38bdf8);color:#04202a;padding:13px 16px;font-weight:800;cursor:pointer}button:disabled{opacity:.55;cursor:wait}.hint{display:block;text-align:center;color:#68717e;font-size:10px;margin-top:13px}.error{display:none;color:#ff9d8f;background:#291815;border:1px solid #603229;padding:10px;border-radius:7px;font-size:11px;margin-bottom:12px}
@media(max-width:560px){.card{padding:23px}.flow{grid-template-columns:1fr}.flow div{display:flex;justify-content:space-between}.flow b{margin:0}h1{font-size:22px}}
</style></head><body><main class="card">
<div class="brand"><span class="mark">TH</span><strong>TH MEDIA · AI STORY PRODUCTION</strong></div>
<div class="step">BƯỚC 01 / 03</div><h1>Chọn thư mục làm việc</h1>
<p>Mỗi lần mở ứng dụng là một phiên mới. Hãy chọn hoặc tạo một thư mục riêng; project, video và ảnh tham chiếu sẽ được lưu tại đó. API key/cookie đã mã hóa được ứng dụng tự ghi nhớ riêng.</p>
<div class="flow"><div class="active"><b>01 · Thư mục</b>Đang thực hiện</div><div><b>02 · Phân tích</b>Chưa mở</div><div><b>03 · Sản xuất</b>Chưa mở</div></div>
<div class="error" id="error"></div><button id="choose" disabled>Đang khởi tạo bộ chọn thư mục...</button>
<span class="hint">Ứng dụng không tự mở lại dự án cũ. Bạn vẫn có thể chủ động mở project đã lưu sau đó.</span>
</main><script>
const button=document.getElementById('choose'),error=document.getElementById('error');
function bridgeReady(){return typeof window.pywebview?.api?.choose_workspace==='function'}
function enableChooser(){if(!bridgeReady())return;button.disabled=false;button.textContent='Chọn thư mục trên máy tính →';error.style.display='none'}
window.addEventListener('pywebviewready',enableChooser);enableChooser();
const bridgePoll=setInterval(()=>{if(bridgeReady()){clearInterval(bridgePoll);enableChooser()}},100);
setTimeout(()=>{if(!bridgeReady()){error.textContent='Bộ chọn thư mục chưa sẵn sàng. Hãy đóng ứng dụng và mở lại.';error.style.display='block'}},10000);
button.onclick=async()=>{button.disabled=true;button.textContent='Đang mở hộp chọn thư mục...';error.style.display='none';
try{if(!bridgeReady())throw new Error('Bộ chọn thư mục chưa sẵn sàng');const result=await window.pywebview.api.choose_workspace();if(result.ok){button.textContent='Đang khởi động phiên mới...';window.location.replace(result.url);return}if(result.error){error.textContent=result.error;error.style.display='block'}}catch(e){error.textContent='Không thể chọn thư mục: '+e;error.style.display='block'}
button.disabled=false;button.textContent='Chọn thư mục trên máy tính →'};
</script></body></html>"""


def _available_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _wait_until_ready(url: str, timeout: float = 20) -> None:
    parsed = urlsplit(url)
    if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.port is None:
        raise ValueError("Backend readiness URL must be loopback HTTP")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=1)
        try:
            connection.request("GET", "/api/health")
            response = connection.getresponse()
            if response.status == 200:
                return
        except OSError:
            time.sleep(0.1)
        finally:
            connection.close()
    raise RuntimeError("Backend nội bộ không khởi động được")


class DesktopSession:
    """Own one fresh desktop session and its explicitly selected workspace."""

    def __init__(self, webview_module: Any, credential_root: Path | None = None) -> None:
        # pywebview recursively exposes every public attribute of ``js_api``.
        # Keep bridge internals private so only ``choose_workspace`` is injected.
        self._webview = webview_module
        self._window: Any | None = None
        self._server: Any | None = None
        self._thread: threading.Thread | None = None
        self._url = ""
        self._lock = threading.Lock()
        self._workspace_lock: WorkspaceLock | None = None
        self._session_token = uuid4().hex + uuid4().hex
        app_data_root = Path(user_data_dir("TH Media", "TH Media"))
        legacy_data_root = Path(user_data_dir("Flow Story Studio", "Flow Story Studio"))
        new_secret_root = app_data_root / "secrets"
        legacy_secret_root = legacy_data_root / "secrets"
        if credential_root is not None:
            self._credential_root = credential_root
        elif new_secret_root.exists() or not legacy_secret_root.exists():
            self._credential_root = new_secret_root
        else:
            self._credential_root = legacy_secret_root
        configure_logging(app_data_root / "logs")

    def _start_backend(self, workspace: Path) -> str:
        with self._lock:
            if self._url:
                return self._url
            workspace = workspace.expanduser().resolve()
            workspace.mkdir(parents=True, exist_ok=True)
            workspace_lock = WorkspaceLock(workspace)
            workspace_lock.acquire()
            self._workspace_lock = workspace_lock
            probe = workspace / f".flow-story-write-test-{uuid4().hex}.tmp"
            try:
                probe.write_text("ok", encoding="utf-8")
            finally:
                probe.unlink(missing_ok=True)

            os.environ["FLOW_STUDIO_DATA_DIR"] = str(workspace)
            os.environ["FLOW_STUDIO_SESSION_ID"] = uuid4().hex

            import uvicorn

            from .main import create_app
            from .storage import ProjectStorage

            port = _available_port()
            self._url = f"http://127.0.0.1:{port}"
            app = create_app(
                ProjectStorage(workspace / "projects"),
                credential_root=self._credential_root,
                session_token=self._session_token,
            )
            self._server = uvicorn.Server(
                uvicorn.Config(
                    app,
                    host="127.0.0.1",
                    port=port,
                    log_level="warning",
                    log_config=None,
                    access_log=False,
                )
            )
            self._thread = threading.Thread(
                target=self._server.run, name="studio-backend", daemon=True
            )
            self._thread.start()
            _wait_until_ready(self._url)
            LOGGER.info("Desktop backend ready for workspace %s", workspace)
            return f"{self._url}/#session={self._session_token}"

    def choose_workspace(self) -> dict[str, object]:
        if self._window is None:
            return {"ok": False, "error": "Cửa sổ ứng dụng chưa sẵn sàng"}
        selection = self._window.create_file_dialog(self._webview.FOLDER_DIALOG)
        if not selection:
            return {"ok": False}
        try:
            workspace = Path(selection[0]).resolve()
            url = self._start_backend(workspace)
            return {"ok": True, "url": url, "workspace": str(workspace)}
        except (OSError, RuntimeError, WorkspaceLockError) as exc:
            if self._workspace_lock is not None:
                self._workspace_lock.release()
                self._workspace_lock = None
            LOGGER.exception("Unable to open workspace %s", selection[0])
            return {"ok": False, "error": f"Không thể dùng thư mục đã chọn: {exc}"}

    def _shutdown(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=8)
        if self._workspace_lock is not None:
            self._workspace_lock.release()
            self._workspace_lock = None


def _workspace_override() -> Path | None:
    value = os.getenv("FLOW_STUDIO_WORKSPACE_DIR") or os.getenv("FLOW_STUDIO_DATA_DIR")
    return Path(value).expanduser().resolve() if value else None


def run_desktop() -> None:
    if getattr(sys, "frozen", False):
        bundled_browsers = Path(getattr(sys, "_MEIPASS", "")) / "playwright-browsers"
        os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(bundled_browsers))

    import webview

    session = DesktopSession(webview)
    workspace = _workspace_override()
    initial_url = session._start_backend(workspace) if workspace else None
    session._window = webview.create_window(
        "TH Media",
        initial_url or "",
        html=None if initial_url else WORKSPACE_GATE_HTML,
        js_api=None if initial_url else session,
        width=1100 if initial_url else 720,
        height=700 if initial_url else 600,
        min_size=(680, 520),
        background_color="#0b0d12",
    )
    try:
        webview.start(gui="edgechromium", debug=False)
    finally:
        session._shutdown()


if __name__ == "__main__":
    run_desktop()
