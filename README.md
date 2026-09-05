# TH Media

Ứng dụng Windows desktop biến một văn bản dài thành **story world → story bible → timeline → storyboard → Google Flow prompts → chuỗi video** có continuity. Giao diện chạy trong cửa sổ riêng bằng WebView2; backend nội bộ và Flow CLI được đóng gói cùng EXE.

## Tính năng đã triển khai

- Giao diện ba cột: Project/Bible, Storyboard kéo-thả, Scene Editor.
- Quy trình desktop ba bước bắt buộc: chọn thư mục làm việc → phân tích nội dung → sản xuất video.
- Mỗi lần mở EXE là một phiên mới và không tự nạp project cũ; project đã lưu chỉ được mở khi người dùng chủ động chọn **Mở đã lưu**.
- Giao diện desktop responsive: ba cột trên màn hình rộng, tự chuyển thành ba vùng có nút điều hướng khi cửa sổ hẹp; hỗ trợ từ 680×520 mà không tràn trang.
- Phân tích offline toàn bộ nội dung trước khi chia cảnh.
- Chọn engine phân tích Offline hoặc xKiro; toàn bộ catalog model khả dụng được tải live và phân loại Free/Paid/Premium.
- Bộ tiền xử lý kịch bản có cấu trúc loại tiêu đề Markdown, metadata, Character Bible và nhãn kỹ thuật khỏi các scene hình ảnh; chặn thực thể rác như `ft`, `Giọng`, `Voice` và camera label.
- Pipeline xKiro dài hạn không dùng timeout tổng: Story Bible được đọc tuần tự theo phần, scene được duyệt theo lô thích ứng với context/output của model và mọi request có timeout/retry riêng.
- Checkpoint SQLite giao dịch được lưu sau từng phần Story Bible và từng lô scene; lỗi mạng, model quá tải, đóng ứng dụng hoặc chạy lại không làm mất các phần đã hoàn tất.
- Checkpoint scene-level được ghi ngay sau từng cảnh sửa. Model trả object cảnh đơn, mảng, mapping hoặc alias field đều được chuẩn hóa; phần còn thiếu được sửa tuần tự thay vì chạy lại cả lô.
- xKiro `duplicate request already being processed` có hàng chờ riêng, không tiêu thụ retry. Trạng thái dedupe stale được đổi chữ ký phục hồi có giới hạn để không khóa dự án vĩnh viễn.
- Continuity thật sự nối dây chuyền: `start_state` của scene sau được khóa bằng chính `end_state` AI đã duyệt của scene trước; cảnh thiếu trường bắt buộc sẽ được yêu cầu model sửa thay vì âm thầm coi là hoàn tất.
- Mọi scene sau phân tích bật **AI Continuity Lock**: backend khóa Location, nhân vật, nội dung, hành động, camera, ánh sáng, không khí, prompt và start/end state. Người dùng phải chủ động mở khóa scene trước khi sửa.
- Nhật ký phân tích theo thời gian thực hiển thị model, từng giai đoạn xử lý, thời gian chờ, kết quả hoặc lỗi; có thể sao chép, xóa và hủy tác vụ.
- Character, Location và Prop Bible với ID ổn định.
- Chia scene theo câu, nhịp kể, transition và ngân sách voiceover.
- Master Project Prompt, Global Visual Style, Visual Prompt và Google Flow Prompt.
- Start frame/End frame, continuity state, auto continuity và cảnh báo downstream khi sửa scene.
- Chỉnh nội dung, location, camera, ánh sáng, voiceover, prompt và duration từng scene.
- Queue render tuần tự; Generate Selected/All, Pause, Resume và Retry qua nút Generate scene.
- Google Flow CLI chạy trực tiếp trong ứng dụng, điều khiển Chromium ẩn và tải MP4 về máy.
- Nếu API tải media của Flow trả sai preview hoặc CDN từ chối, Studio tự phục hồi MP4 qua phiên Chromium đã đăng nhập, kiểm tra container rồi mới đánh dấu scene hoàn tất.
- Flow project ID và workflow/media ID được lưu ngay khi gửi lệnh; tác vụ gián đoạn có thể tiếp tục tải kết quả mà không tạo lại video. `Generate all` bỏ qua scene đã hoàn tất.
- API key xKiro và cookie Flow chỉ cần gắn một lần, được mã hóa bằng Windows DPAPI và tự dùng lại ở mọi phiên/thư mục làm việc; chỉ hiện ô nhập khi bấm **Thêm mới / Thay đổi**.
- Chọn model Veo, xem trạng thái xác thực/credit, gắn ảnh tham chiếu và phát video ngay trên scene.
- Trình phát giữ nguyên thẻ media, thời điểm phát và bộ đệm khi hàng đợi polling tiến độ; các scene tiếp theo có thể render mà không làm video đang xem tải lại liên tục.
- Tự trích last frame bằng FFmpeg và chuyển thành reference image cho scene kế tiếp.
- Khi mọi scene có MP4 hoàn chỉnh, nút **Ghép video** sẽ bật. Studio ghép theo đúng thứ tự storyboard, mã hóa H.264/AAC tương thích, phát trực tiếp và cho tải một MP4 duy nhất.
- Quality report 0–100 sau mỗi render, ngưỡng mặc định 85.
- Lưu project JSON nguyên tử, mở lại project gần đây, export JSON và ZIP chứa prompt từng scene.
- Backend FastAPI chỉ lắng nghe loopback trên một cổng ngẫu nhiên khi chạy desktop.

