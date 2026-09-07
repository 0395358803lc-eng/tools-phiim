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

## Credentials and local API

xKiro API key được bảo vệ bằng Windows DPAPI. Desktop session dùng token ngẫu nhiên theo process.

Mutating API requests (`POST`, `PUT`, `PATCH`, `DELETE`) yêu cầu header `X-Flow-Studio-Session`. Tên header này được giữ vì compatibility của ứng dụng desktop hiện tại.

API chỉ bind `127.0.0.1` trên cổng ngẫu nhiên.

## Workspace concurrency

Workspace được bảo vệ bởi `.flow-story-studio.lock`. Đây là tên runtime legacy của ứng dụng và được giữ để tránh phá compatibility.

## Dependency reproducibility

`requirements.lock.txt` là constraints được xác nhận cho Windows/Python 3.12.

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

## WebView2 prerequisite

Installer kiểm tra Microsoft Edge WebView2 Evergreen Runtime. Nếu thiếu, bootstrapper chính thức của Microsoft được tải, xác minh Authenticode và cài trước TH Media.
