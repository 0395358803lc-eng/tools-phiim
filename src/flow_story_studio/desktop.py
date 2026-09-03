"""Native Windows launcher for Flow Story Studio."""

# ruff: noqa: E501

from __future__ import annotations

import os
import socket
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any
from uuid import uuid4

from platformdirs import user_data_dir

WORKSPACE_GATE_HTML = """<!doctype html>
<html lang="vi"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Flow Story Studio · Chọn thư mục</title><style>
:root{color-scheme:dark;font-family:Inter,Segoe UI,sans-serif;background:#090b0f;color:#eef1f5}
*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;background:radial-gradient(circle at 50% 15%,#252b1b 0,#10141a 38%,#080a0d 76%)}
.card{width:min(650px,calc(100vw - 36px));padding:34px;border:1px solid #343b46;border-radius:16px;background:#11151bdc;box-shadow:0 28px 90px #000b}
.brand{display:flex;gap:12px;align-items:center;color:#e9ff58;font-size:11px;letter-spacing:.16em}.mark{width:36px;height:36px;border:2px solid #e9ff58;border-radius:9px;display:grid;place-items:center;font-size:17px}
.step{margin-top:30px;color:#8f99a8;font:700 10px ui-monospace,Consolas,monospace;letter-spacing:.14em}h1{font-size:27px;line-height:1.2;margin:9px 0 12px}p{color:#9aa3af;font-size:13px;line-height:1.65;margin:0 0 23px}
.flow{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin:0 0 25px}.flow div{border:1px solid #2c323c;border-radius:8px;padding:11px;color:#727c8a;font-size:10px}.flow b{display:block;color:#dfe4eb;margin-bottom:5px}.flow .active{border-color:#657329;background:#1d2214}.flow .active b{color:#e9ff58}
button{width:100%;border:0;border-radius:8px;background:#e9ff58;color:#15180c;padding:13px 16px;font-weight:800;cursor:pointer}button:disabled{opacity:.55;cursor:wait}.hint{display:block;text-align:center;color:#68717e;font-size:10px;margin-top:13px}.error{display:none;color:#ff9d8f;background:#291815;border:1px solid #603229;padding:10px;border-radius:7px;font-size:11px;margin-bottom:12px}
@media(max-width:560px){.card{padding:23px}.flow{grid-template-columns:1fr}.flow div{display:flex;justify-content:space-between}.flow b{margin:0}h1{font-size:22px}}
</style></head><body><main class="card">
<div class="brand"><span class="mark">▶</span><strong>FLOW STORY · CONTINUITY STUDIO</strong></div>
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
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{url}/api/health", timeout=1) as response:
                if response.status == 200:
                    return
        except OSError:
            time.sleep(0.1)
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
        self._credential_root = (
            credential_root
            or Path(user_data_dir("Flow Story Studio", "Flow Story Studio")) / "secrets"
        )

    def _start_backend(self, workspace: Path) -> str:
        with self._lock:
            if self._url:
                return self._url
            workspace = workspace.expanduser().resolve()
            workspace.mkdir(parents=True, exist_ok=True)
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
            )
            self._server = uvicorn.Server(
                uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
            )
            self._thread = threading.Thread(
                target=self._server.run, name="studio-backend", daemon=True
            )
            self._thread.start()
            _wait_until_ready(self._url)
            return self._url

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
        except (OSError, RuntimeError) as exc:
            return {"ok": False, "error": f"Không thể dùng thư mục đã chọn: {exc}"}

    def _shutdown(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=8)


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
        "Flow Story Studio",
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
