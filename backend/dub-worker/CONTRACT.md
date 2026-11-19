# HỢP ĐỒNG DỮ LIỆU — Dubbing pipeline (cho đợt sửa trang P9/P10)

Tài liệu này là thỏa thuận giữa backend/worker (đã làm xong đợt này) và
trang web (làm ở đợt sau). Trang web KHÔNG được tự ý đổi tên bảng, tên cột,
tên bucket, hay các trạng thái dưới đây.

## 1. Bảng `public.dubbing_jobs`

| Cột | Kiểu | Ý nghĩa |
|---|---|---|
| `id` | uuid (pk, tự sinh) | ID job — dùng trong đường dẫn file |
| `user_id` | uuid (FK auth.users) | Chủ job (RLS: user chỉ thấy job của mình) |
| `job_type` | text `'video'` \| `'podcast'` | Loại job |
| `product` | text `'p9'` \| `'p9pro'` \| `'p10'` | Gói: p9 ≤ 3 phút, p9pro ≤ 15 phút, p10 podcast |
| `source_lang` | text \| null | Ngôn ngữ gốc: `vi/en/ja/ko/zh/fr/de/es`, null = tự nhận diện (chỉ video) |
| `target_lang` | text | Ngôn ngữ đích (podcast: ngôn ngữ đọc kịch bản) |
| `duration_seconds` | int \| null | Thời lượng video (giây), trang điền lúc upload |
| `source_path` | text \| null | Đường dẫn file gốc trong bucket `dubbing-source` |
| `result_path` | text \| null | Đường dẫn file kết quả trong bucket `dubbing-result` (chỉ khi `done`) |
| `status` | text | `queued` → `processing` → `done` \| `failed` |
| `error_message` | text \| null | Lý do lỗi khi `failed` (hiện cho khách nếu là lỗi do khách, vd vượt 3 phút) |
| `payload` | jsonb \| null | Dữ liệu riêng theo loại job (xem §3) |
| `created_at` / `updated_at` | timestamptz | Tự sinh / tự cập nhật |

## 2. Storage

| Bucket | Chế độ | Đường dẫn |
|---|---|---|
| `dubbing-source` | riêng tư | `<user_id>/<job_id>/source.mp4` (video gốc khách upload — gói Chuẩn sau khi nén, hoặc 1 file duy nhất) |
| `dubbing-source` | riêng tư | `<user_id>/<job_id>/part-001`, `part-002`, … (gói Pro: file gốc chia thành các chunk, mỗi chunk ≤ 40 MB; xem §3) |
| `dubbing-result` | công khai | `<user_id>/<job_id>/dubbed.mp4` (video P9) hoặc `<user_id>/<job_id>/podcast.mp3` (P10) |

Quyết định của chủ shop (19/11/2026): gói Chuẩn (≤ 3 phút) **nén ngay trên
trình duyệt khách** bằng ffmpeg.wasm xuống 720p/~2 Mbps trước khi upload
(3 phút ≈ 45 MB, dưới ngưỡng 50 MB/file của Supabase Free); gói Pro
(≤ 15 phút) **chia file thành nhiều chunk < 50 MB** rồi upload, worker ghép
lại (giữ chất lượng gốc). Nếu file Chuẩn sau khi nén vẫn > 48 MB thì hạ
bitrate nén xuống (~1,2 Mbps); nếu vẫn > 48 MB thì dùng luôn cơ chế chia
chunk như gói Pro. Giới hạn 50 MB/file lấy từ bảng giá Supabase (chưa thử
upload thật > 50 MB lúc viết hợp đồng này — xem §7).

RLS Storage: user chỉ upload/xem file gốc trong thư mục `<user_id>` của mình;
ai cũng đọc được file kết quả (link tải chia sẻ được, đường dẫn có UUID nên
không đoán được). Worker dùng service_role nên bypass RLS.

## 3. Payload

**Podcast (P10)** — `payload` bắt buộc:

```json
{
  "turns": [
    {"speaker": "host", "text": "Chào mừng các bạn đến với..."},
    {"speaker": "guest", "text": "Cảm ơn anh đã mời em..."}
  ],
  "lang": "vi",
  "voice_host": "vi-VN-NamMinhNeural",
  "voice_guest": "vi-VN-HoaiMyNeural"
}
```