## Chạy nhanh trên Windows

```powershell
cd C:\Users\Admin\Desktop\tools-phim\flow-story-studio
powershell -ExecutionPolicy Bypass -File .\start.ps1
```

Script tự cài Flow CLI, Playwright Chromium và mở cửa sổ **TH Media**. Trước tiên,
ứng dụng yêu cầu chọn hoặc tạo thư mục làm việc. Các thư mục `projects`, `renders` và `references`
sẽ được tạo bên trong thư mục này. Kho xác thực dùng chung nằm trong vùng dữ liệu người dùng của
TH Media và được Windows mã hóa. Không cần chạy `flow api serve` và không cần mở
trình duyệt thủ công.

Nếu muốn cài thủ công:

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install .\vendor\flow_cli-0.6.0-py3-none-any.whl -e ".[dev]"
.venv\Scripts\python.exe -m playwright install chromium
.venv\Scripts\python.exe -m flow_story_studio.desktop
```

## Render thật qua Google Flow

Google Flow được cấu hình ở bước **Thiết lập video**, chỉ mở sau khi nội dung đã phân tích xong:

1. Xuất toàn bộ cookie của `https://labs.google` bằng Cookie-Editor.
2. Phân tích nội dung để tạo storyboard.
3. Nhấn **Thiết lập video → Thêm cookie**, dán JSON/chuỗi cookie hoặc chọn `cookies.json`.
4. Nhấn **Lưu cookie mới**, chọn model Veo rồi nhấn Generate scene/selected/all. Những lần mở sau
   ứng dụng tự dùng cookie đã lưu; chỉ bấm **Thêm mới / Thay đổi** khi cần đổi tài khoản/cookie.

Không nhập email hay mật khẩu Google vào Studio. Cookie không được trả lại frontend, không lưu
trong project JSON và tệp cookie trên đĩa được DPAPI mã hóa theo tài khoản Windows hiện tại.

Các biến môi trường tùy chọn:

```powershell
$env:FLOW_RENDER_TIMEOUT = "900"
```

Video được lưu trong thư mục `renders` thuộc thư mục làm việc đã chọn khi mở ứng dụng.
Sau khi toàn bộ scene render thành công, nhấn **Ghép video → Ghép toàn bộ video**. Tệp tổng được
lưu tại `renders/<project-id>/final/final-video.mp4`. Nếu render lại một scene hoặc đổi thứ tự
storyboard, kết quả tổng cũ sẽ được đánh dấu cần ghép lại để tránh dùng nhầm phiên bản.

## Build EXE Windows

```powershell
cd C:\Users\Admin\Desktop\tools-phim\flow-story-studio
powershell -ExecutionPolicy Bypass -File .\build-exe.ps1
```

Kết quả: `dist\THMedia.exe`. Bản one-file chứa mã Flow CLI, Chromium Playwright, giao diện
và FFmpeg nên dung lượng lớn; lần mở đầu tiên cần thời gian giải nén vào thư mục tạm.

## Phân tích nội dung bằng xKiro

Trong bước **Phân tích nội dung**:

