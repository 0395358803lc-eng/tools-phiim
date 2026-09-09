# Production Operations — TH Media

## Release gates

Một production release phải đi qua `scripts/release.ps1`:

1. Dependency integrity.
2. Ruff và JavaScript syntax.
3. Full pytest với branch coverage >= 70%.
4. Browser E2E của TH Media.
5. Bandit.
6. `pip-audit` và CycloneDX SBOM.
7. Third-party license report.
8. PyInstaller build.
9. EXE smoke test với `/api/health`.
10. Authenticode signing.
11. Inno Setup installer.
12. Installer signing.

Playwright Chromium chỉ phục vụ browser E2E trong dev/CI. Runtime renderer hiện không yêu cầu browser automation.

## External render provider

Core application hiện không bundle production render provider.

Mọi provider tương lai phải:
- thỏa `VideoProvider` contract;
- trả `RenderResult` generic;
- không đưa provider-specific transport vào RenderQueue;
- đi qua Visual QC, Audio QC, Continuity QC và Production Acceptance;
- chỉ được Final Merge sau khi scene đạt Accepted.

Khi chưa có renderer:
- application health vẫn `ok=true`;
- `GET /api/render/status` trả `configured=false`;
- analysis, project save/open, continuity và scene contract vẫn hoạt động;
- generate bị chặn có kiểm soát trước queue;
- QC/Acceptance không tự PASS;
- Final Merge không mở khi scene chưa Accepted.

## Data and backups

Mỗi workspace lưu project JSON trong `projects/` và backup trong `backups/<project-id>/`.

Project JSON có `schema_version`. Payload cũ được migration theo version; payload mới hơn runtime bị từ chối thay vì downgrade ngầm.

Schema migration hiện chuyển các field render legacy sang tên provider-neutral nhưng vẫn bảo đảm project cũ mở được.

## Credentials and Web API

Windows desktop vẫn dùng DPAPI. Linux/web dùng encrypted server vault; master key phải nằm ngoài Git và có quyền đọc giới hạn cho user chạy service.

Web mode dùng tài khoản single-user và HttpOnly SameSite session cookie. Khi auth web được bật, toàn bộ API/project/media bị chặn nếu chưa đăng nhập; `/api/health` và trang login là ngoại lệ cần cho healthcheck.

Header `X-Flow-Studio-Session` vẫn được giữ để compatibility với desktop loopback cũ. Web bind host/port qua biến runtime và chỉ được public qua HTTPS.

## Workspace concurrency

Workspace được bảo vệ bởi `.flow-story-studio.lock`. Đây là tên runtime legacy của ứng dụng và được giữ để tránh phá compatibility.

## Dependency reproducibility

`requirements.lock.txt` là constraints desktop Windows; `requirements-linux.lock.txt` ghi snapshot dependency đã nghiệm thu trên web server Linux hiện tại.

Setup production chỉ cài project dependencies thực sự. Không có vendored render CLI, cookie runtime hoặc browser profile trong release artifact.

## Media requirements

FFmpeg vẫn là dependency runtime cần thiết cho:
- extract frame;
- Visual QC;
- Audio QC;
- last-frame continuity;
- final merge.

Không được loại FFmpeg khi thay renderer.

## Release signing

Không phát hành production installer unsigned. `scripts/sign-artifact.ps1` dùng Windows SDK `signtool.exe`, SHA-256 digest và RFC3161 timestamp.

## Web deployment

Linux setup dùng `scripts/setup-web-linux.sh`; healthcheck dùng `scripts/healthcheck-web.sh`. Môi trường Replit/Nix hiện tại cần build `greenlet` từ source nếu wheel Playwright không nạp được C++ runtime.

Google Flow dùng Chromium headless/CDP trên server và encrypted imported session. Khi triển khai domain thật phải đặt reverse proxy TLS ở trước FastAPI và bật secure cookie.

WebView2 chỉ còn là prerequisite của gói desktop Windows tùy chọn.