- `turns`: mảng lượt thoại theo thứ tự; `speaker` là `"host"` (giọng nam)
  hoặc `"guest"` (giọng nữ). Tối đa 8000 ký tự toàn kịch bản.
- `lang`: ngôn ngữ đọc (`vi` mặc định).
- `voice_host` / `voice_guest`: tên giọng Edge TTS gợi ý (tùy chọn —
  worker dùng bảng giọng theo `lang` nếu thiếu).

**Video (P9)** — `payload` chỉ dùng khi chia chunk:

```json
{ "chunks": ["part-001", "part-002", "part-003"] }
```

- Không chia chunk (gói Chuẩn, 1 file): `payload` để null, `source_path` =
  `<user_id>/<job_id>/source.mp4`.
- Có chia chunk (gói Pro, hoặc Chuẩn nén xong vẫn > 48 MB): `payload.chunks`
  liệt kê đủ tên các chunk theo đúng thứ tự; `source_path` =
  `<user_id>/<job_id>/part-001` (chunk đầu — để trường này không bao giờ
  rỗng và luôn trỏ tới file thật). Các chunk là lát cắt byte liên tiếp của
  file gốc (mỗi chunk ≤ 40 MB), worker chỉ cần nối byte theo đúng thứ tự
  trong `payload.chunks` là khôi phục đúng file gốc (không cần ffmpeg
  concat). Trang web chỉ insert job SAU KHI upload hết các chunk/file xong
  — không tạo job rác khi upload lỗi giữa chừng.

- Gửi lại job failed (retry, từ 20/11/2026): RLS chỉ cho user update job
  của mình thành `failed`, KHÔNG cho update ngược về `queued` — nên trang
  tạo JOB MỚI trỏ cùng file nguồn: giữ nguyên `source_path` + `payload`
  của job cũ, `status='queued'`, id mới. Nếu job cũ chia chunk thì payload
  thêm `source_base: "<user_id>/<job_id cũ>"` để worker tải chunk từ thư
  mục cũ (không upload lại). Ngoài ra payload có thể mang `file_name`
  (tên file gốc để trang hiển thị, worker bỏ qua).

## 4. Luồng trang web cần làm (đợt sau)

**P9 — Dịch Video:**
1. Khách đăng nhập + có quyền p9/p9pro (kiểm tra như hiện tại — job cần
   `user_id` nên chưa đăng nhập thì yêu cầu đăng nhập trước). Giới hạn
   thời lượng ở client như hiện tại (≤ 3:00 Chuẩn, ≤ 15:00 Pro, > 15:00
   từ chối).
2. Sinh `job_id` trước bằng `crypto.randomUUID()` (để biết đường dẫn trước
   khi upload).
3. Gói Chuẩn: nén video bằng ffmpeg.wasm (core 1 luồng, không cần
   cross-origin isolation) xuống 720p/~2 Mbps, hiện tiến độ "Đang nén
   video trước khi gửi…". Nếu nén xong vẫn > 48 MB thì nén lại ở ~1,2 Mbps;
   nếu vẫn > 48 MB thì chia chunk như gói Pro. Upload 1 file lên
   `dubbing-source/<user_id>/<job_id>/source.mp4`.
   Gói Pro: chia file gốc thành các chunk ≤ 40 MB, upload từng chunk lên
   `dubbing-source/<user_id>/<job_id>/part-001`, `part-002`, … (hiện tiến
   độ từng phần, cho gửi lại nếu lỗi giữa chừng).
4. Chỉ insert 1 dòng `dubbing_jobs` SAU KHI upload xong
   (`job_type='video'`, `product` = p9/p9pro theo thời lượng,
   `source_lang`, `target_lang`, `duration_seconds`, `source_path` như §2,
   `payload` như §3, `status='queued'`). Upload/insert chạy với phiên của
   khách (RLS yêu cầu `auth.uid() = user_id`).
5. Poll trạng thái mỗi 10 giây: `supabase.from('dubbing_jobs').select('*')
   .eq('id', job_id).single()` — hiện tiến trình cho khách (`queued` =
   "Đang chờ xử lý", `processing` = "Đang lồng tiếng…"). Poll tiếp tục
   cho tới khi job `done`/`failed`; tải lại trang thì "Video của tôi"
   đọc lại job từ bảng và poll tiếp các job chưa xong.
