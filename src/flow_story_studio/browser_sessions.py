# ruff: noqa: E501

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import psutil

from .credentials import CredentialVaultError, EncryptedCredentialVault

GOOGLE_FLOW_URL = "https://flow.google.com/"
_ALLOWED_SUFFIXES = ("google.com", "googleusercontent.com", "googleapis.com")
_SESSION_SOURCES = {"google_account", "google_flow"}
_PROXY_SCHEMES = {"http", "https", "socks5"}


class BrowserSessionError(RuntimeError):
    pass


def _allowed_host(value: str) -> bool:
    host = value.strip().lower().lstrip(".")
    return any(host == suffix or host.endswith("." + suffix) for suffix in _ALLOWED_SUFFIXES)


def _flow_host(value: str) -> bool:
    host = value.strip().lower().lstrip(".")
    return host == "flow.google.com" or host.endswith(".flow.google.com")


def _account_host(value: str) -> bool:
    host = value.strip().lower().lstrip(".")
    return _allowed_host(host) and not _flow_host(host)


class GoogleFlowSessionManager:
    """Server-side Google Flow authentication using imported browser session state."""

    def __init__(
        self,
        root: Path,
        *,
        chrome_path: Path | None = None,
        launcher=None,
        credential_path: Path | None = None,
    ) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._chrome_path = chrome_path
        self._launcher = launcher or self._launch_process
        flow_credential_path = (
            credential_path or (self.root / "google-flow-session.bin")
        ).resolve()
        self._flow_vault = EncryptedCredentialVault(flow_credential_path)
        self._account_vault = EncryptedCredentialVault(
            flow_credential_path.with_name("google-account-session.bin")
        )
        self._proxy_vault = EncryptedCredentialVault(
            flow_credential_path.with_name("google-flow-proxy.bin")
        )
        self._proxy_auth_sessions: dict[int, Any] = {}
        self._proxy_bridge_pid_path = self.root / "proxy-bridge.pid"
        self._proxy_bridge_port_path = self.root / "proxy-bridge.port"
        # Backward-compatibility alias for older internal callers.
        self._vault = self._flow_vault
        self.profile_dir = self.root / "runtime-profile"
        self.profile_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _normalize_proxy_payload(payload: dict[str, Any]) -> dict[str, Any]:
        server = str(payload.get("server") or payload.get("url") or "").strip()
        if not server:
            raise BrowserSessionError("Hãy nhập proxy URL")

        if "://" not in server:
            server = "http://" + server
        try:
            parsed = urlparse(server)
            port = parsed.port
        except ValueError as exc:
            raise BrowserSessionError("Proxy port không hợp lệ") from exc

        scheme = parsed.scheme.lower()
        if scheme == "sock5":
            scheme = "socks5"
        if scheme not in _PROXY_SCHEMES:
            raise BrowserSessionError("Proxy chỉ hỗ trợ http://, https:// hoặc socks5://")
        host = (parsed.hostname or "").strip()
        if not host or port is None or port < 1 or port > 65535:
            raise BrowserSessionError("Proxy phải có host và port hợp lệ")
        if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise BrowserSessionError("Proxy URL không được chứa path/query/fragment")

        inline_username = unquote(parsed.username or "")
        inline_password = unquote(parsed.password or "")
        username = str(payload.get("username") or inline_username).strip()
        password = str(payload.get("password") or inline_password)
        if bool(username) != bool(password):
            raise BrowserSessionError(
                "Proxy auth phải có cả username và password hoặc để trống cả hai"
            )
        return {
            "version": 1,
            "scheme": scheme,
            "host": host,
            "port": int(port),
            "username": username,
            "password": password,
            "updated_at": time.time(),
        }

    def _load_proxy_config(self) -> dict[str, Any]:
        try:
            payload = self._proxy_vault.load()
        except CredentialVaultError as exc:
            raise BrowserSessionError(str(exc)) from exc
        if not payload:
            return {}
        try:
            scheme = str(payload.get("scheme") or "").lower()
            host = str(payload.get("host") or "").strip()
            port = int(payload.get("port") or 0)
        except (TypeError, ValueError) as exc:
            raise BrowserSessionError("Proxy vault không hợp lệ; hãy lưu lại proxy") from exc
        if scheme not in _PROXY_SCHEMES or not host or not (1 <= port <= 65535):
            raise BrowserSessionError("Proxy vault không hợp lệ; hãy lưu lại proxy")
        return {
            "version": 1,
            "scheme": scheme,
            "host": host,
            "port": port,
            "username": str(payload.get("username") or ""),
            "password": str(payload.get("password") or ""),
        }

    @staticmethod
    def _proxy_endpoint(config: dict[str, Any]) -> str:
        if not config:
            return ""
        return f"{config['scheme']}://{config['host']}:{config['port']}"

    def proxy_status(self) -> dict[str, object]:
        try:
            config = self._load_proxy_config()
        except BrowserSessionError as exc:
            return {
                "proxy_configured": False,
                "proxy_scheme": "",
                "proxy_endpoint": "",
                "proxy_auth_configured": False,
                "proxy_error": str(exc),
            }
        return {
            "proxy_configured": bool(config),
            "proxy_scheme": str(config.get("scheme") or ""),
            "proxy_endpoint": self._proxy_endpoint(config),
            "proxy_auth_configured": bool(config.get("username") and config.get("password")),
            "proxy_error": "",
        }

    def save_proxy(self, payload: dict[str, Any]) -> dict[str, object]:
        config = self._normalize_proxy_payload(payload)
        try:
            self._proxy_vault.save(config)
        except CredentialVaultError as exc:
            raise BrowserSessionError(str(exc)) from exc
        self._stop_profile_processes()
        self._proxy_auth_sessions.clear()
        return self.status()

    def clear_proxy(self) -> dict[str, object]:
        try:
            self._proxy_vault.clear()
        except CredentialVaultError as exc:
            raise BrowserSessionError(str(exc)) from exc
        self._stop_profile_processes()
        self._proxy_auth_sessions.clear()
        return self.status()

    def _proxy_bridge_process(self) -> psutil.Process | None:
        try:
            pid = int(self._proxy_bridge_pid_path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return None
        try:
            process = psutil.Process(pid)
            command = self._process_command_line(process)
        except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
            return None
        if "flow_story_studio.socks5_bridge" not in command:
            return None
        return process

    def _stop_proxy_bridge(self) -> None:
        process = self._proxy_bridge_process()
        if process is not None:
            try:
                process.terminate()
                process.wait(timeout=3.0)
            except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.TimeoutExpired):
                try:
                    process.kill()
                except (psutil.AccessDenied, psutil.NoSuchProcess):
                    pass
        self._proxy_bridge_pid_path.unlink(missing_ok=True)
        self._proxy_bridge_port_path.unlink(missing_ok=True)

    @staticmethod
    def _free_loopback_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])

    def _start_socks5_auth_bridge(self, config: dict[str, Any]) -> str:
        process = self._proxy_bridge_process()
        if process is not None:
            try:
                port = int(self._proxy_bridge_port_path.read_text(encoding="utf-8").strip())
            except (OSError, ValueError):
                port = 0
            if port and self._port_open(port):
                return f"socks5://127.0.0.1:{port}"
            self._stop_proxy_bridge()

        port = self._free_loopback_port()
        log_path = self.root / "proxy-bridge.log"
        log = open(log_path, "ab", buffering=0)
        try:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "flow_story_studio.socks5_bridge",
                    "--listen-host",
                    "127.0.0.1",
                    "--listen-port",
                    str(port),
                ],
                stdin=subprocess.PIPE,
                stdout=log,
                stderr=log,
                text=True,
                start_new_session=True,
            )
            payload = {
                "upstream_host": str(config["host"]),
                "upstream_port": int(config["port"]),
                "username": str(config.get("username") or ""),
                "password": str(config.get("password") or ""),
            }
            assert process.stdin is not None
            process.stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
            process.stdin.flush()
            process.stdin.close()
        finally:
            log.close()

        self._proxy_bridge_pid_path.write_text(str(process.pid), encoding="utf-8")
        self._proxy_bridge_port_path.write_text(str(port), encoding="utf-8")

        deadline = time.monotonic() + 6.0
        while time.monotonic() < deadline:
            if process.poll() is not None:
                self._stop_proxy_bridge()
                raise BrowserSessionError("SOCKS5 auth bridge không khởi động được")
            if self._port_open(port):
                return f"socks5://127.0.0.1:{port}"
            time.sleep(0.1)

        self._stop_proxy_bridge()
        raise BrowserSessionError("SOCKS5 auth bridge không mở được cổng localhost")

    def _proxy_launch_server(self) -> str:
        config = self._load_proxy_config()
        if not config:
            self._stop_proxy_bridge()
            return ""
        if config.get("scheme") == "socks5" and config.get("username") and config.get("password"):
            return self._start_socks5_auth_bridge(config)
        self._stop_proxy_bridge()
        return self._proxy_endpoint(config)

    def install_proxy_auth(self, page: Any) -> None:
        config = self._load_proxy_config()
        if not config or not config.get("username"):
            return
        if config.get("scheme") == "socks5":
            return
        if config.get("scheme") not in {"http", "https"}:
            raise BrowserSessionError("Proxy auth chỉ hỗ trợ HTTP/HTTPS trong Chromium worker")
        page_key = id(page)
        if page_key in self._proxy_auth_sessions:
            return

        try:
            session = page.context.new_cdp_session(page)
            session.send("Fetch.enable", {"handleAuthRequests": True})

            def continue_request(params: dict[str, Any]) -> None:
                request_id = params.get("requestId")
                if request_id:
                    try:
                        session.send("Fetch.continueRequest", {"requestId": request_id})
                    except Exception:
                        pass

            def handle_auth(params: dict[str, Any]) -> None:
                request_id = params.get("requestId")
                if not request_id:
                    return
                challenge = params.get("authChallenge") or {}
                if str(challenge.get("source") or "").lower() == "proxy":
                    response = {
                        "response": "ProvideCredentials",
                        "username": str(config["username"]),
                        "password": str(config["password"]),
                    }
                else:
                    response = {"response": "Default"}
                try:
                    session.send(
                        "Fetch.continueWithAuth",
                        {
                            "requestId": request_id,
                            "authChallengeResponse": response,
                        },
                    )
                except Exception:
                    pass

            session.on("Fetch.requestPaused", continue_request)
            session.on("Fetch.authRequired", handle_auth)
            self._proxy_auth_sessions[page_key] = session
        except Exception as exc:
            raise BrowserSessionError("Không thể bật proxy authentication trên Chromium") from exc

    def _find_chrome(self) -> Path | None:
        if self._chrome_path and self._chrome_path.is_file():
            return self._chrome_path
        configured = os.getenv("TH_MEDIA_CHROME_PATH", "").strip()
        candidates: list[Path] = []
        if configured:
            candidates.append(Path(configured))
        if os.name == "nt":
            local = os.getenv("LOCALAPPDATA", "")
            if local:
                candidates.append(Path(local) / "Google/Chrome/Application/chrome.exe")
            candidates.extend(
                [
                    Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
                    Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
                ]
            )
        for name in (
            "google-chrome",
            "google-chrome-stable",
            "chromium",
            "chromium-browser",
            "chrome",
            "chrome.exe",
        ):
            found = shutil.which(name)
            if found:
                candidates.append(Path(found))
        return next((item for item in candidates if item.is_file()), None)

    def chrome_path(self) -> Path:
        chrome = self._find_chrome()
        if chrome is None:
            raise BrowserSessionError("Không tìm thấy Chromium/Google Chrome trên máy chủ")
        return chrome

    @staticmethod
    def _normalize_cookie(item: dict[str, Any]) -> dict[str, Any] | None:
        name = str(item.get("name") or "").strip()
        value = str(item.get("value") or "")
        domain = str(item.get("domain") or "").strip()
        if not domain and item.get("url"):
            domain = urlparse(str(item["url"])).hostname or ""
        if not name or not domain or not _allowed_host(domain):
            return None
        cookie: dict[str, Any] = {
            "name": name,
            "value": value,
            "domain": domain,
            "path": str(item.get("path") or "/"),
            "httpOnly": bool(item.get("httpOnly", False)),
            "secure": bool(item.get("secure", True)),
        }
        expires = item.get("expires", item.get("expirationDate"))
        if isinstance(expires, (int, float)) and expires > 0:
            cookie["expires"] = float(expires)
        same_site = str(item.get("sameSite") or "").strip().lower()
        if same_site in {"strict", "lax", "none"}:
            cookie["sameSite"] = same_site.title()
        return cookie

    @staticmethod
    def _normalize_origins(
        items: object,
        *,
        source: str | None = None,
    ) -> list[dict[str, Any]]:
        if not isinstance(items, list):
            return []
        result: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            origin = str(item.get("origin") or "").strip()
            parsed = urlparse(origin)
            if parsed.scheme != "https" or not parsed.hostname:
                continue
            if source == "google_flow" and not _flow_host(parsed.hostname):
                continue
            if source == "google_account" and not _account_host(parsed.hostname):
                continue
            if source is None and not _allowed_host(parsed.hostname):
                continue
            local = item.get("localStorage")
            if not isinstance(local, list):
                local = []
            clean = []
            for pair in local:
                if isinstance(pair, dict) and "name" in pair and "value" in pair:
                    clean.append({"name": str(pair["name"]), "value": str(pair["value"])})
            result.append({"origin": origin, "localStorage": clean})
        return result

    @staticmethod
    def _source_accepts_domain(source: str, domain: str) -> bool:
        if source == "google_flow":
            return _flow_host(domain)
        if source == "google_account":
            return _account_host(domain)
        raise BrowserSessionError(f"Session source không hợp lệ: {source}")

    def _state_from_payload(
        self,
        payload: dict[str, Any],
        *,
        source: str,
    ) -> dict[str, Any]:
        if source not in _SESSION_SOURCES:
            raise BrowserSessionError(f"Session source không hợp lệ: {source}")
        state = (
            payload.get("storage_state")
            if isinstance(payload.get("storage_state"), dict)
            else payload
        )
        cookies_raw = state.get("cookies") if isinstance(state, dict) else None
        if not isinstance(cookies_raw, list):
            raise BrowserSessionError(
                "Session phải chứa cookies hoặc Playwright storage_state hợp lệ"
            )
        cookies = []
        for item in cookies_raw:
            if not isinstance(item, dict):
                continue
            clean = self._normalize_cookie(item)
            if clean is None:
                continue
            if self._source_accepts_domain(source, str(clean["domain"])):
                cookies.append(clean)
        if not cookies:
            label = "Google Account" if source == "google_account" else "Google Flow"
            raise BrowserSessionError(f"Không tìm thấy cookie {label} hợp lệ trong JSON đã nhập")
        return {
            "version": 2,
            "source": source,
            "cookies": cookies,
            "origins": self._normalize_origins(
                state.get("origins") if isinstance(state, dict) else [],
                source=source,
            ),
            "imported_at": time.time(),
        }

    @staticmethod
    def _safe_vault_load(vault: EncryptedCredentialVault) -> dict[str, Any]:
        state = vault.load()
        return state if isinstance(state, dict) else {}

    def _migrate_legacy_combined_vault(self) -> None:
        account = self._safe_vault_load(self._account_vault)
        flow = self._safe_vault_load(self._flow_vault)
        if not flow or flow.get("source") in _SESSION_SOURCES:
            return

        cookies = flow.get("cookies", [])
        origins = flow.get("origins", [])
        account_cookies = [
            item
            for item in cookies
            if isinstance(item, dict) and _account_host(str(item.get("domain") or ""))
        ]
        flow_cookies = [
            item
            for item in cookies
            if isinstance(item, dict) and _flow_host(str(item.get("domain") or ""))
        ]
        account_origins = self._normalize_origins(origins, source="google_account")
        flow_origins = self._normalize_origins(origins, source="google_flow")

        if account_cookies and not account.get("cookies"):
            self._account_vault.save(
                {
                    "version": 2,
                    "source": "google_account",
                    "cookies": account_cookies,
                    "origins": account_origins,
                    "imported_at": flow.get("imported_at", time.time()),
                }
            )
        if flow_cookies:
            self._flow_vault.save(
                {
                    "version": 2,
                    "source": "google_flow",
                    "cookies": flow_cookies,
                    "origins": flow_origins,
                    "imported_at": flow.get("imported_at", time.time()),
                }
            )
        else:
            self._flow_vault.clear()

    def _save_source_session(
        self,
        payload: dict[str, Any],
        *,
        source: str,
    ) -> dict[str, object]:
        session = self._state_from_payload(payload, source=source)
        vault = self._account_vault if source == "google_account" else self._flow_vault
        try:
            vault.save(session)
        except CredentialVaultError as exc:
            raise BrowserSessionError(str(exc)) from exc
        self._reset_runtime_profile()
        return self.status()

    def import_google_account_session(
        self,
        payload: dict[str, Any],
    ) -> dict[str, object]:
        return self._save_source_session(payload, source="google_account")

    def import_flow_session(
        self,
        payload: dict[str, Any],
    ) -> dict[str, object]:
        return self._save_source_session(payload, source="google_flow")

    def import_session(self, payload: dict[str, Any]) -> dict[str, object]:
        """Backward-compatible combined import that splits cookies by domain."""
        state = (
            payload.get("storage_state")
            if isinstance(payload.get("storage_state"), dict)
            else payload
        )
        cookies_raw = state.get("cookies") if isinstance(state, dict) else None
        if not isinstance(cookies_raw, list):
            raise BrowserSessionError(
                "Session phải chứa cookies hoặc Playwright storage_state hợp lệ"
            )
        imported = False
        for source, vault in (
            ("google_account", self._account_vault),
            ("google_flow", self._flow_vault),
        ):
            try:
                session = self._state_from_payload(payload, source=source)
            except BrowserSessionError:
                continue
            vault.save(session)
            imported = True
        if not imported:
            raise BrowserSessionError("Không tìm thấy cookie Google hợp lệ trong session")
        self._reset_runtime_profile()
        return self.status()

    def clear_source_session(self, source: str) -> dict[str, object]:
        if source not in _SESSION_SOURCES:
            raise BrowserSessionError(f"Session source không hợp lệ: {source}")
        vault = self._account_vault if source == "google_account" else self._flow_vault
        try:
            vault.clear()
        except CredentialVaultError as exc:
            raise BrowserSessionError(str(exc)) from exc
        self._reset_runtime_profile()
        return self.status()

    def clear_session(self) -> dict[str, object]:
        try:
            self._account_vault.clear()
            self._flow_vault.clear()
        except CredentialVaultError as exc:
            raise BrowserSessionError(str(exc)) from exc
        self._reset_runtime_profile()
        return self.status()

    @staticmethod
    def _merge_session_states(
        account: dict[str, Any],
        flow: dict[str, Any],
    ) -> dict[str, Any]:
        merged_cookies: dict[tuple[str, str, str], dict[str, Any]] = {}
        for state in (account, flow):
            for cookie in state.get("cookies", []):
                if not isinstance(cookie, dict):
                    continue
                key = (
                    str(cookie.get("domain") or ""),
                    str(cookie.get("path") or "/"),
                    str(cookie.get("name") or ""),
                )
                merged_cookies[key] = cookie

        merged_origins: dict[str, dict[str, Any]] = {}
        for state in (account, flow):
            for origin in state.get("origins", []):
                if isinstance(origin, dict) and origin.get("origin"):
                    merged_origins[str(origin["origin"])] = origin
        return {
            "version": 2,
            "source": "merged",
            "cookies": list(merged_cookies.values()),
            "origins": list(merged_origins.values()),
        }

    def _load_session(self) -> dict[str, Any]:
        try:
            self._migrate_legacy_combined_vault()
            account = self._safe_vault_load(self._account_vault)
            flow = self._safe_vault_load(self._flow_vault)
        except CredentialVaultError as exc:
            raise BrowserSessionError(str(exc)) from exc
        account_count = len(account.get("cookies", []))
        flow_count = len(flow.get("cookies", []))
        if not account_count:
            raise BrowserSessionError("Chưa import Google Account session")
        if not flow_count:
            raise BrowserSessionError("Chưa import Google Flow session")
        return self._merge_session_states(account, flow)

    def status(self) -> dict[str, object]:
        chrome = self._find_chrome()
        account_count = 0
        flow_count = 0
        account_refreshed_at = 0.0
        flow_refreshed_at = 0.0
        error = ""
        try:
            self._migrate_legacy_combined_vault()
            account = self._safe_vault_load(self._account_vault)
            flow = self._safe_vault_load(self._flow_vault)
            account_count = len(account.get("cookies", []))
            flow_count = len(flow.get("cookies", []))
            account_refreshed_at = float(account.get("refreshed_at") or 0.0)
            flow_refreshed_at = float(flow.get("refreshed_at") or 0.0)
        except CredentialVaultError as exc:
            error = str(exc)
        account_ready = account_count > 0
        flow_ready = flow_count > 0
        configured = account_ready and flow_ready
        total = account_count + flow_count
        proxy = self.proxy_status()
        return {
            "configured": configured,
            "chrome_available": chrome is not None,
            "auth_mode": "split_session_import",
            "storage_mode": "dual_encrypted_session_vault",
            "session_present": total > 0,
            "cookie_count": total,
            "google_account_configured": account_ready,
            "google_flow_configured": flow_ready,
            "google_account_cookie_count": account_count,
            "google_flow_cookie_count": flow_count,
            "runtime_session_refreshed_at": max(
                account_refreshed_at,
                flow_refreshed_at,
            ),
            "profile_ready": self.profile_dir.is_dir(),
            "flow_url": GOOGLE_FLOW_URL,
            "error": error,
            "active": (
                {
                    "id": "split-session",
                    "status": "active" if configured else "partial",
                }
                if total
                else None
            ),
            "pending": None,
            **proxy,
        }

    @staticmethod
    def _port_open(port: int) -> bool:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.25):
                return True
        except OSError:
            return False

    @staticmethod
    def _process_command_line(process: psutil.Process) -> str:
        try:
            return " ".join(process.cmdline())
        except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
            return ""

    def _profile_processes(self) -> list[psutil.Process]:
        token = str(self.profile_dir).lower()
        matches: list[psutil.Process] = []
        for process in psutil.process_iter(["name"]):
            try:
                name = str(process.info.get("name") or "").lower()
                command = self._process_command_line(process).lower()
                if token in command and ("chrom" in name or "chrom" in command):
                    matches.append(process)
            except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
                continue
        return matches

    def _stop_profile_processes(self) -> None:
        self._proxy_auth_sessions.clear()
        self._stop_proxy_bridge()
        processes = self._profile_processes()
        for process in processes:
            try:
                process.terminate()
            except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
                pass
        _, alive = psutil.wait_procs(processes, timeout=4.0)
        for process in alive:
            try:
                process.kill()
            except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
                pass

    def _reset_runtime_profile(self) -> None:
        self._stop_profile_processes()
        shutil.rmtree(self.profile_dir, ignore_errors=True)
        self.profile_dir.mkdir(parents=True, exist_ok=True)

    def automation_browser_ready(self, debug_port: int = 9333) -> bool:
        marker = f"--remote-debugging-port={debug_port}"
        return self._port_open(debug_port) and any(
            marker in self._process_command_line(process) for process in self._profile_processes()
        )

    def _chrome_launch_args(
        self,
        chrome: Path,
        profile_dir: Path,
        debug_port: int,
    ) -> list[str]:
        args = [
            str(chrome),
            f"--user-data-dir={profile_dir}",
            "--profile-directory=Default",
            f"--remote-debugging-port={debug_port}",
            "--remote-allow-origins=*",
            "--window-size=1440,1000",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-dev-shm-usage",
        ]
        proxy_server = self._proxy_launch_server()
        if proxy_server:
            args.append(f"--proxy-server={proxy_server}")
            args.append("--disable-quic")

        headless = os.getenv("TH_MEDIA_FLOW_HEADLESS", "").strip().lower()
        if headless not in {"0", "false", "no"} or (os.name != "nt" and not os.getenv("DISPLAY")):
            args.append("--headless=new")
        if os.getenv("TH_MEDIA_FLOW_NO_SANDBOX", "").strip().lower() in {
            "1",
            "true",
            "yes",
        }:
            args.append("--no-sandbox")
        args.append(GOOGLE_FLOW_URL)
        return args

    def _launch_process(self, chrome: Path, profile_dir: Path, debug_port: int) -> None:
        profile_dir.mkdir(parents=True, exist_ok=True)
        args = self._chrome_launch_args(chrome, profile_dir, debug_port)
        log_path = self.root / "chromium.log"
        log = open(log_path, "ab", buffering=0)
        try:
            subprocess.Popen(
                args,
                stdout=log,
                stderr=log,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
        finally:
            log.close()

    def ensure_automation_browser(
        self,
        *,
        debug_port: int = 9333,
        timeout_seconds: float = 15.0,
    ) -> dict[str, object]:
        self._load_session()
        if self.automation_browser_ready(debug_port):
            return {
                "ready": True,
                "debug_port": debug_port,
                "profile_dir": str(self.profile_dir),
                "restarted": False,
            }
        self._stop_profile_processes()
        self._launcher(self.chrome_path(), self.profile_dir, debug_port)
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if self.automation_browser_ready(debug_port):
                return {
                    "ready": True,
                    "debug_port": debug_port,
                    "profile_dir": str(self.profile_dir),
                    "restarted": True,
                }
            time.sleep(0.2)
        raise BrowserSessionError(f"Chromium automation không mở được cổng localhost:{debug_port}")

    @staticmethod
    def _merge_runtime_origins(
        existing: list[dict[str, Any]],
        runtime: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        merged: dict[str, dict[str, Any]] = {}
        for source in (existing, runtime):
            for item in source:
                if not isinstance(item, dict):
                    continue
                origin = str(item.get("origin") or "").strip()
                if not origin:
                    continue
                current = merged.get(origin, {"origin": origin, "localStorage": []})
                pairs: dict[str, str] = {
                    str(pair.get("name")): str(pair.get("value"))
                    for pair in current.get("localStorage", [])
                    if isinstance(pair, dict) and "name" in pair and "value" in pair
                }
                for pair in item.get("localStorage", []):
                    if isinstance(pair, dict) and "name" in pair and "value" in pair:
                        pairs[str(pair["name"])] = str(pair["value"])
                merged[origin] = {
                    "origin": origin,
                    "localStorage": [
                        {"name": name, "value": value} for name, value in sorted(pairs.items())
                    ],
                }
        return list(merged.values())

    def persist_runtime_session(self, browser: Any) -> dict[str, object]:
        """Persist Google session rotations from a proven runtime back into encrypted vaults.

        This intentionally does not reset or restart Chromium. A successful authenticated
        runtime is the freshest source of cookie/localStorage state; persisting it makes
        session continuity survive server/browser restarts.
        """
        if not browser.contexts:
            raise BrowserSessionError("Chromium automation không có browser context")
        context = browser.contexts[0]
        try:
            runtime_state = context.storage_state()
        except Exception as exc:
            raise BrowserSessionError("Không đọc được runtime storage state của Chromium") from exc
        if not isinstance(runtime_state, dict):
            raise BrowserSessionError("Chromium runtime storage state không hợp lệ")

        refreshed_at = time.time()
        updated_sources: list[str] = []
        for source, vault in (
            ("google_account", self._account_vault),
            ("google_flow", self._flow_vault),
        ):
            try:
                fresh = self._state_from_payload(runtime_state, source=source)
            except BrowserSessionError:
                continue
            try:
                existing = self._safe_vault_load(vault)
                fresh["origins"] = self._merge_runtime_origins(
                    self._normalize_origins(
                        existing.get("origins", []),
                        source=source,
                    ),
                    fresh.get("origins", []),
                )
                fresh["imported_at"] = existing.get("imported_at", refreshed_at)
                fresh["refreshed_at"] = refreshed_at
                vault.save(fresh)
            except CredentialVaultError as exc:
                raise BrowserSessionError(str(exc)) from exc
            updated_sources.append(source)

        if not updated_sources:
            raise BrowserSessionError(
                "Runtime Chromium không chứa Google session hợp lệ để đồng bộ"
            )
        return {
            **self.status(),
            "runtime_session_persisted": True,
            "runtime_session_sources": updated_sources,
            "runtime_session_refreshed_at": refreshed_at,
        }

    def apply_session(self, browser: Any) -> None:
        state = self._load_session()
        if not browser.contexts:
            raise BrowserSessionError("Chromium automation không có browser context")
        context = browser.contexts[0]
        context.add_cookies(state["cookies"])
        origin_values: dict[str, dict[str, str]] = {}
        for item in state.get("origins", []):
            if not isinstance(item, dict):
                continue
            pairs = item.get("localStorage", [])
            origin_values[str(item.get("origin") or "")] = {
                str(pair["name"]): str(pair["value"])
                for pair in pairs
                if isinstance(pair, dict) and "name" in pair and "value" in pair
            }
        if origin_values:
            payload = json.dumps(origin_values, ensure_ascii=False)
            context.add_init_script(
                f"""(() => {{
                    const all = {payload};
                    const values = all[location.origin];
                    if (values) for (const [k, v] of Object.entries(values)) localStorage.setItem(k, v);
                }})();"""
            )

    def validate_session(self, debug_port: int = 9333) -> dict[str, object]:
        self.ensure_automation_browser(debug_port=debug_port)
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise BrowserSessionError("Playwright chưa được cài trên máy chủ") from exc
        with sync_playwright() as playwright:
            browser = playwright.chromium.connect_over_cdp(
                f"http://127.0.0.1:{debug_port}", timeout=15_000
            )
            self.apply_session(browser)
            context = browser.contexts[0]
            page = context.pages[0] if context.pages else context.new_page()
            self.install_proxy_auth(page)
            page.goto(GOOGLE_FLOW_URL, wait_until="domcontentloaded", timeout=30_000)
            page.wait_for_timeout(1800)

            # Flow now exposes a public /about landing page even for anonymous users.
            # Validation must prove access to the authenticated application shell, not
            # merely that the public marketing page is reachable.
            if "flow.google.com/about" in page.url.lower():
                entry = page.get_by_role(
                    "button",
                    name="Create with Google Flow",
                    exact=True,
                )
                if not entry.count():
                    entry = page.get_by_role(
                        "button",
                        name="Try Google Flow",
                        exact=True,
                    )
                if not entry.count():
                    entry = page.get_by_role(
                        "button",
                        name="Try in Google Flow",
                        exact=True,
                    )
                if entry.count():
                    entry.first.click()
                    page.wait_for_timeout(2200)

            url = page.url
            title = page.title()
            lower = url.lower()
            is_signin = "accounts.google.com" in lower or "signin" in lower
            public_landing = "flow.google.com/about" in lower
            authenticated = not is_signin and not public_landing
            runtime_persisted = False
            runtime_persist_error = ""
            if authenticated:
                try:
                    self.persist_runtime_session(browser)
                    runtime_persisted = True
                except BrowserSessionError as exc:
                    runtime_persist_error = str(exc)
            return {
                **self.status(),
                "validated": authenticated,
                "runtime_session_persisted": runtime_persisted,
                "runtime_session_persist_error": runtime_persist_error,
                "current_url": url,
                "title": title,
                "message": (
                    "Google Flow app session hợp lệ."
                    if authenticated
                    else (
                        "Session chưa đăng nhập được Google Flow app; hãy import đầy đủ "
                        "cookie Google Account/Flow rồi kiểm tra lại."
                    )
                ),
            }

    # Legacy desktop actions are intentionally disabled in server session mode.
    def create_new(self) -> dict[str, object]:
        raise BrowserSessionError("Web mode dùng Import Session, không mở cửa sổ đăng nhập Chrome")

    def open_active(self) -> dict[str, object]:
        raise BrowserSessionError("Web mode không mở Chrome tương tác; dùng Validate Session")

    def open_pending(self) -> dict[str, object]:
        raise BrowserSessionError("Web mode không có pending Google Flow session")

    def activate_pending(self) -> dict[str, object]:
        raise BrowserSessionError("Web mode không có pending Google Flow session")

    def cancel_pending(self) -> dict[str, object]:
        return self.status()

    def active_profile_dir(self) -> Path:
        self._load_session()
        return self.profile_dir

    def active_session_id(self) -> str:
        self._load_session()
        return "imported-session"
