"""Giọng đọc bằng edge-tts + tạo phụ đề theo câu."""
import asyncio
import random
import re
import threading
import time
from pathlib import Path

import edge_tts

from .util import duration


def clean_text(t: str) -> str:
    t = re.sub(r"[*_#`~<>\[\]]", "", t)
    return re.sub(r"\s+", " ", t).strip()


# Server edge-tts hay chặn tạm (trả "No audio was received") khi gọi dồn dập.
# Khi một luồng bị chặn, mọi luồng cùng nghỉ một lúc rồi mới gọi tiếp.
_cool_lock = threading.Lock()
_cool_until = 0.0


def _wait_cooldown():
    while True:
        with _cool_lock:
            left = _cool_until - time.time()
        if left <= 0:
            return
        time.sleep(min(left, 1.0))


def _set_cooldown(sec):
    global _cool_until
    with _cool_lock:
        _cool_until = max(_cool_until, time.time() + sec)


async def _synth(text, voice, rate, out: Path, boundary="SentenceBoundary"):
    com = edge_tts.Communicate(text, voice, rate=f"{rate:+d}%", boundary=boundary)
    cues = []
    tmp = out.with_suffix(".part")
    try:
        with open(tmp, "wb") as f:
            async for ch in com.stream():
                if ch["type"] == "audio":
                    f.write(ch["data"])
                elif ch["type"] == boundary:
                    cues.append((ch["offset"] / 1e7, (ch["offset"] + ch["duration"]) / 1e7, ch["text"]))
        tmp.replace(out)  # chỉ ghi đè file thật khi đã nhận đủ audio
    finally:
        tmp.unlink(missing_ok=True)
    return cues


# ---------- Gemini TTS ----------
GEMINI_PREFIX = "gemini:"
_gem_lock = threading.Lock()
_gem_switched = {}   # model TTS quá tải -> model dự phòng (nhớ trong lần chạy này)


def is_gemini(voice: str) -> bool:
    return voice.startswith(GEMINI_PREFIX)


def _gemini_tts(text, voice, rate, out: Path, style="", model=None, tries=6):
    """Đọc bằng Gemini TTS, ghi ra mp3. rate (%) áp dụng bằng atempo (không đổi cao độ)."""
    import os
    from google import genai
    from google.genai import errors, types
    from .config import GEMINI_TTS_FALLBACK, GEMINI_TTS_MODEL
    from .util import run

    model = model or GEMINI_TTS_MODEL
    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not key:
        raise RuntimeError("Thiếu GEMINI_API_KEY cho giọng Gemini.")
    client = genai.Client(api_key=key, http_options=types.HttpOptions(timeout=180000))
    name = voice[len(GEMINI_PREFIX):]
    prompt = f"{style.strip().rstrip(':')}:\n{text}" if style.strip() else text
    with _gem_lock:
        model = _gem_switched.get(model, model)
    asked, busy, last = model, 0, None
    for i in range(tries):
        try:
            r = client.models.generate_content(
                model=model, contents=prompt,
                config=types.GenerateContentConfig(
                    response_modalities=["AUDIO"],
                    speech_config=types.SpeechConfig(voice_config=types.VoiceConfig(
                        prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=name)))))
            part = r.candidates[0].content.parts[0].inline_data
            if not part or not part.data:
                raise RuntimeError("Gemini không trả về audio")
            break
        except errors.ClientError as e:
            last = e
            if e.code != 429:   # sai tên giọng / model -> báo ngay
                raise RuntimeError(f"Gemini TTS lỗi: {e}") from e
            if "PerDay" in str(e):   # hết hạn mức NGÀY -> chờ cũng vô ích
                if model != GEMINI_TTS_FALLBACK:
                    model = GEMINI_TTS_FALLBACK
                    with _gem_lock:
                        _gem_switched[asked] = model
                    print(f"[gemini-tts] hết hạn mức ngày, thử {model}", flush=True)
                    continue
                raise RuntimeError(
                    "Giọng Gemini đã hết hạn mức miễn phí hôm nay. Các cảnh đã đọc xong được giữ lại: "
                    "mai bấm render tiếp, hoặc đổi sang giọng Microsoft, hoặc bật thanh toán cho key Gemini.") from e
            wait = min(10 * (i + 1), 60) + random.uniform(0, 3)   # hết hạn mức/phút -> chờ
            print(f"[gemini-tts] 429, chờ {wait:.0f}s", flush=True)
            time.sleep(wait)
        except Exception as e:   # 503 quá tải / mạng
            last, busy = e, busy + 1
            if busy >= 2 and model != GEMINI_TTS_FALLBACK:
                model = GEMINI_TTS_FALLBACK
                with _gem_lock:
                    _gem_switched[asked] = model
                print(f"[gemini-tts] quá tải, chuyển sang {model}", flush=True)
                continue
            time.sleep(min(3 * 2 ** i, 30))
    else:
        raise RuntimeError(f"Gemini TTS lỗi sau {tries} lần thử: {last}")

    raw = out.with_suffix(".gem")
    raw.write_bytes(part.data)
    # model mới trả WAV có header; model cũ trả PCM 16-bit 24kHz mono thô
    src = ["-i", str(raw)] if part.data[:4] == b"RIFF" else ["-f", "s16le", "-ar", "24000", "-ac", "1", "-i", str(raw)]
    tempo = max(0.5, min(2.0, 1 + rate / 100))
    af = ["-af", f"atempo={tempo:.3f}"] if rate else []
    codec = ["-c:a", "pcm_s16le"] if out.suffix == ".wav" else ["-c:a", "libmp3lame", "-b:a", "128k"]
    try:
        run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *src, *af, *codec, str(out)])
    finally:
        raw.unlink(missing_ok=True)


