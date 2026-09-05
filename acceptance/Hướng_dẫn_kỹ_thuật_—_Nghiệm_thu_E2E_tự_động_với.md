# HƯỚNG DẪN NGHIỆM THU E2E TỰ ĐỘNG VỚI xKIRO AI

## 1. Mục tiêu

Xây dựng một quy trình nghiệm thu end-to-end có thể tự động thực hiện toàn bộ chuỗi sau:

```text
Khởi động ứng dụng TH Media
        ↓
Mở workspace nghiệm thu riêng
        ↓
Đọc tài liệu kịch bản đầu vào
        ↓
Xác nhận xKiro đã được cấu hình
        ↓
Chọn model AI xKiro
        ↓
Gửi kịch bản vào Analysis Job
        ↓
Theo dõi job đến trạng thái cuối
        ↓
Đọc project đã được phân tích
        ↓
Xuất toàn bộ dữ liệu phân tích
        ↓
Tạo manifest + log + báo cáo
        ↓
Đóng ứng dụng sạch
        ↓
Bàn giao một thư mục nghiệm thu duy nhất
```

Mục tiêu cuối cùng là tạo một thư mục dữ liệu mà người nghiệm thu có thể đọc độc lập và đối chiếu toàn bộ kết quả do xKiro tạo ra.

---

# 2. Nguyên tắc bắt buộc

Không được:

- ghi API key xKiro vào script;
- ghi API key vào JSON nghiệm thu;
- ghi API key vào command line;
- ghi credential vào log;
- copy `data/secrets` vào output;
- xuất cookies Google Flow;
- dùng mock provider thay cho xKiro rồi báo là nghiệm thu AI thật;
- sửa kết quả AI trước khi xuất;
- chỉ xuất screenshot thay cho dữ liệu gốc;
- báo PASS nếu analysis job failed/cancelled;
- báo PASS nếu không xác minh model thực tế.

Phải sử dụng credential xKiro đã được ứng dụng lưu an toàn từ trước.

---

# 3. Dữ liệu đầu vào

Nhân viên chọn một tài liệu kịch bản chuẩn dùng cho nghiệm thu.

Ưu tiên fixture hiện có:

```text
acceptance/fixtures/chiec-ve-khong-co-chuyen-tau.md
```

Nếu sử dụng tài liệu khác, phải ghi chính xác absolute path.

Ví dụ:

```text
C:\Users\Admin\Desktop\phim\tools-phiim\acceptance\fixtures\chiec-ve-khong-co-chuyen-tau.md
```

Không chỉnh sửa tài liệu sau khi bắt đầu nghiệm thu.

Trước khi chạy phải tính SHA-256 của tài liệu nguồn.

---

# 4. Workspace nghiệm thu riêng

Không sử dụng workspace sản xuất của người dùng.

Tạo workspace riêng, ví dụ:

```text
C:\Users\Admin\Desktop\phim\xkiro-acceptance-workspace
```

Mỗi lần chạy nên tạo subfolder có timestamp:

```text
xkiro-acceptance-workspace\
└── run-20260905-221500\
```

Mục tiêu:

- không làm thay đổi project cũ;
- không trộn dữ liệu các lần nghiệm thu;
- dễ bàn giao nguyên trạng.

---

# 5. Thư mục output cuối

Output nghiệm thu phải được tách riêng khỏi runtime workspace.

Đề xuất:

```text
C:\Users\Admin\Desktop\phim\xkiro-acceptance-results\
└── run-20260905-221500\
```

Thư mục này là dữ liệu cuối cùng bàn giao cho người nghiệm thu.

---

# 6. Tạo automation runner

Nhân viên kỹ thuật cần tạo script:

```text
scripts/xkiro_acceptance_runner.py
```

Không nên tự động hóa bằng click chuột/screenshot nếu API nội bộ của ứng dụng đã cung cấp contract rõ ràng.

Runner phải sử dụng chính application/backend hiện có.

Không được xây một pipeline AI song song bỏ qua `StudioService`.

---

# 7. Khởi động application

Application hiện hỗ trợ workspace override qua:

```text
FLOW_STUDIO_WORKSPACE_DIR
```

Runner phải tạo một desktop session với workspace nghiệm thu đã xác định.

Luồng chuẩn dự kiến:

