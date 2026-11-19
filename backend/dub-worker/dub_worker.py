#!/usr/bin/env python3
"""
CongCuShop dub worker — lồng tiếng video (P9) + render podcast (P10),
MIỄN PHÍ và TỰ ĐỘNG, không dùng API trả phí nào.

Pipeline video (P9):
    video gốc -> faster-whisper (nghe lời thoại, chạy local)
             -> dịch text sang ngôn ngữ đích (Google Translate miễn phí,
                fallback MyMemory) -> Edge TTS đọc lại bằng giọng đích
             -> ffmpeg ghép tiếng lồng vào video (thay audio gốc, giữ hình ảnh)
             -> upload file kết quả lên Supabase Storage
Pipeline podcast (P10):
    kịch bản 2 giọng (JSON) -> Edge TTS từng lượt thoại (2 giọng khác nhau)
             -> ffmpeg nối thành 1 file MP3 -> upload lên Storage

GIỚI HẠN ĐÃ BIẾT (nói thật với khách, không giấu):
  - Lồng tiếng dạng thuyết minh đè lên hình (voice-over): môi người trong
    video KHÔNG khớp theo tiếng mới. Khớp môi chỉ có dịch vụ trả phí
    (HeyGen/Rask AI) mới làm được.
  - Dịch + đọc 1 lượt cho cả video (không căn từng câu theo mốc thời gian):
    giọng lồng có thể lệch nhịp so với lời gốc vài giây ở video dài.
  - Edge TTS là API không chính thức của Microsoft (miễn phí, dùng chung
    với trình duyệt Edge): giọng rất tốt nhưng không có SLA, có thể bị
    chặn. Fallback Piper TTS chạy hoàn toàn local nếu Edge lỗi. Edge TTS
    dùng WebSocket (wss): môi trường nào chặn WebSocket (vd proxy egress
    của VM dev này) thì Edge TTS không chạy được — Piper là đường thoát
    duy nhất ở đó. Dịch: Google Translate và MyMemory đều có giới hạn
    theo IP dùng chung; nếu cả hai cùng lỗi thì job failed để rơi về
    hàng chờ làm tay (đừng để khách chờ mà không có gì).

CHẠY:
  - Worker thật (GitHub Actions, mỗi 5 phút):
        SUPABASE_URL=... SUPABASE_SERVICE_ROLE_KEY=... python dub_worker.py --run-once
  - Test local không cần Supabase:
        python dub_worker.py --local-video test.mp4 --source-lang en --target-lang vi --out dubbed.mp4
        python dub_worker.py --local-podcast script.json --out podcast.mp3

BIẾN MÔI TRƯỜNG:
  SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY (bắt buộc ở chế độ worker;
    KHÔNG hardcode key nào vào code — key lấy từ GitHub Secrets khi deploy),
  WHISPER_MODEL (mặc định "base"; test nhanh dùng "tiny"),
  WORK_DIR (mặc định /tmp/dub-work),
  MAX_JOBS_PER_RUN (mặc định 5),
  PIPER_VOICE_VI / PIPER_VOICE_EN / PIPER_VOICE (đường dẫn file .onnx
    của giọng Piper theo ngôn ngữ đích, chỉ dùng khi Edge TTS lỗi),
  DUB_GOOGLE_TRANSLATE_URL / DUB_MYMEMORY_URL (chỉ để test local: trỏ
    sang mock server; production không đặt),
  RUN_TIMEOUT_MINUTES (mặc định 50 — worker tự dừng để không chồng chéo
    với lượt chạy Actions tiếp theo).
"""

import argparse
import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# Giới hạn thời lượng video theo gói (giây).
DURATION_LIMITS = {"p9": 180, "p9pro": 900}
# Podcast: giới hạn ký tự kịch bản (khoảng 25-30 phút đọc).
PODCAST_MAX_CHARS = 8000
# Tốc độ đọc tối đa cho phép khi ép giọng lồng vừa khung video (1.4x).
MAX_TEMPO = 1.4