1. Chọn `Analysis provider → xKiro API`.
2. Nhấn **Thêm API key**, nhập key xKiro và chọn **Lưu API key mới**.
3. Chọn bất kỳ model nào trong catalog vừa tải (Free, Paid hoặc Premium).
4. Nhấn **Phân tích nội dung**.

Studio gọi catalog live `GET https://api.xkiro.com/v1/models` và hiển thị toàn bộ model tài khoản có thể thấy;
danh sách không bị hard-code. Model Paid/Premium có thể phát sinh phí theo chính sách xKiro. Key được xác thực qua backend local, mã hóa
bằng Windows DPAPI và tự dùng lại trong phiên sau; key không được lưu vào project hoặc trả lại cho trình duyệt. Chỉ bấm **Thêm mới / Thay đổi** khi muốn thay key. Có thể
cấu hình key từ môi trường trước khi chạy nếu muốn:

```powershell
$env:XKIRO_API_KEY = "your-key"
$env:XKIRO_REQUEST_TIMEOUT = "900"       # timeout cho MỖI request, không phải toàn job
$env:XKIRO_REQUEST_RETRIES = "4"          # tự thử lại lỗi timeout/429/5xx
$env:XKIRO_SCENE_BATCH_SIZE = "6"         # tự hạ nếu context/output model nhỏ
powershell -ExecutionPolicy Bypass -File .\start.ps1
```

Khi dùng xKiro, engine offline tạo canonical ID ban đầu; model đã chọn đọc toàn bộ nội dung để
hoàn thiện Story Bible, Character/Location/Prop Bible, scene action, camera, thoại và continuity
state. Kết quả vẫn đi qua cùng Continuity Engine và Prompt Generation Engine của ứng dụng.
Mọi độ dài đều đi qua cùng pipeline có giới hạn bộ nhớ theo request. Nội dung tối đa 5.000.000 ký tự;
Story Bible được cập nhật tuần tự qua các phần tối đa theo context của model, sau đó scene được duyệt
theo lô 1–8 cảnh. Checkpoint nằm trong `analysis-checkpoints` của thư mục làm việc và tự xóa sau khi
project cuối đã được ghi thành công. Nếu cùng nội dung/model/thiết lập được chạy lại sau lỗi, Studio
tự nhận checkpoint và tiếp tục phần còn thiếu.
Trong Scene Editor, nút **AI đã khóa** cho biết các trường đang được bảo vệ. Chỉ bấm mở khóa khi
thực sự cần sửa thủ công; sau khi sửa nên khóa lại và chạy **Auto continuity** trước khi render.

API local liên quan:

- `GET /api/ai/xkiro/status`
- `POST /api/ai/xkiro/connect`
- `GET /api/ai/xkiro/models`
- `DELETE /api/ai/xkiro`
- `POST /api/analysis/jobs`
- `GET /api/analysis/jobs/{job_id}`
- `DELETE /api/analysis/jobs/{job_id}`

## Chạy kiểm thử

```powershell
.venv\Scripts\python.exe -m pip install -e ".[dev]"
.venv\Scripts\python.exe -m pytest
.venv\Scripts\python.exe -m ruff check .
```

## Cấu trúc

```text
src/flow_story_studio/
├── engines/              # analyze, segment, prompt, continuity, quality
├── providers/            # contract và mock provider
├── flow_integration.py   # cookie vault + Flow CLI + download + last frame
├── desktop.py            # cửa sổ Windows WebView2 + backend loopback
├── main.py               # các route nội bộ + static UI
├── models.py             # canonical project schema
├── render_queue.py       # sequential render orchestration
├── video_merger.py       # kiểm tra scene + ghép MP4 bằng FFmpeg
├── service.py            # application use cases
└── storage.py            # atomic JSON persistence
static/                   # giao diện web không cần Node/npm
vendor/                   # Flow CLI wheel vendored; không cần thư mục sibling khi chạy/build
tests/                    # engine, storage, API tests
data/projects/            # project runtime (không commit)
```

## Giới hạn có chủ đích

Engine offline dùng quy tắc ngôn ngữ nên phù hợp để chạy ngay và kiểm tra workflow. Tích hợp Google
Flow dùng cookie và giao diện web không chính thức, vì vậy có thể cần cập nhật Flow CLI khi Google
thay đổi UI hoặc endpoint. Tài khoản vẫn phải có quyền sử dụng model/credit tương ứng.