```text
DesktopSession
→ _start_backend(workspace)
→ create_app(...)
→ Uvicorn
→ 127.0.0.1:<random-port>
```

Backend phải vẫn bind:

```text
127.0.0.1
```

Không đổi sang:

```text
0.0.0.0
```

để phục vụ nghiệm thu.

---

# 8. Không hard-code port

Application hiện chọn port động.

Automation runner không được giả định:

```text
8000
8765
8080
```

Runner phải lấy URL do chính `DesktopSession` tạo ra.

Ví dụ về mặt logic:

```python
session = DesktopSession(...)
url = session._start_backend(workspace)
```

URL sẽ có dạng:

```text
http://127.0.0.1:<PORT>/#session=<SESSION_TOKEN>
```

Automation phải tách:

```text
base_url
session_token
```

trong memory.

Không ghi `session_token` vào output.

---

# 9. Khởi động UI thực tế

Nghiệm thu phải xác nhận application desktop thực sự có thể start.

Runner hoặc orchestration script phải mở window TH Media bằng code path production hiện có.

Mục tiêu:

```text
Desktop launcher
PASS
```

Không chỉ chạy FastAPI TestClient.

Có thể automation backend chạy trong worker thread trong khi WebView application vẫn được mở.

Kết quả phải chứng minh cả:

```text
Desktop application started
Backend ready
```

---

# 10. Health check

Sau khi backend start, gọi:

```http
GET /api/health
```

Chỉ tiếp tục khi:

```text
HTTP 200
```

Nếu backend không ready:

```text
FAIL
```

và dừng nghiệm thu.

---

# 11. Xác nhận xKiro credential

Gọi:

```http
GET /api/ai/xkiro/status?include_models=true
```

Không yêu cầu nhân viên đọc secret.

Không gọi endpoint connect với API key hard-coded.

Nếu status cho thấy chưa kết nối/cấu hình:

```text
STOP
```

Báo:

```text
xKiro credential is not configured.
```

Không được chuyển sang offline provider.

---

# 12. Lấy danh sách model xKiro

Gọi:

```http
GET /api/ai/xkiro/models
```

hoặc:

```http
GET /api/ai/xkiro/models?refresh=true
```

nếu cần kiểm tra dữ liệu model mới từ provider.

Lưu response đã loại bỏ mọi field nhạy cảm vào:

```text
00-environment\xkiro-models.json
```

---

# 13. Model dùng để nghiệm thu

Không chọn model ngẫu nhiên.

Model phải được xác định trước khi chạy.

Runner nhận parameter:

```text
--model <MODEL_ID>
```

Ví dụ:

```cmd
python scripts\xkiro_acceptance_runner.py ^
  --input "acceptance\fixtures\chiec-ve-khong-co-chuyen-tau.md" ^
  --model "<xkiro-model-id>"
```

Script phải xác minh model ID tồn tại trong danh sách do xKiro trả về.

Nếu không tồn tại:

```text
FAIL
```

Không fallback sang model khác mà không thông báo.

---

# 14. Đọc nguyên bản kịch bản

Script phải đọc file bằng:

```text
UTF-8
```

Lưu lại một bản copy byte-identical vào output:

```text
01-input\screenplay-original.md
```

Đồng thời lưu:

```text
01-input\screenplay.sha256
```

Đảm bảo người nghiệm thu biết chính xác source nào đã được đưa vào AI.

---

# 15. Request phân tích

Source hiện sử dụng model:

```python
AnalyzeRequest
```

với:

```text
name
original_text
settings
```

Settings cần đặt:

```json
{
  "analysis_provider": "xkiro",
  "analysis_model": "<MODEL_ID>"
}
```

Các setting video khác giữ theo default trừ khi fixture yêu cầu khác.

Đặc biệt:

```text
analysis_provider = xkiro
```

là điều kiện bắt buộc.

---

# 16. Không sử dụng `/api/projects/analyze` cho job dài

Đối với nghiệm thu AI thật, ưu tiên API background job hiện có:

```http
POST /api/analysis/jobs
```

không dùng synchronous endpoint nếu screenplay dài.

API này được thiết kế cho:

```text
long-running story analysis jobs
```

và có:

- queued;
- running;
- logs;
- completed;
- failed;
- cancelled.

---

# 17. Request mẫu về logic

Request phải tương đương:

