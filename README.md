# TH Media

TH Media là ứng dụng web cho pipeline sản xuất phim theo hướng continuity-first; lớp Windows desktop cũ được giữ dưới dạng compatibility tùy chọn:

```text
Screenplay
→ Analysis
→ Semantic Finalization
→ Canonical Film Model
→ Scene Intent
→ State Delta
→ Visual Bible
→ Scene Contract
→ Render Queue
→ Render Provider Interface
→ QC
→ Production Acceptance
→ Accepted Runtime State
→ Final Merge
```

Hiện core **không đóng gói render backend mặc định**. Kiến trúc render vẫn được giữ nguyên để tích hợp renderer mới qua `ProviderRegistry` mà không thay đổi analysis/QC/acceptance pipeline.

## Thành phần chính

- Phân tích offline hoặc xKiro.
- Canonical Film Model, Visual Bible, Audio Bible và Scene Contract.
- AI Continuity Lock và kiểm tra continuity.
- RenderQueue tuần tự, Pause/Resume/Retry.
- Provider-neutral `VideoProvider` và `ReferenceProvider`.
- Visual QC, Audio QC, Continuity QC và Production Gate fail-closed.
- FFmpeg media tools, final merge và chống video trùng lặp.
- Project JSON có versioned migration và backup.
- Web runtime FastAPI + SPA HTML/CSS/JavaScript; Windows WebView2 chỉ còn là optional compatibility.

## Render engine

Trạng thái mặc định:

```text
Render Provider Interface
→ No render backend configured
```

`GET /api/render/status` trả trạng thái provider hiện tại. Khi chưa có backend, app vẫn healthy và analysis/project/continuity vẫn hoạt động bình thường.

Các nút Generate scene/selected/all được giữ trong UI nhưng bị khóa khi renderer chưa được cấu hình. API generate trả lỗi controlled `409 Conflict` trước khi đưa scene vào queue.

Provider mới chỉ cần đăng ký vào registry và trả về kết quả generic:

```python
RenderResult(
    job_id=...,
    result_url=...,
    result_file=...,
    last_frame_file=...,
)
```

Sau đó kết quả tiếp tục đi qua media extraction, QC, Production Acceptance và Final Merge hiện có.

## Scene Image Continuity Plan

Mỗi scene có một `image_plan` được biên dịch từ Visual Bible và continuity state trước khi nối image renderer thật.

- Character/Location/Prop Master Reference là tài sản cấp Project trong `Visual Bible`; mỗi entity có đúng một Master đang được duyệt và mọi scene chỉ reuse Master đó.
- Scene output frame không bao giờ được tự promote thành Character/Location/Prop Master. Master phải được tạo/tải ở cấp Project và qua Vision QC.
- Opening/canonical scene dùng `canonical_reanchor`: tạo composition mới bằng cách REUSE các Master đã Approved; không được thiết kế lại khuôn mặt, location hoặc prop.
- Direct scene dùng `previous_accepted_end_frame`: frame cuối Accepted của scene trước là physical start-frame anchor, còn Project Masters vẫn là identity/world guards.
- Mỗi scene có thêm target/end keyframe prompt từ cùng bộ Master để khóa trạng thái cuối cảnh cho renderer hỗ trợ start/end image sau này.
- Nếu Master Reference bắt buộc chưa được duyệt, Image Plan ở trạng thái `Blocked`.
- Upload Master dùng xKiro Vision fail-closed: chỉ đạt ngưỡng và không có error mới chuyển sang `approved`; nếu Vision chưa khả dụng thì chỉ là `candidate`.
- Khi một Master được thay bằng phiên bản mới đã duyệt, mọi scene phụ thuộc và direct-continuation downstream bị invalidate media evidence cũ để không trộn hai phiên bản identity/world.
- Đường dẫn ảnh, trạng thái approved và file runtime không nằm trong semantic Scene Contract; chúng được bind động để không làm stale AI lock.
- Nếu semantic plan thay đổi (nhân vật, location, action, camera, lighting, state...), ảnh scene đã tạo trước đó bị invalidated.
- UI `Project → Bible` là Master Reference Library; UI `Scene editor → Tạo ảnh` chỉ hiển thị REUSE MASTER, strategy, anchor, identity/composition locks, start-frame prompt và target-frame prompt.

## Chạy Web trên Linux

```bash
cd /home/runner/workspace/tools-phiim
./scripts/setup-web-linux.sh
./scripts/start-web-daemon.sh
./scripts/healthcheck-web.sh
```