6. Khi `status='done'`: hiện link tải
   `https://<ref>.supabase.co/storage/v1/object/public/dubbing-result/<result_path>`
   + nút tải về. Lưu job vào danh sách "Video của tôi" (đọc từ bảng, thay
   cho localStorage như hiện tại). Các job localStorage cũ vẫn hiển thị kèm
   bên dưới, đánh dấu "bản nháp cũ — gửi lại để xử lý thật".
7. Khi `status='failed'`: hiện "Video của bạn đang được xử lý thủ công,
   shop sẽ gửi lại qua Zalo/SĐT bạn để lại." và KHÔNG hiện link lỗi.

**P10 — Podcast AI:**
1. Khách tạo kịch bản như hiện tại (2 giọng) → bấm "Xuất file MP3" (nút mới,
   đặt cạnh nút tải .txt).
2. Yêu cầu đăng nhập trước (job cần `user_id`). Insert 1 dòng `dubbing_jobs`
   (`job_type='podcast'`, `product='p10'`, `target_lang` = ngôn ngữ kịch
   bản, `payload` như §3, `status='queued'`). Không cần upload file nguồn
   (`source_path` = null). Kịch bản lấy từ `currentScript` của trang
   (`who: "A"` → `speaker: "host"`, `"B"` → `"guest"`).
3. Poll như P9; khi `done` thì hiện link tải `podcast.mp3` từ bucket
   `dubbing-result` (dùng `result_path`); khi `failed` thì hiện thông báo
   xử lý thủ công như P9. Giữ nguyên nút nghe thử trong trình duyệt và
   copy/tải .txt hiện có.

## 5. Trạng thái & thời gian

- Worker chạy mỗi 5 phút (GitHub Actions cron), mỗi lượt xử lý tối đa 5 job.
- Video 3 phút: xử lý khoảng 4–8 phút (model Whisper base trên runner 2 CPU).
  Video 10 giây đo được local: 8,4 giây (model tiny, đã tải model từ trước).
- Job `failed` KHÔNG tự chạy lại — rơi về hàng chờ làm tay (xem `error_message`).

## 6. Giới hạn phải nói rõ trên trang (copy trung thực)

- "Video lồng tiếng là dạng thuyết minh đè lên hình — môi người trong video
  không khớp theo tiếng mới."
- "Video hoàn chỉnh thường xong trong vài phút; video dài có thể chờ lâu hơn."
- "Video gói Chuẩn được nén xuống 720p trước khi gửi để tải lên nhanh hơn —
  chất lượng hình ảnh có thể giảm nhẹ so với bản gốc. Video gói Pro giữ
  nguyên chất lượng gốc."

## 7. Giới hạn 50 MB/file + quy tắc vận hành (quyết định 19/11/2026)

- Supabase Free: upload tối đa 50 MB/file, tổng storage 1 GB, egress
  5 GB/tháng (theo bảng giá Supabase). Thiết kế nén (Chuẩn) + chia chunk
  (Pro) ở §2 là để không vượt ngưỡng này. **Đã kiểm chứng bằng upload
  thật 19/11/2026 ~13:41:** file MP4 hợp lệ 62.043.751 byte (~59,2 MiB,
  40 giây, 720p), đăng nhập tài khoản test, POST lên bucket dubbing-source
  → HTTP 400 `{"statusCode":"413","error":"Payload too large","message":
  "The object exceeded the maximum allowed size","code":"EntityTooLarge"}`.
  Giới hạn 50 MB/file được XÁC NHẬN — thiết kế nén (Chuẩn) + chia chunk
  (Pro, mỗi chunk ≤ 40 MB) là bắt buộc, không phải phỏng đoán.
- Mỗi job chỉ lưu 2 file trong Storage: video gốc (đã nén / các chunk) +
  video thành phẩm. Tự xóa sau 7 ngày kể từ khi giao (CHƯA code — làm ở
  bước dọn dẹp riêng; worker và trang chưa xóa file).
- Link tải hiện dùng URL công khai của bucket `dubbing-result` (đường dẫn
  có UUID nên không đoán được); signed URL có hạn sẽ thay thế khi có bước
  dọn dẹp tự động.
- Gần chạm 5 GB egress/tháng thì tạm dừng nhận job mới và báo chủ shop —
  CHƯA code phần tạm dừng này, chỉ ghi chú.
