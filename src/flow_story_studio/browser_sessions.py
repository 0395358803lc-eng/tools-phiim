from __future__ import annotations

import json
import os
import shutil
import socket
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import psutil

GOOGLE_FLOW_URL = "https://labs.google/fx/tools/flow"


class BrowserSessionError(RuntimeError):
    pass


class GoogleFlowSessionManager:
    """Persist isolated Chrome profiles for user-managed Google Flow login sessions."""

    def __init__(
        self,
        root: Path,
        *,
        chrome_path: Path | None = None,
        launcher=None,
    ) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.state_path = self.root / "sessions.json"
        self._chrome_path = chrome_path
        self._launcher = launcher or self._launch_process

    def _empty_state(self) -> dict[str, object]:
        return {
            "active_session_id": "",
            "pending_session_id": "",
            "sessions": [],
        }

    def _read_state(self) -> dict[str, object]:
        if not self.state_path.is_file():
            return self._empty_state()
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return self._empty_state()
        if not isinstance(payload, dict):
            return self._empty_state()
        sessions = payload.get("sessions")
        if not isinstance(sessions, list):
            sessions = []
        return {
            "active_session_id": str(payload.get("active_session_id") or ""),
            "pending_session_id": str(payload.get("pending_session_id") or ""),
            "sessions": [item for item in sessions if isinstance(item, dict)],
        }

    def _write_state(self, payload: dict[str, object]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        handle, temp_name = tempfile.mkstemp(
            prefix=".sessions-",
            suffix=".json.tmp",
            dir=self.root,
        )
        temp_path = Path(temp_name)
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_path, self.state_path)
        finally:
            temp_path.unlink(missing_ok=True)

    def _find_chrome(self) -> Path | None:
        if self._chrome_path and self._chrome_path.is_file():
            return self._chrome_path
        configured = os.getenv("TH_MEDIA_CHROME_PATH", "").strip()
        candidates = []
        if configured:
            candidates.append(Path(configured))
        local_app_data = os.getenv("LOCALAPPDATA", "")
        if local_app_data:
            candidates.append(
                Path(local_app_data) / "Google" / "Chrome" / "Application" / "chrome.exe"
            )
        candidates.extend(
            [
                Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
                Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
            ]
        )
        discovered = shutil.which("chrome") or shutil.which("chrome.exe")
        if discovered:
            candidates.append(Path(discovered))
        return next((item for item in candidates if item.is_file()), None)

    def _profile_dir(self, session_id: str) -> Path:
        return self.root / session_id / "chrome-profile"

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat()

    def _session(self, state: dict[str, object], session_id: str) -> dict[str, object] | None:
        sessions = state.get("sessions", [])
        if not isinstance(sessions, list):
            return None
        return next(
            (
                item
                for item in sessions
                if isinstance(item, dict) and str(item.get("id") or "") == session_id
            ),
            None,
        )

    def _public_session(
        self,
        session: dict[str, object] | None,
    ) -> dict[str, object] | None:
        if not session:
            return None
        session_id = str(session.get("id") or "")
        return {
            "id": session_id,
            "status": str(session.get("status") or "inactive"),
            "created_at": str(session.get("created_at") or ""),
            "activated_at": str(session.get("activated_at") or ""),
            "last_opened_at": str(session.get("last_opened_at") or ""),
            "profile_ready": self._profile_dir(session_id).is_dir(),
        }

    def status(self) -> dict[str, object]:
        state = self._read_state()
        active_id = str(state.get("active_session_id") or "")
        pending_id = str(state.get("pending_session_id") or "")
        active = self._session(state, active_id) if active_id else None
        pending = self._session(state, pending_id) if pending_id else None
        chrome = self._find_chrome()
        return {
            "configured": bool(active and self._profile_dir(active_id).is_dir()),
            "chrome_available": chrome is not None,
            "active": self._public_session(active),
            "pending": self._public_session(pending),
            "flow_url": GOOGLE_FLOW_URL,
            "storage_mode": "isolated_chrome_profile",
        }

    def _launch_process(self, chrome: Path, profile_dir: Path) -> None:
        profile_dir.mkdir(parents=True, exist_ok=True)
        if os.name != "nt" or not hasattr(os, "startfile"):
            raise BrowserSessionError(
                "Google Flow browser session hiện chỉ hỗ trợ mở Chrome trên Windows"
            )
        arguments = (
            f'--user-data-dir="{profile_dir}" '
            '--profile-directory=Default '
            '--new-window '
            '--no-first-run '
            '--no-default-browser-check '
            f'"{GOOGLE_FLOW_URL}"'
        )
        # Verified Chrome executable + fixed internal args; no user-controlled command text.
        os.startfile(str(chrome), arguments=arguments)  # nosec B606

    def _open(self, session_id: str) -> dict[str, object]:
        state = self._read_state()
        session = self._session(state, session_id)
        if not session:
            raise BrowserSessionError("Không tìm thấy phiên Google Flow")
        chrome = self._find_chrome()
        if chrome is None:
            raise BrowserSessionError("Không tìm thấy Google Chrome trên máy")
        profile_dir = self._profile_dir(session_id)
        profile_dir.mkdir(parents=True, exist_ok=True)
        self._launcher(chrome, profile_dir)
        session["last_opened_at"] = self._now()
        self._write_state(state)
        return self.status()

    def active_profile_dir(self) -> Path:
        state = self._read_state()
        session_id = str(state.get("active_session_id") or "")
        if not session_id:
            raise BrowserSessionError("Chưa có phiên Google Flow đang hoạt động")
        profile_dir = self._profile_dir(session_id)
        if not profile_dir.is_dir():
            raise BrowserSessionError("Chrome profile của phiên Google Flow không còn tồn tại")
        return profile_dir

    def active_session_id(self) -> str:
        state = self._read_state()
        session_id = str(state.get("active_session_id") or "")
        if not session_id:
            raise BrowserSessionError("Chưa có phiên Google Flow đang hoạt động")
        return session_id

    def chrome_path(self) -> Path:
        chrome = self._find_chrome()
        if chrome is None:
            raise BrowserSessionError("Không tìm thấy Google Chrome trên máy")
        return chrome

    @staticmethod
    def _port_open(port: int) -> bool:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return True
        except OSError:
            return False

    @staticmethod
    def _process_command_line(process: psutil.Process) -> str:
        try:
            return " ".join(process.cmdline())
        except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
            return ""

    def _profile_chrome_processes(self, profile_dir: Path) -> list[psutil.Process]:
        profile_token = str(profile_dir).lower()
        matches: list[psutil.Process] = []
        for process in psutil.process_iter(["name"]):
            try:
                if (process.info.get("name") or "").lower() != "chrome.exe":
                    continue
                if profile_token in self._process_command_line(process).lower():
                    matches.append(process)
            except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
                continue
        return matches

    def automation_browser_ready(self, debug_port: int = 9333) -> bool:
        profile_dir = self.active_profile_dir()
        marker = f"--remote-debugging-port={debug_port}"
        return self._port_open(debug_port) and any(
            marker in self._process_command_line(process)
            for process in self._profile_chrome_processes(profile_dir)
        )

    def ensure_automation_browser(
        self,
        *,
        debug_port: int = 9333,
        timeout_seconds: float = 12.0,
    ) -> dict[str, object]:
        """Run the active TH Media Chrome profile in off-screen CDP automation mode."""
        profile_dir = self.active_profile_dir()
        if self.automation_browser_ready(debug_port):
            return {
                "ready": True,
                "debug_port": debug_port,
                "profile_dir": str(profile_dir),
                "restarted": False,
            }

        for process in self._profile_chrome_processes(profile_dir):
            try:
                process.terminate()
            except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
                continue
        gone, alive = psutil.wait_procs(
            self._profile_chrome_processes(profile_dir),
            timeout=4.0,
        )
        del gone
        for process in alive:
            try:
                process.kill()
            except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
                continue

        chrome = self.chrome_path()
        arguments = (
            f'--user-data-dir="{profile_dir}" '
            '--profile-directory=Default '
            f'--remote-debugging-port={debug_port} '
            '--remote-allow-origins=* '
            '--window-position=-32000,-32000 '
            '--window-size=1440,1000 '
            '--no-first-run '
            '--no-default-browser-check '
            '"https://flow.google.com/"'
        )
        if os.name != "nt" or not hasattr(os, "startfile"):
            raise BrowserSessionError(
                "Google Flow browser automation hiện chỉ hỗ trợ Chrome trên Windows"
            )
        os.startfile(str(chrome), arguments=arguments)  # nosec B606

        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if self.automation_browser_ready(debug_port):
                state = self._read_state()
                session = self._session(state, self.active_session_id())
                if session:
                    session["last_opened_at"] = self._now()
                    self._write_state(state)
                return {
                    "ready": True,
                    "debug_port": debug_port,
                    "profile_dir": str(profile_dir),
                    "restarted": True,
                }
            time.sleep(0.2)
        raise BrowserSessionError(
            f"Chrome automation không mở được cổng localhost:{debug_port}"
        )

    def open_active(self) -> dict[str, object]:
        state = self._read_state()
        session_id = str(state.get("active_session_id") or "")
        if not session_id:
            raise BrowserSessionError("Chưa có phiên Google Flow đang hoạt động")
        return self._open(session_id)

    def open_pending(self) -> dict[str, object]:
        state = self._read_state()
        session_id = str(state.get("pending_session_id") or "")
        if not session_id:
            raise BrowserSessionError("Không có phiên đăng nhập mới đang chờ")
        return self._open(session_id)

    def create_new(self) -> dict[str, object]:
        state = self._read_state()
        if str(state.get("pending_session_id") or ""):
            raise BrowserSessionError(
                "Đã có một phiên mới đang chờ xác nhận; hãy xác nhận hoặc hủy phiên đó trước"
            )
        chrome = self._find_chrome()
        if chrome is None:
            raise BrowserSessionError("Không tìm thấy Google Chrome trên máy")
        session_id = uuid4().hex[:12]
        record: dict[str, object] = {
            "id": session_id,
            "status": "pending",
            "created_at": self._now(),
            "activated_at": "",
            "last_opened_at": "",
        }
        sessions = state.setdefault("sessions", [])
        if not isinstance(sessions, list):
            sessions = []
            state["sessions"] = sessions
        sessions.append(record)
        state["pending_session_id"] = session_id
        self._write_state(state)
        try:
            return self._open(session_id)
        except Exception:
            state = self._read_state()
            state["pending_session_id"] = ""
            session = self._session(state, session_id)
            if session:
                session["status"] = "failed"
            self._write_state(state)
            raise

    def activate_pending(self) -> dict[str, object]:
        state = self._read_state()
        pending_id = str(state.get("pending_session_id") or "")
        if not pending_id:
            raise BrowserSessionError("Không có phiên đăng nhập mới để xác nhận")
        pending = self._session(state, pending_id)
        if not pending:
            raise BrowserSessionError("Dữ liệu phiên đăng nhập mới không còn hợp lệ")

        active_id = str(state.get("active_session_id") or "")
        if active_id and active_id != pending_id:
            active = self._session(state, active_id)
            if active:
                active["status"] = "inactive"

        pending["status"] = "active"
        pending["activated_at"] = self._now()
        state["active_session_id"] = pending_id
        state["pending_session_id"] = ""
        self._write_state(state)
        return self.status()

    def cancel_pending(self) -> dict[str, object]:
        state = self._read_state()
        pending_id = str(state.get("pending_session_id") or "")
        if not pending_id:
            return self.status()
        pending = self._session(state, pending_id)
        if pending:
            pending["status"] = "cancelled"
        state["pending_session_id"] = ""
        self._write_state(state)
        return self.status()