def _silences(wav: Path):
    """Trung điểm các khoảng lặng (giây) trong file audio."""
    import subprocess
    r = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(wav), "-af", "silencedetect=noise=-35dB:d=0.2",
                        "-f", "null", "-"], capture_output=True, text=True, encoding="utf-8", errors="ignore")
    starts = [float(x) for x in re.findall(r"silence_start: ([\d.]+)", r.stderr)]
    ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", r.stderr)]
    return [(a + b) / 2 for a, b in zip(starts, ends)]


def gemini_chapter(texts, voice, rate, outs, style=""):
    """Đọc cả một chương bằng MỘT lần gọi Gemini (tiết kiệm hạn mức, giọng liền mạch),
    rồi cắt thành từng cảnh tại khoảng lặng gần vị trí ước tính nhất. Trả về [độ dài từng cảnh]."""
    from .util import run
    texts = [clean_text(t) for t in texts]
    wav = outs[0].parent.parent / f"_chapter_{outs[0].parent.name}.wav"
    _gemini_tts("\n\n".join(texts), voice, rate, wav, style)
    try:
        total = duration(wav)
        sil = _silences(wav)
        lens = [len(t) for t in texts]
        cuts, acc, prev = [0.0], 0, 0.0
        for i in range(len(texts) - 1):
            acc += lens[i]
            est = total * acc / sum(lens)
            win = max(2.5, 0.3 * total * lens[i] / sum(lens))   # chỉ tìm khoảng lặng quanh vị trí ước tính
            cand = [x for x in sil if prev + 1.0 < x < total - 1.0 and abs(x - est) <= win]
            prev = min(cand, key=lambda x: abs(x - est)) if cand else max(est, prev + 1.0)
            cuts.append(prev)
        cuts.append(total)
        durs = []
        for i, out in enumerate(outs):
            run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(wav),
                 "-ss", f"{cuts[i]:.3f}", "-to", f"{cuts[i + 1]:.3f}",
                 "-c:a", "libmp3lame", "-b:a", "128k", str(out)])
            durs.append(duration(out))
        return durs
    finally:
        wav.unlink(missing_ok=True)


def whisper_word_times(text, out: Path, d, lang, whisper_model):
    """Mốc thời gian từng từ bằng Whisper (None nếu lỗi)."""
    from . import align
    words = clean_text(text).split()
    try:
        heard = align.whisper_words(out, lang, whisper_model)
        pairs = align.align_words(words, heard, d)
        return [(a, b, w) for (a, b), w in zip(pairs, words)]
    except Exception as e:
        print("Whisper lỗi, dùng timing theo câu:", e)
        return None


