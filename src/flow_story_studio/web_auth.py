"""Single-user web authentication for the server deployment."""

# ruff: noqa: E501

from __future__ import annotations

import hmac
import os
import secrets
import threading
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

COOKIE_NAME = "th_media_web_session"


def _truthy(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _secret_from_env(name: str, file_name: str) -> str:
    value = os.getenv(name, "").strip()
    if value:
        return value
    path = os.getenv(file_name, "").strip()
    if not path:
        return ""
    try:
        return Path(path).expanduser().read_text(encoding="utf-8").strip()
    except OSError:
        return ""


class WebAuth:
    def __init__(self) -> None:
        password = _secret_from_env("TH_MEDIA_WEB_PASSWORD", "TH_MEDIA_WEB_PASSWORD_FILE")
        explicit = _truthy(os.getenv("TH_MEDIA_WEB_AUTH", ""))
        self.enabled = explicit or bool(password)
        self.username = os.getenv("TH_MEDIA_WEB_USERNAME", "admin").strip() or "admin"
        self.password = password
        if self.enabled and not self.password:
            raise RuntimeError(
                "TH_MEDIA_WEB_AUTH bật nhưng chưa cấu hình TH_MEDIA_WEB_PASSWORD(_FILE)"
            )
        self.secure_cookie = _truthy(os.getenv("TH_MEDIA_COOKIE_SECURE", "0"))
        self._sessions: set[str] = set()
        self._lock = threading.Lock()

    def verify(self, username: str, password: str) -> bool:
        return hmac.compare_digest(username, self.username) and hmac.compare_digest(
            password, self.password
        )

    def issue(self) -> str:
        token = secrets.token_urlsafe(48)
        with self._lock:
            self._sessions.add(token)
            if len(self._sessions) > 16:
                self._sessions = set(list(self._sessions)[-16:])
        return token

    def valid(self, token: str) -> bool:
        if not token:
            return False
        with self._lock:
            return token in self._sessions

    def revoke(self, token: str) -> None:
        with self._lock:
            self._sessions.discard(token)


LOGIN_HTML = """<!doctype html>
<html lang="vi"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>TH Media · Đăng nhập</title><style>
:root{font-family:Inter,Segoe UI,sans-serif;color-scheme:dark;background:#071018;color:#eef7fb}
*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;background:radial-gradient(circle at 50% 10%,#12384a,#071018 55%,#03070a)}
main{width:min(430px,calc(100vw - 32px));padding:32px;border:1px solid #2e3b46;border-radius:16px;background:#0d151ddd;box-shadow:0 30px 90px #0008}
small{color:#67e8f9;letter-spacing:.15em;font-weight:700}h1{margin:10px 0 8px}p{color:#98a6b5;line-height:1.5}
label{display:block;margin:16px 0 6px;color:#b8c4cf;font-size:12px}input{width:100%;padding:12px;border:1px solid #344553;border-radius:8px;background:#081018;color:#fff}
button{margin-top:20px;width:100%;padding:12px;border:0;border-radius:8px;background:#67e8f9;color:#05202a;font-weight:800;cursor:pointer}
#error{min-height:18px;margin-top:12px;color:#ff9d8f;font-size:12px}
</style></head><body><main><small>TH MEDIA · WEB</small><h1>Đăng nhập</h1>
<p>Phiên web riêng tư để quản lý pipeline sản xuất phim.</p>
<form id="f"><label>Tài khoản</label><input id="u" autocomplete="username" value="admin">
<label>Mật khẩu</label><input id="p" type="password" autocomplete="current-password">
<button>Đăng nhập</button><div id="error"></div></form>
<script>f.onsubmit=async(e)=>{e.preventDefault();error.textContent='';const r=await fetch('/api/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:u.value,password:p.value})});if(r.ok)location.href='/';else error.textContent='Thông tin đăng nhập không đúng.'}</script>
</main></body></html>"""


def install_web_auth(app: FastAPI) -> WebAuth:
    auth = WebAuth()
    app.state.web_auth = auth
    if not auth.enabled:
        return auth

    public = {"/login", "/api/health", "/api/auth/login", "/api/auth/status"}

    @app.middleware("http")
    async def web_auth_middleware(request: Request, call_next):
        path = request.url.path
        if path in public or path.startswith("/assets/"):
            return await call_next(request)
        token = request.cookies.get(COOKIE_NAME, "")
        if auth.valid(token):
            return await call_next(request)
        if path.startswith("/api/"):
            return JSONResponse(status_code=401, content={"detail": "Web session required"})
        return RedirectResponse("/login", status_code=303)

    @app.get("/login", include_in_schema=False)
    async def login_page() -> HTMLResponse:
        return HTMLResponse(LOGIN_HTML)

    @app.get("/api/auth/status")
    async def auth_status(request: Request) -> dict[str, object]:
        token = request.cookies.get(COOKIE_NAME, "")
        return {"enabled": True, "authenticated": auth.valid(token), "username": auth.username}

    @app.post("/api/auth/login")
    async def auth_login(request: Request):
        payload = await request.json()
        username = str(payload.get("username") or "")
        password = str(payload.get("password") or "")
        if not auth.verify(username, password):
            return JSONResponse(status_code=401, content={"detail": "Invalid credentials"})
        token = auth.issue()
        response = JSONResponse({"ok": True, "username": auth.username})
        response.set_cookie(
            COOKIE_NAME,
            token,
            httponly=True,
            secure=auth.secure_cookie,
            samesite="strict",
            path="/",
            max_age=12 * 60 * 60,
        )
        return response

    @app.delete("/api/auth/session")
    async def auth_logout(request: Request):
        token = request.cookies.get(COOKIE_NAME, "")
        auth.revoke(token)
        response = JSONResponse({"ok": True})
        response.delete_cookie(COOKIE_NAME, path="/")
        return response

    return auth