```json
{
  "name": "xkiro-e2e-acceptance",
  "original_text": "<TOÀN BỘ KỊCH BẢN>",
  "settings": {
    "analysis_provider": "xkiro",
    "analysis_model": "<MODEL_ID>",
    "provider": "mock",
    "aspect_ratio": "16:9",
    "resolution": "1080p",
    "style": "Cinematic",
    "scene_duration": 8,
    "character_lock": true,
    "location_lock": true,
    "auto_continuity": true,
    "quality_threshold": 85
  }
}
```

`provider: mock` ở đây chỉ có nghĩa **không render video Google Flow** trong bài nghiệm thu phân tích.

Nó không được thay đổi:

```text
analysis_provider = xkiro
```

Không chạy auto video pipeline trong bài test này.

---

# 18. Session authentication

Các mutating request phải sử dụng session token do desktop session tạo.

Ví dụ header ở mức logic:

```http
X-Flow-Studio-Session: <SESSION_TOKEN>
```

Token chỉ tồn tại trong memory của runner.

Không log giá trị token.

Không ghi nó vào:

```text
request.json
manifest.json
report.md
stdout
```

---

# 19. Khởi tạo analysis job

Gọi:

```http
POST /api/analysis/jobs
```

Mong đợi:

```text
HTTP 202
```

Response phải chứa:

```text
job id
status
provider
model
```

Lưu response đã sanitize vào:

```text
02-analysis-job\job-created.json
```

---

# 20. Theo dõi job

Polling:

```http
GET /api/analysis/jobs/{job_id}
```

Chu kỳ đề xuất:

```text
2–5 giây
```

Không spam endpoint.

Tiếp tục tới khi:

```text
completed
failed
cancelled
```

Không đặt timeout quá ngắn cho phân tích xKiro thật.

---

# 21. Lưu toàn bộ lifecycle

Mỗi lần status thay đổi, ghi event vào:

```text
02-analysis-job\job-events.jsonl
```

Ví dụ:

```json
{"status":"queued","at":"..."}
{"status":"running","at":"..."}
{"status":"completed","at":"..."}
```

Không cần ghi duplicate snapshot mỗi 2 giây nếu không có thay đổi.

---

# 22. Lưu log AI job

Snapshot cuối của job có field:

```text
logs
```

Xuất nguyên trạng vào:

```text
02-analysis-job\job-final.json
02-analysis-job\job-logs.json
```

Không chỉnh sửa wording.

Nếu log chứa dữ liệu nhạy cảm bất ngờ thì phải sanitize credential nhưng không được xóa thông tin lỗi kỹ thuật khác.

---

# 23. Điều kiện analysis PASS

Chỉ PASS khi:

```text
status == completed
```

và:

```text
project.id
```

tồn tại.

Nếu:

```text
failed
```

thì lưu:

```text
error
logs
model
provider
```

và kết thúc với exit code khác 0.

Không tiếp tục giả vờ xuất project thành công.

---

# 24. Đọc full Project qua API

Job snapshot hiện chỉ chứa summary:

```text
id
name
scene_count
continuity_score
scenes status/progress
```

Vì vậy sau khi completed, runner bắt buộc gọi:

```http
GET /api/projects/{project_id}
```

để lấy **toàn bộ canonical Project**.

Response đầy đủ chứa các nhóm dữ liệu:

```text
schema_version
id
name
original_text
settings
story_bible
characters
locations
props
timeline
visual_style
master_prompt
visual_bible
scenes
continuity_score
continuity_warnings
flow_project_id
final_video
```

Đây mới là dữ liệu chính để nghiệm thu.

---

# 25. Xuất canonical project

Lưu response chính xác vào:

```text
03-project\project-full.json
```

Dùng UTF-8.

Pretty-print được phép nhưng không thay đổi dữ liệu.

---

# 26. Copy project persistence gốc

Ngoài API response, tìm JSON được application thực sự lưu trong workspace:

```text
<WORKSPACE>\projects\
```

Copy file project tương ứng sang:

```text
03-project\project-storage-original.json
```

Sau đó tính SHA-256 cả hai.

Nếu API canonical response và storage representation khác ở field hợp lệ như serialization formatting thì ghi nhận.

Nếu semantic data khác:

```text
FAIL
```

và báo rõ.

---

# 27. Tách dữ liệu để người nghiệm thu đọc dễ hơn