Runtime hiện tại dùng `/home/runner/workspace/thmedia-data/thmedia.env`. Web API bind theo `TH_MEDIA_HOST/TH_MEDIA_PORT`; khi public qua HTTPS phải bật secure cookie ở reverse proxy/runtime.

## Chạy desktop trên Windows

```powershell
cd C:\Users\Admin\Desktop\phim\tools-phiim
powershell -ExecutionPolicy Bypass -File .\setup.ps1
powershell -ExecutionPolicy Bypass -File .\start.ps1
```

`setup.ps1` chỉ cài dependency thật sự của TH Media. Playwright là dev dependency cho browser E2E của chính ứng dụng, không phải runtime renderer.

## xKiro analysis

Trong bước Phân tích nội dung:

1. Chọn `Analysis provider → xKiro API`.
2. Nhập API key và tải catalog model.
3. Chọn riêng model AI phân tích nội dung.
4. Chọn riêng model AI Vision cho Visual QC/Continuity QC; UI chỉ liệt kê model có `capabilities.vision=true`.
5. Chạy phân tích.

`vision_model` được lưu trong settings của project và được dùng bắt buộc cho QC 5 frame, QC continuity giữa hai clip trực tiếp và QC reference image. Nếu model Vision đã chọn không còn khả dụng hoặc mất capability Vision, QC fail-closed thay vì âm thầm đổi model.

Key không ghi vào project JSON. Desktop Windows tiếp tục dùng DPAPI; Linux/web dùng encrypted server vault. Pipeline xKiro dùng checkpoint để tiếp tục công việc dài, sau đó vẫn đi qua cùng Semantic Finalization, Canonical Film Model và Scene Contract.

API liên quan:

- `GET /api/ai/xkiro/status`
- `POST /api/ai/xkiro/connect`
- `GET /api/ai/xkiro/models`
- `GET /api/ai/xkiro/models?vision_only=true`
- `DELETE /api/ai/xkiro`
- `POST /api/analysis/jobs`
- `GET /api/analysis/jobs/{job_id}`
- `DELETE /api/analysis/jobs/{job_id}`

## Google Flow session trên Web

Google Flow chạy bằng Chromium worker trên server. Người dùng không nhập mật khẩu Google vào TH Media; thay vào đó, UI nhận Playwright storage-state JSON hoặc danh sách cookie của phiên Google đã đăng nhập.

- Chỉ cookie thuộc domain Google được chấp nhận; cookie domain khác bị loại bỏ.
- Session được mã hóa trong server vault và API trạng thái chỉ trả metadata, không trả cookie.
- Nút `Import session` lưu session; nút `Kiểm tra session` khởi động Chromium/CDP và xác minh trạng thái đăng nhập.
- Renderer tiếp tục dùng `GoogleFlowBrowserWorker`; chỉ lớp session/Chrome transport được thay đổi cho Linux/web.
- Runtime dùng persistent Chromium profile riêng dưới `browser-sessions/google-flow`.

API:

- `GET /api/google-flow/session`
- `POST /api/google-flow/session/import`
- `POST /api/google-flow/session/validate`
- `DELETE /api/google-flow/session`

## Kiểm thử

```powershell
.venv\Scripts\python.exe -m pytest
.venv\Scripts\python.exe -m pytest --cov=flow_story_studio
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe -m pip check
node --check static\app.js
```

Browser E2E:

```powershell
.venv\Scripts\python.exe -m playwright install chromium
.venv\Scripts\python.exe scripts\browser-e2e.py
```

## Cấu trúc

```text
src/flow_story_studio/
├── analysis_providers/
├── engines/
├── film/
├── providers/
│   ├── base.py
│   ├── mock.py
│   ├── reference.py
│   ├── registry.py
│   └── unavailable.py
├── media_tools.py
├── reference_manager.py
├── render_queue.py
├── production_gate.py
├── video_merger.py
├── visual_qc.py
├── audio_qc.py
└── main.py
```

Tên package `flow_story_studio` và các biến runtime `FLOW_STUDIO_*` được giữ để tránh migration rủi ro; đây là tên ứng dụng legacy, không phải implementation của render provider.

## Build EXE

```powershell
powershell -ExecutionPolicy Bypass -File .\build-exe.ps1
```

Build bundle core, static UI và FFmpeg. Renderer tương lai phải được tích hợp riêng qua provider contract.