def synth(text, voice, rate, out: Path, timing="sentence", lang="vi", whisper_model="base", tries=10,
          style="", fallback_voice=None):
    """Trả về (độ dài giây, [(start,end,câu)], [(start,end,từ)] hoặc None)."""
    from . import align
    text = clean_text(text)
    orig_words = text.split()
    words = None
    d = cues = None
    if is_gemini(voice):
        try:
            _gemini_tts(text, voice, rate, out, style)
            d = duration(out)
            if d <= 0:
                raise RuntimeError("audio rỗng")
            # Gemini không trả mốc thời gian -> phụ đề chia theo độ dài câu (hoặc Whisper nếu bật)
            cues = proportional(text, d)
        except Exception as e:
            if not fallback_voice:
                raise
            print(f"[tts] Gemini lỗi, dùng giọng dự phòng {fallback_voice}: {e}", flush=True)
            voice, d = fallback_voice, None
    if d is not None:
        pass
    elif timing == "word":  # thử mốc từng từ của edge-tts (chỉ một số giọng hỗ trợ)
        try:
            _wait_cooldown()
            bounds = asyncio.run(_synth(text, voice, rate, out, "WordBoundary"))
            d = duration(out)
            if bounds and d > 0:
                pairs = align.align_words(orig_words, bounds, d)
                words = [(a, b, w) for (a, b), w in zip(pairs, orig_words)]
                cues = proportional(text, d)
        except Exception:
            d = None
    if d is None:
        last = None
        for i in range(tries):
            try:
                _wait_cooldown()
                cues = asyncio.run(_synth(text, voice, rate, out))
                d = duration(out)
                if d <= 0:
                    raise RuntimeError("audio rỗng")
                break
            except Exception as e:
                last = e
                wait = min(3 * 2 ** i, 60) + random.uniform(0, 2)  # 3s, 6s, 12s… tối đa 60s
                print(f"[tts] lỗi lần {i + 1}/{tries}: {e} -> chờ {wait:.0f}s", flush=True)
                _set_cooldown(wait)
        else:
            raise RuntimeError(f"edge-tts lỗi sau {tries} lần thử (server Microsoft đang chặn tạm, "
                               f"đợi vài phút rồi bấm render lại — các cảnh đã xong sẽ được giữ): {last}")
        cues = [(max(a, cues[i - 1][1]) if i else a, b, t) for i, (a, b, t) in enumerate(cues)]
        if not cues:  # không có mốc thời gian -> chia theo độ dài ký tự
            cues = proportional(text, d)
    if timing in ("word", "whisper") and words is None:
        try:
            heard = align.whisper_words(out, lang, whisper_model)
            pairs = align.align_words(orig_words, heard, d)
            words = [(a, b, w) for (a, b), w in zip(pairs, orig_words)]
        except Exception as e:  # Whisper lỗi -> vẫn dùng phụ đề theo câu
            print("Whisper lỗi, dùng timing theo câu:", e)
    return d, cues, words


def word_cues(words, max_words):
    """Gom từng từ đã có mốc thời gian thành các dòng phụ đề ngắn."""
    out, cur = [], []
    for w in words:
        cur.append(w)
        if len(cur) >= max_words or re.search(r"[.!?…,;:]$", w[2]) and len(cur) >= max_words // 2:
            out.append(cur)
            cur = []
    if cur:
        out.append(cur)
    cues = []
    for i, ch in enumerate(out):
        st = ch[0][0]
        en = ch[-1][1]
        nxt = out[i + 1][0][0] if i + 1 < len(out) else en + 0.3
        cues.append((st, max(min(nxt, en + 0.35), st + 0.2), " ".join(x[2] for x in ch)))
    return cues


def proportional(text, dur):
    sents = [s for s in re.split(r"(?<=[.!?…])\s+", text) if s]
    total = sum(len(s) for s in sents) or 1
    t, cues = 0.0, []
    for s in sents:
        d = dur * len(s) / total
        cues.append((t, t + d, s))
        t += d
    return cues


def split_cues(cues, max_words):
    """Tách mỗi câu thành các dòng phụ đề ngắn, chia thời gian theo số ký tự."""
    out = []
    for st, en, text in cues:
        words = text.split()
        n = max(1, -(-len(words) // max_words))
        size = -(-len(words) // n)
        chunks = [" ".join(words[i:i + size]) for i in range(0, len(words), size)]
        total = sum(len(c) for c in chunks) or 1
        t = st
        for c in chunks:
            d = (en - st) * len(c) / total
            out.append((t, t + d, c))
            t += d
    return out