Ngoài `project-full.json`, automation nên xuất từng domain riêng.

Cấu trúc:

```text
04-analysis-data\
├── story-bible.json
├── characters.json
├── locations.json
├── props.json
├── timeline.json
├── visual-bible.json
├── scenes.json
├── continuity.json
└── master-prompt.txt
```

Đây là bản tách từ canonical project.

Không gọi AI lần hai để tạo các file này.

---

# 28. Xuất từng scene riêng

Tạo:

```text
05-scenes\
```

Mỗi scene một JSON:

```text
0001-<scene-id>.json
0002-<scene-id>.json
0003-<scene-id>.json
...
```

Giữ thứ tự canonical của `project.scenes`.

Mỗi file chứa toàn bộ scene object.

---

# 29. Tạo scene index

Tạo:

```text
05-scenes\scene-index.csv
```

Các cột tối thiểu:

```text
index
scene_id
summary
location_id
characters
duration
status
continuity_start
continuity_end
```

CSV chỉ phục vụ review nhanh.

JSON canonical vẫn là source of truth.

---

# 30. Continuity report

Tạo:

```text
06-validation\continuity-report.json
```

Bao gồm:

```text
continuity_score
continuity_warnings
scene_count
boundary_count
broken_boundaries
```

Runner phải tự đối chiếu state chain.

Với mỗi cặp scene mà contract yêu cầu direct continuation, kiểm tra:

```text
previous.end_state
==
current.start_state
```

Không sửa project nếu mismatch.

Chỉ ghi vào report.

---

# 31. Source-truth comparison

Nếu sử dụng fixture:

```text
chiec-ve-khong-co-chuyen-tau.md
```

thì sử dụng:

```text
acceptance/fixtures/chiec-ve-source-truth.json
```

làm dữ liệu đối chiếu.

Copy vào:

```text
01-input\source-truth.json
```

Không sửa fixture.

---

# 32. Semantic validation

Nghiệm thu tự động nên đối chiếu tối thiểu:

```text
character identity
character presence
forbidden characters
locations
required props
flashback/present timeline
dialogue delivery
recorded content
object permanence
continuity
```

Kết quả:

```text
06-validation\semantic-validation.json
```

Không tự suy diễn source truth ngoài fixture.

---

# 33. Không render video trong bài nghiệm thu này

Yêu cầu hiện tại là:

```text
kịch bản
→ xKiro
→ dữ liệu phân tích
```

Không cần gọi:

```text
Google Flow
Veo
video generation
final-video merge
```

Điều này giúp tách riêng chất lượng phân tích AI khỏi chất lượng video provider.

Nếu muốn nghiệm thu render, đó phải là bài E2E kế tiếp.

---

# 34. Metadata môi trường

Tạo:

```text
00-environment\environment.json
```

Bao gồm:

```text
timestamp UTC
hostname
Windows version
Python version
application version
Git branch
Git commit
Git dirty state
input path
input SHA-256
workspace path
xKiro configured: true/false
selected model ID
analysis provider
```

Không ghi:

```text
API key
session token
cookies
Authorization header
```

---

# 35. Git metadata

Lưu:

```text
00-environment\git-status.txt
00-environment\git-commit.txt
```

Mục đích là biết chính xác source version nào sinh ra dữ liệu nghiệm thu.

---

# 36. Timing

Tạo:

```text
07-metrics\timing.json
```

Bao gồm:

```text
application_start_seconds
backend_ready_seconds
xkiro_status_seconds
analysis_seconds
export_seconds
total_seconds
```

Không cần benchmark performance phức tạp.

---

# 37. Manifest

Cuối quy trình phải tạo:

```text
manifest.json
```

Manifest liệt kê mọi file trong thư mục nghiệm thu với:

```text
relative_path
size_bytes
sha256
```

Không đưa chính `manifest.json` vào hash của chính nó trừ khi dùng manifest hai tầng.

---

# 38. Báo cáo máy đọc

Tạo:

```text
result.json
```

Ví dụ cấu trúc:

```json
{
  "status": "PASS",
  "application_started": true,
  "backend_ready": true,
  "xkiro_connected": true,
  "analysis_provider": "xkiro",
  "analysis_model": "<MODEL_ID>",
  "analysis_job_status": "completed",
  "project_id": "<ID>",
  "scene_count": 22,
  "continuity_score": 100,
  "canonical_project_exported": true,
  "semantic_validation": "PASS",
  "secrets_exported": false
}
```