# Giọng Edge TTS theo ngôn ngữ: (giọng nam, giọng nữ).
VOICES = {
    "vi": ("vi-VN-NamMinhNeural", "vi-VN-HoaiMyNeural"),
    "en": ("en-US-GuyNeural", "en-US-AriaNeural"),
    "ja": ("ja-JP-KeitaNeural", "ja-JP-NanamiNeural"),
    "ko": ("ko-KR-InJoonNeural", "ko-KR-SunHiNeural"),
    "zh": ("zh-CN-YunyangNeural", "zh-CN-XiaoxiaoNeural"),
    "fr": ("fr-FR-HenriNeural", "fr-FR-DeniseNeural"),
    "de": ("de-DE-ConradNeural", "de-DE-KatjaNeural"),
    "es": ("es-ES-AlvaroNeural", "es-ES-ElviraNeural"),
}

SOURCE_BUCKET = "dubbing-source"
RESULT_BUCKET = "dubbing-result"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("dub_worker")


# ---------------------------------------------------------------------------
# Tiện ích hệ thống
# ---------------------------------------------------------------------------
def run_cmd(cmd, timeout=600):
    """Chạy lệnh shell, log lại, ném lỗi nếu exit != 0."""
    log.info("$ %s", " ".join(str(c) for c in cmd))
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(
            f"lệnh thất bại ({r.returncode}): {' '.join(map(str, cmd))}\n"
            f"stderr: {r.stderr.strip()[-2000:]}"
        )
    return r


def ffprobe_duration(path):
    r = run_cmd([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "csv=p=0", str(path),
    ])
    return float(r.stdout.strip())


def ffprobe_has_audio(path):
    r = run_cmd([
        "ffprobe", "-v", "error", "-select_streams", "a",
        "-show_entries", "stream=index", "-of", "csv=p=0", str(path),
    ])
    return bool(r.stdout.strip())


def split_sentences(text, max_len=900):
    """Chia text thành đoạn ngắn theo câu, mỗi đoạn <= max_len ký tự."""
    parts = re.split(r"(?<=[.!?。！？…])\s+", text.strip())
    chunks, cur = [], ""
    for p in parts:
        if len(cur) + len(p) + 1 > max_len and cur:
            chunks.append(cur)
            cur = p
        else:
            cur = (cur + " " + p).strip() if cur else p
    if cur:
        chunks.append(cur)
    return [c for c in chunks if c]


# ---------------------------------------------------------------------------
# Bước 1: nghe lời thoại (faster-whisper, chạy local, miễn phí)
# ---------------------------------------------------------------------------
def transcribe(audio_path, source_lang, model_size):
    from faster_whisper import WhisperModel

    log.info("Whisper: model=%s, ngôn ngữ gốc=%s", model_size, source_lang or "tự nhận diện")
    model = WhisperModel(model_size, device="cpu", compute_type="int8")
    kwargs = dict(language=source_lang) if source_lang else {}
    try:
        segments, info = model.transcribe(str(audio_path), vad_filter=True, **kwargs)
    except Exception:
        log.warning("Whisper VAD lỗi, thử lại không dùng VAD")
        segments, info = model.transcribe(str(audio_path), vad_filter=False, **kwargs)
    text = " ".join(seg.text.strip() for seg in segments if seg.text.strip())
    detected = getattr(info, "language", None)
    log.info("Whisper: nhận diện ngôn ngữ=%s, %d ký tự lời thoại", detected, len(text))
    if not text:
        raise RuntimeError("no_speech_detected: không nghe được lời thoại nào trong video")
    return text, detected