Không hard-code số scene hoặc score.

Phải lấy từ execution thực tế.

---

# 39. Báo cáo cho con người

Tạo:

```text
REPORT.md
```

Nội dung:

```text
# XKiro E2E Acceptance Report

Run ID:
Timestamp:
Git commit:

## Input
File:
SHA-256:
Characters:
Lines:

## Application
Desktop started:
Backend health:
Workspace:

## xKiro
Connected:
Model:
Provider:

## Analysis
Job ID:
Status:
Duration:
Scene count:
Continuity score:

## Validation
Source truth:
Continuity:
Characters:
Locations:
Props:
Timeline:
Dialogue delivery:

## Export
Canonical project:
Scene files:
Manifest:
Secrets found:

## Result
PASS / FAIL

## Remaining issues
...
```

---

# 40. Cấu trúc output bắt buộc cuối cùng

Sau một run thành công, người nghiệm thu phải nhận được dạng:

```text
run-20260905-221500\
│
├── manifest.json
├── result.json
├── REPORT.md
│
├── 00-environment\
│   ├── environment.json
│   ├── git-status.txt
│   ├── git-commit.txt
│   └── xkiro-models.json
│
├── 01-input\
│   ├── screenplay-original.md
│   ├── screenplay.sha256
│   └── source-truth.json
│
├── 02-analysis-job\
│   ├── job-created.json
│   ├── job-events.jsonl
│   ├── job-final.json
│   └── job-logs.json
│
├── 03-project\
│   ├── project-full.json
│   └── project-storage-original.json
│
├── 04-analysis-data\
│   ├── story-bible.json
│   ├── characters.json
│   ├── locations.json
│   ├── props.json
│   ├── timeline.json
│   ├── visual-bible.json
│   ├── scenes.json
│   ├── continuity.json
│   └── master-prompt.txt
│
├── 05-scenes\
│   ├── scene-index.csv
│   ├── 0001-....json
│   ├── 0002-....json
│   └── ...
│
├── 06-validation\
│   ├── continuity-report.json
│   └── semantic-validation.json
│
└── 07-metrics\
    └── timing.json
```

---

# 41. Secret scan trước bàn giao

Trước khi kết thúc, automation phải kiểm tra toàn bộ output.

Không được tìm thấy:

```text
X-Flow-Studio-Session
Authorization:
Bearer
API key plaintext
Flow cookies
data/secrets
```

Nếu phát hiện:

```text
FAIL
```

và không bàn giao folder cho tới khi xử lý.

Không ghi secret thực tế vào báo cáo lỗi.

Chỉ ghi file/path bị phát hiện.

---

# 42. Exit code

Runner phải trả:

```text
0 = PASS
```

Các lỗi nghiệm thu:

```text
1 = general failure
2 = application startup failure
3 = xKiro unavailable
4 = model unavailable
5 = analysis failed
6 = validation failed
7 = export failed
8 = secret scan failed
```

Có thể điều chỉnh numbering nhưng phải documented.

---

# 43. Cleanup

Sau khi export hoàn tất:

1. đóng analysis polling;
2. shutdown backend;
3. đóng desktop application;
4. release workspace lock;
5. xác nhận process không còn chạy bất thường.

Không xóa workspace nghiệm thu ngay.

Giữ workspace cho tới khi người nghiệm thu hoàn tất đối chiếu.

---

# 44. Không đóng gói output thành ZIP ở bước đầu

Để người nghiệm thu có thể đọc trực tiếp bằng Windows Control API, ưu tiên giữ dưới dạng folder.

Không ZIP trừ khi cần bàn giao ra ngoài.

---

# 45. Command giao cho nhân viên

Sau khi xây runner, cú pháp mục tiêu nên đơn giản:

```cmd
cd /d C:\Users\Admin\Desktop\phim\tools-phiim

.venv\Scripts\python.exe scripts\xkiro_acceptance_runner.py ^
  --input "acceptance\fixtures\chiec-ve-khong-co-chuyen-tau.md" ^
  --source-truth "acceptance\fixtures\chiec-ve-source-truth.json" ^
  --model "<MODEL_ID>" ^
  --workspace-root "C:\Users\Admin\Desktop\phim\xkiro-acceptance-workspace" ^
  --output-root "C:\Users\Admin\Desktop\phim\xkiro-acceptance-results"
```