# ---------------------------------------------------------------------------
# Bước 2: dịch (googletrans miễn phí, fallback MyMemory miễn phí)
# ---------------------------------------------------------------------------
def _translate_google(text, src, dest):
    """Dịch miễn phí qua endpoint web của Google Translate (client=gtx —
    cùng endpoint mà thư viện googletrans dùng, nhưng gọi trực tiếp bằng
    requests để không bị kẹt phiên bản httpx). URL đổi được qua biến
    DUB_GOOGLE_TRANSLATE_URL để test local (mock server)."""
    import requests

    url = os.environ.get(
        "DUB_GOOGLE_TRANSLATE_URL",
        "https://translate.googleapis.com/translate_a/single")
    dest_g = {"zh": "zh-CN"}.get(dest, dest)
    src_g = {"zh": "zh-CN"}.get(src, src) if src else "auto"
    out = []
    for chunk in split_sentences(text, max_len=3000):
        r = requests.get(
            url,
            params={"client": "gtx", "sl": src_g, "tl": dest_g,
                    "dt": "t", "q": chunk},
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        translated = "".join(seg[0] for seg in data[0] if seg and seg[0])
        if not translated:
            raise RuntimeError("Google Translate trả về rỗng")
        out.append(translated)
    return " ".join(out)


def _translate_mymemory(text, src, dest):
    import requests

    url = os.environ.get(
        "DUB_MYMEMORY_URL",
        "https://api.mymemory.translated.net/get")
    dest_mm = {"zh": "zh-CN"}.get(dest, dest)
    src_mm = src or "auto"
    out = []
    for chunk in split_sentences(text, max_len=450):
        r = requests.get(
            url,
            params={"q": chunk, "langpair": f"{src_mm}|{dest_mm}"},
            timeout=30,
        )
        r.raise_for_status()
        data = r.json().get("responseData", {})
        translated = data.get("translatedText", "")
        if not translated or translated.upper().startswith("QUERY LENGTH LIMIT"):
            raise RuntimeError("MyMemory từ chối (vượt giới hạn miễn phí)")
        out.append(translated)
    return " ".join(out)


def translate_text(text, src, dest):
    """Dịch text. src=None = tự nhận diện (chỉ Google làm được).
    Thứ tự: Google Translate -> MyMemory. Cả hai cùng lỗi thì job failed
    với lý do rõ ràng (để rơi về hàng chờ làm tay)."""
    if src and src == dest:
        return text
    try:
        log.info("Dịch bằng Google Translate: %s -> %s", src or "auto", dest)
        return _translate_google(text, src, dest)
    except Exception as e:
        log.warning("Google Translate lỗi (%s), chuyển sang MyMemory", e)
    log.info("Dịch bằng MyMemory: %s -> %s", src or "auto", dest)
    return _translate_mymemory(text, src, dest)


# ---------------------------------------------------------------------------
# Bước 3: đọc lại bằng giọng AI miễn phí (Edge TTS, fallback Piper local)
# ---------------------------------------------------------------------------
async def _edge_save(text, voice, out_path):
    import edge_tts

    # aiohttp trong edge-tts không tự đọc biến proxy của môi trường: phải
    # truyền proxy rõ ràng, nếu không nó nối thẳng và bị chặn (VM này chỉ
    # ra ngoài được qua proxy). GitHub Actions không đặt HTTPS_PROXY nên
    # không ảnh hưởng gì ở đó.
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    kwargs = {"proxy": proxy} if proxy else {}
    await edge_tts.Communicate(text, voice, **kwargs).save(str(out_path))


def _edge_ssl_context():
    """Edge TTS tự tạo SSL context từ certifi.where() nên KHÔNG tin CA của
    proxy MITM (môi trường VM này, mạng công ty...). Nếu SSL_CERT_FILE được
    đặt, đổi context của edge-tts sang context mặc định của hệ thống (đọc
    SSL_CERT_FILE). Môi trường bình thường (GitHub Actions) không đặt biến
    này nên không ảnh hưởng gì."""
    if not os.environ.get("SSL_CERT_FILE"):
        return
    try:
        import ssl
        import edge_tts.communicate as _etc
        _etc._SSL_CTX = ssl.create_default_context()
        log.info("Edge TTS: dùng SSL context hệ thống (SSL_CERT_FILE đã đặt)")
    except Exception as e:
        log.warning("Không đổi được SSL context của edge-tts (%s), thử tiếp", e)


def tts_edge(text, voice, out_path):
    _edge_ssl_context()
    chunks = split_sentences(text, max_len=800)
    tmp_files = []
    try:
        for i, chunk in enumerate(chunks):
            part = out_path.with_suffix(f".part{i}.mp3")
            asyncio.run(_edge_save(chunk, voice, part))
            tmp_files.append(part)
        if len(tmp_files) == 1:
            shutil.move(str(tmp_files[0]), str(out_path))
        else:
            _concat_audio(tmp_files, out_path)
    finally:
        for p in tmp_files:
            if p != out_path and p.exists():
                p.unlink(missing_ok=True)


def _concat_audio(parts, out_path):
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
        for p in parts:
            f.write(f"file '{p}'\n")
        list_file = f.name
    try:
        run_cmd(["ffmpeg", "-y", "-f", "concat", "-safe", "0",
                 "-i", list_file, "-c", "copy", str(out_path)])
    finally:
        os.unlink(list_file)


def tts_piper(text, voice_path, out_path):
    """Fallback local: cần file giọng Piper .onnx (biến PIPER_VOICE).
    Piper luôn xuất WAV dù đặt tên file là gì, nên ghi ra file .wav tạm
    rồi chuyển sang đúng định dạng của out_path (mp3) bằng ffmpeg — nếu
    không, bước nối file podcast (ffmpeg concat -c copy) sẽ lỗi vì lẫn
    WAV đội lốt .mp3 với MP3 thật."""
    if not voice_path or not Path(voice_path).exists():
        raise RuntimeError("Piper chưa cấu hình (thiếu PIPER_VOICE trỏ tới file .onnx)")
    piper_bin = shutil.which("piper") or str(
        Path(sys.executable).parent / "piper")
    tmp_wav = Path(out_path).with_suffix(".wav")
    cmd = [piper_bin, "--model", str(voice_path), "--output_file", str(tmp_wav)]
    p = subprocess.run(cmd, input=text, capture_output=True, text=True, timeout=600)
    if p.returncode != 0 or not tmp_wav.exists():
        raise RuntimeError(f"Piper lỗi: {p.stderr.strip()[-500:]}")
    if Path(out_path).suffix.lower() == ".mp3":
        run_cmd(["ffmpeg", "-y", "-i", str(tmp_wav),
                 "-c:a", "libmp3lame", "-q:a", "4", str(out_path)])
        tmp_wav.unlink(missing_ok=True)
    elif tmp_wav != Path(out_path):
        shutil.move(str(tmp_wav), str(out_path))


def _piper_voice_for(lang):
    """Chọn file giọng Piper theo ngôn ngữ đích. Thứ tự: biến riêng theo
    ngôn ngữ (PIPER_VOICE_VI / PIPER_VOICE_EN / ...) -> PIPER_VOICE chung."""
    return (os.environ.get(f"PIPER_VOICE_{lang.upper()}")
            or os.environ.get("PIPER_VOICE", ""))


def tts(text, lang, out_path, female=False):
    """Đọc text bằng giọng của ngôn ngữ đích. Trả về giọng đã dùng."""
    voices = VOICES.get(lang, VOICES["en"])
    voice = voices[1] if female else voices[0]
    try:
        log.info("Edge TTS: giọng %s (%d ký tự)", voice, len(text))
        tts_edge(text, voice, out_path)
        return voice
    except Exception as e:
        log.warning("Edge TTS lỗi (%s), thử Piper fallback", e)
    piper_voice = _piper_voice_for(lang)
    log.info("Piper TTS fallback: giọng %s", piper_voice or "?")
    tts_piper(text, piper_voice, out_path)
    return f"piper:{piper_voice}"


# ---------------------------------------------------------------------------
# Bước 4: ghép tiếng lồng vào video / nối các lượt thoại podcast
# ---------------------------------------------------------------------------
def fit_dub_to_video(dub_wav, video_duration, out_wav):
    """Ép audio lồng vừa đúng thời lượng video:
       - ngắn hơn -> đệm im lặng tới đủ; dài hơn -> tăng tốc tối đa 1.4x,
         vẫn dài thì cắt đuôi (ghi log cảnh báo)."""
    dub_dur = ffprobe_duration(dub_wav)
    log.info("Độ dài: video %.1fs, tiếng lồng %.1fs", video_duration, dub_dur)
    if dub_dur <= video_duration:
        run_cmd(["ffmpeg", "-y", "-i", str(dub_wav), "-af",
                 f"apad=whole_dur={video_duration}",
                 "-t", str(video_duration), str(out_wav)])
    else:
        tempo = dub_dur / video_duration
        if tempo <= MAX_TEMPO:
            log.info("Tăng tốc giọng lồng x%.2f cho vừa khung video", tempo)
            run_cmd(["ffmpeg", "-y", "-i", str(dub_wav), "-af",
                     f"atempo={tempo:.3f}", "-t", str(video_duration), str(out_wav)])
        else:
            log.warning("Giọng lồng dài gấp %.2fx video — tăng tốc 1.4x rồi cắt đuôi", tempo)
            run_cmd(["ffmpeg", "-y", "-i", str(dub_wav), "-af",
                     f"atempo={MAX_TEMPO}", "-t", str(video_duration), str(out_wav)])
    return out_wav


def mux_dubbed_video(video_path, dub_wav, video_duration, out_path):
    run_cmd([
        "ffmpeg", "-y", "-i", str(video_path), "-i", str(dub_wav),
        "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "128k",
        "-t", str(video_duration), "-movflags", "+faststart", str(out_path),
    ])
    return out_path


# ---------------------------------------------------------------------------
# Pipeline hoàn chỉnh trên file local (dùng chung cho worker + test local)
# ---------------------------------------------------------------------------
def process_video_file(src_video, source_lang, target_lang, workdir, model_size):
    """Video vào -> video đã lồng tiếng ra. Trả về đường dẫn file kết quả."""
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    duration = ffprobe_duration(src_video)
    log.info("Video nguồn: %s, thời lượng %.1fs", src_video, duration)

    wav = workdir / "original.wav"
    run_cmd(["ffmpeg", "-y", "-i", str(src_video), "-vn",
             "-ac", "1", "-ar", "16000", str(wav)])
    transcript, detected = transcribe(wav, source_lang, model_size)
    src = source_lang or detected or "en"
    translated = translate_text(transcript, src, target_lang)
    log.info("Bản dịch (%s -> %s): %s", src, target_lang,
             translated[:200] + ("..." if len(translated) > 200 else ""))

    dub_mp3 = workdir / "dub.mp3"
    voice = tts(translated, target_lang, dub_mp3)
    log.info("Đã đọc giọng %s", voice)

    dub_wav = workdir / "dub.wav"
    run_cmd(["ffmpeg", "-y", "-i", str(dub_mp3), str(dub_wav)])
    fitted = workdir / "dub_fitted.wav"
    fit_dub_to_video(dub_wav, duration, fitted)

    out_path = workdir / "dubbed.mp4"
    mux_dubbed_video(src_video, fitted, duration, out_path)
    log.info("Xong video trong %.1fs -> %s", time.time() - t0, out_path)
    return out_path


def process_podcast_file(script_json, workdir):
    """Kịch bản JSON {"turns":[{"speaker":"host"|"guest","text":"..."}]}
       -> 1 file MP3. Trả về đường dẫn file kết quả."""
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    if isinstance(script_json, (str, Path)):
        data = json.loads(Path(script_json).read_text(encoding="utf-8"))
    else:
        data = script_json
    turns = data["turns"]
    lang = data.get("lang", "vi")
    total_chars = sum(len(t.get("text", "")) for t in turns)
    if total_chars > PODCAST_MAX_CHARS:
        raise RuntimeError(
            f"script_too_long: kịch bản {total_chars} ký tự, vượt giới hạn "
            f"{PODCAST_MAX_CHARS} ký tự")
    parts = []
    silence = workdir / "silence.mp3"
    run_cmd(["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono",
             "-t", "0.4", "-c:a", "libmp3lame", "-q:a", "4", str(silence)])
    for i, turn in enumerate(turns):
        text = (turn.get("text") or "").strip()
        if not text:
            continue
        female = turn.get("speaker") == "guest"
        part = workdir / f"turn{i}.mp3"
        voice = tts(text, lang, part, female=female)
        log.info("Lượt %d (%s, giọng %s): %d ký tự",
                 i, turn.get("speaker"), voice, len(text))
        parts.append(part)
        parts.append(silence)
    if not parts:
        raise RuntimeError("empty_script: kịch bản không có lượt thoại nào")
    out_path = workdir / "podcast.mp3"
    _concat_audio(parts[:-1], out_path)  # bỏ khoảng lặng cuối
    log.info("Xong podcast trong %.1fs -> %s", time.time() - t0, out_path)
    return out_path


# ---------------------------------------------------------------------------
# Chế độ worker: lấy job từ Supabase, xử lý, cập nhật trạng thái
# ---------------------------------------------------------------------------
def get_supabase():
    from supabase import create_client

    url = os.environ.get("SUPABASE_URL", "")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    if not url or not key:
        raise RuntimeError("thiếu SUPABASE_URL hoặc SUPABASE_SERVICE_ROLE_KEY")
    return create_client(url, key)


def fetch_queued_jobs(sb, limit):
    res = (sb.table("dubbing_jobs")
           .select("*")
           .eq("status", "queued")
           .order("created_at", desc=False)
           .limit(limit)
           .execute())
    return res.data or []


def update_job(sb, job_id, **fields):
    # updated_at do trigger set_updated_at() của DB tự cập nhật — không gửi
    # chuỗi "now()" qua API (PostgREST sẽ lỗi ép kiểu).
    (sb.table("dubbing_jobs").update(fields).eq("id", job_id).execute())


def download_source_video(sb, job, workdir):
    """Tải video gốc về workdir. Hỗ trợ 2 dạng upload từ trang (xem
    CONTRACT.md §2-§3):
      - 1 file: source_path = <uid>/<job_id>/source.mp4 (gói Chuẩn sau nén).
      - chia chunk: payload.chunks = ["part-001", ...], source_path trỏ
        chunk đầu. Các chunk là lát cắt byte liên tiếp của file gốc (mỗi
        chunk <= 40 MB) nên chỉ cần nối byte theo đúng thứ tự trong
        payload.chunks là khôi phục đúng file gốc (không cần ffmpeg concat).
    Trả về đường dẫn file source.mp4 local."""
    src_local = Path(workdir) / "source.mp4"
    payload = job.get("payload") or {}
    chunks = payload.get("chunks") if isinstance(payload, dict) else None
    if chunks:
        base = f"{job['user_id']}/{job['id']}"
        log.info("Job chia chunk: %d phần, nối byte theo thứ tự", len(chunks))
        with open(src_local, "wb") as out_f:
            for name in chunks:
                data = sb.storage.from_(SOURCE_BUCKET).download(f"{base}/{name}")
                out_f.write(data)
        log.info("Đã ghép %d chunk -> %s (%d bytes)",
                 len(chunks), src_local, src_local.stat().st_size)
    else:
        data = sb.storage.from_(SOURCE_BUCKET).download(job["source_path"])
        src_local.write_bytes(data)
    return src_local


def process_job(sb, job, work_root, model_size):
    job_id = job["id"]
    job_type = job["job_type"]
    product = job["product"]
    workdir = Path(work_root) / job_id
    workdir.mkdir(parents=True, exist_ok=True)
    log.info("=== Job %s (%s, %s) ===", job_id, job_type, product)
    update_job(sb, job_id, status="processing")
    try:
        if job_type == "video":
            limit = DURATION_LIMITS.get(product, 180)
            dur = job.get("duration_seconds") or 0
            if dur and dur > limit:
                raise RuntimeError(
                    f"duration_exceeded: video {dur}s vượt giới hạn gói "
                    f"{product} ({limit}s)")
            src_local = download_source_video(sb, job, workdir)
            real_dur = ffprobe_duration(src_local)
            if real_dur > limit:
                raise RuntimeError(
                    f"duration_exceeded: video thực tế {real_dur:.0f}s vượt "
                    f"giới hạn gói {product} ({limit}s)")
            out = process_video_file(
                src_local, job.get("source_lang") or None,
                job.get("target_lang") or "vi", workdir, model_size)
            remote = f"{job['user_id']}/{job_id}/dubbed.mp4"
            sb.storage.from_(RESULT_BUCKET).upload(
                remote, str(out), {"content-type": "video/mp4", "upsert": "true"})
        else:  # podcast
            payload = job.get("payload") or {}
            if not payload.get("turns"):
                raise RuntimeError("empty_script: job podcast thiếu kịch bản (payload.turns)")
            out = process_podcast_file(payload, workdir)
            remote = f"{job['user_id']}/{job_id}/podcast.mp3"
            sb.storage.from_(RESULT_BUCKET).upload(
                remote, str(out), {"content-type": "audio/mpeg", "upsert": "true"})
        update_job(sb, job_id, status="done", result_path=remote, error_message=None)
        log.info("=== Job %s DONE: %s ===", job_id, remote)
    except Exception as e:
        log.error("=== Job %s FAILED: %s ===", job_id, e)
        update_job(sb, job_id, status="failed", error_message=str(e)[:500])
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def run_once():
    t0 = time.time()
    timeout = int(os.environ.get("RUN_TIMEOUT_MINUTES", "50")) * 60
    limit = int(os.environ.get("MAX_JOBS_PER_RUN", "5"))
    model_size = os.environ.get("WHISPER_MODEL", "base")
    work_root = os.environ.get("WORK_DIR", "/tmp/dub-work")
    sb = get_supabase()
    jobs = fetch_queued_jobs(sb, limit)
    log.info("Tìm thấy %d job đang chờ", len(jobs))
    for job in jobs:
        if time.time() - t0 > timeout:
            log.warning("Hết thời gian chạy, dừng lại — job còn lại để lượt sau")
            break
        process_job(sb, job, work_root, model_size)
    log.info("Worker xong trong %.1fs", time.time() - t0)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="CongCuShop dub worker (miễn phí)")
    ap.add_argument("--run-once", action="store_true",
                    help="chạy 1 lượt worker: lấy job queued từ Supabase và xử lý")
    ap.add_argument("--local-video", help="test local: video nguồn")
    ap.add_argument("--source-lang", default=None,
                    help="ngôn ngữ gốc (vi/en/ja/ko/zh/fr/de/es), để trống = tự nhận diện")
    ap.add_argument("--target-lang", default="vi", help="ngôn ngữ đích (mặc định vi)")
    ap.add_argument("--local-podcast", help="test local: file JSON kịch bản podcast")
    ap.add_argument("--out", help="file kết quả (test local)")
    ap.add_argument("--model", default=os.environ.get("WHISPER_MODEL", "base"),
                    help="model Whisper: tiny/base/small (mặc định base)")
    args = ap.parse_args()

    if args.run_once:
        run_once()
    elif args.local_video:
        tmpdir = tempfile.mkdtemp(prefix="dub-test-")
        try:
            out = process_video_file(
                Path(args.local_video), args.source_lang, args.target_lang,
                tmpdir, args.model)
            dest = Path(args.out) if args.out else Path("dubbed.mp4")
            shutil.copy(out, dest)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
        log.info("File kết quả: %s (video %.1fs, có audio: %s)",
                 dest, ffprobe_duration(dest), ffprobe_has_audio(dest))
    elif args.local_podcast:
        tmpdir = tempfile.mkdtemp(prefix="dub-test-")
        try:
            out = process_podcast_file(args.local_podcast, tmpdir)
            dest = Path(args.out) if args.out else Path("podcast.mp3")
            shutil.copy(out, dest)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
        log.info("File kết quả: %s (thời lượng %.1fs)",
                 dest, ffprobe_duration(dest))
    else:
        ap.print_help()
        sys.exit(2)


if __name__ == "__main__":
    main()