Không có:

```text
--api-key
--cookie
--session-token
```

trên command line.

---

# 46. Test runner trước khi gọi xKiro thật

Trước live test, nhân viên phải viết automated tests cho runner/helper ít nhất cho:

```text
output folder creation
SHA-256 generation
manifest generation
scene export
continuity validation
secret scanning
failed job handling
model-not-found handling
```

Các test này không được gọi API xKiro thật.

---

# 47. Live test chỉ chạy một lần sau khi local tests xanh

Khi runner tests PASS:

```text
ruff PASS
pytest PASS
compile PASS
```

mới chạy live xKiro.

Không lặp API AI nhiều lần chỉ để debug file export.

Nếu export bug xảy ra sau khi project đã được lưu, runner phải có khả năng:

```text
--export-existing-project <project_id>
```

để tạo lại package từ dữ liệu local mà **không gọi xKiro lần nữa**.

Đây là yêu cầu rất quan trọng.

---

# 48. Điều kiện nghiệm thu PASS cuối

Chỉ coi run đạt khi đồng thời:

```text
[ ] Desktop application start thành công
[ ] Backend health 200
[ ] Workspace riêng được sử dụng
[ ] xKiro status connected
[ ] Model ID được xác minh
[ ] Input SHA-256 được ghi
[ ] analysis_provider == xkiro
[ ] Analysis Job HTTP 202
[ ] Job kết thúc completed
[ ] Project ID tồn tại
[ ] Full canonical Project đọc được
[ ] Storage project tồn tại
[ ] Characters được xuất
[ ] Locations được xuất
[ ] Props được xuất
[ ] Timeline được xuất
[ ] Visual Bible được xuất
[ ] Toàn bộ Scenes được xuất
[ ] Continuity report được tạo
[ ] Source-truth validation được tạo
[ ] Job logs được giữ
[ ] Manifest có SHA-256
[ ] Không có credential trong output
[ ] Application shutdown sạch
```

---

# 49. Không chấp nhận các trường hợp sau

Không PASS nếu:

```text
xKiro unavailable → fallback offline
```

Không PASS nếu:

```text
job failed → dùng project của lần trước
```

Không PASS nếu:

```text
model A fail → tự đổi model B
```

Không PASS nếu:

```text
chỉ có screenshot UI
```

Không PASS nếu:

```text
project JSON đã bị sửa tay
```

Không PASS nếu:

```text
không biết Git commit nào tạo output
```

---

# 50. Yêu cầu bàn giao cho người nghiệm thu

Sau khi chạy xong, nhân viên chỉ cần báo:

```text
E2E XKiro Acceptance completed.

Result:
PASS / FAIL

Output folder:
C:\Users\Admin\Desktop\phim\xkiro-acceptance-results\run-XXXXXXXX-XXXXXX

Project ID:
...

Model:
...

Scene count:
...

Continuity score:
...

Analysis duration:
...
```

Không gửi API key.

Không paste hàng nghìn dòng JSON vào chat.

Người nghiệm thu sẽ kết nối trực tiếp tới output folder và đọc dữ liệu.

---

# 51. Giai đoạn nghiệm thu của người kiểm tra

Sau khi nhân viên báo hoàn tất, người nghiệm thu sẽ trực tiếp kiểm tra:

```text
manifest.json
result.json
REPORT.md
project-full.json
project-storage-original.json
job-final.json
job-logs.json
characters.json
locations.json
props.json
timeline.json
visual-bible.json
scenes.json
scene-index.csv
continuity-report.json
semantic-validation.json
```

Sau đó đối chiếu ngược lại:

```text
Kịch bản gốc
      ↓
Source truth
      ↓
xKiro output
      ↓
Canonical project
      ↓
Scene contracts
      ↓
Continuity
```

Mục tiêu cuối cùng không chỉ là chứng minh API xKiro gọi được, mà là chứng minh:

**Ứng dụng thực tế có thể tự động nhận một kịch bản hoàn chỉnh, phân tích bằng đúng model xKiro đã chọn, lưu project chính xác, và tạo đủ dữ liệu để một bên độc lập có thể nghiệm thu toàn bộ kết quả mà không phải gọi lại AI.**