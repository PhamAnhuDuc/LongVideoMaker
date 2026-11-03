"""Căn thời gian từng từ: Whisper (hoặc edge-tts) -> gán vào đúng chữ của kịch bản gốc."""
import re
import threading
import unicodedata
from difflib import SequenceMatcher

_model = None
_model_name = None
_lock = threading.Lock()


def key(w: str) -> str:
    w = unicodedata.normalize("NFD", w.lower().replace("đ", "d"))
    w = "".join(c for c in w if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]", "", w)


def _get_model(model_name):
    global _model, _model_name
    from faster_whisper import WhisperModel
    if _model is None or _model_name != model_name:
        _model = WhisperModel(model_name, device="cpu", compute_type="int8")
        _model_name = model_name
    return _model


def preload(model_name="base"):
    with _lock:
        _get_model(model_name)


def _decode(path):
    """Giải mã mp3 -> mảng float32 16kHz bằng ffmpeg (không phụ thuộc PyAV)."""
    import subprocess
    import numpy as np
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "f32le", "-ac", "1", "-ar", "16000", "-"],
                         capture_output=True, check=True).stdout
    return np.frombuffer(raw, dtype=np.float32)


def whisper_words(path, lang, model_name="base"):
    """Trả về [(start, end, text)] từ Whisper. Model chỉ nạp 1 lần, suy luận tuần tự."""
    audio = _decode(path)
    with _lock:
        model = _get_model(model_name)
        segs, _ = model.transcribe(audio, language=lang, word_timestamps=True,
                                   vad_filter=False, beam_size=1, condition_on_previous_text=False)
        return [(float(w.start), float(w.end), w.word.strip()) for sg in segs for w in (sg.words or [])]


def align_words(orig_words, heard, dur):
    """Gán (start, end) cho từng từ gốc. `heard` = [(s,e,text)] từ bộ nhận dạng."""
    n = len(orig_words)
    if n == 0:
        return []
    ok = [None] * n
    a = [key(w) for w in orig_words]
    b = [key(t) for _, _, t in heard]
    sm = SequenceMatcher(None, a, b, autojunk=False)
    for i, j, size in sm.get_matching_blocks():
        for k in range(size):
            if a[i + k]:
                ok[i + k] = (heard[j + k][0], heard[j + k][1])
    # nội suy các đoạn không khớp giữa hai mốc đã biết
    res = [None] * n
    i = 0
    while i < n:
        if ok[i]:
            res[i] = ok[i]
            i += 1
            continue
        j = i
        while j < n and not ok[j]:
            j += 1
        t0 = res[i - 1][1] if i > 0 else 0.0
        t1 = ok[j][0] if j < n else dur
        t1 = max(t1, t0)
        weights = [max(1, len(orig_words[k])) for k in range(i, j)]
        tot = sum(weights)
        t = t0
        for k, wgt in zip(range(i, j), weights):
            d = (t1 - t0) * wgt / tot
            res[k] = (t, t + d)
            t += d
        i = j
    # đảm bảo tăng dần
    prev = 0.0
    out = []
    for s, e in res:
        s = max(s, prev)
        e = max(e, s + 0.02)
        out.append((s, e))
        prev = s
    return out


def edge_words_to_pairs(orig_words, edge_bounds, dur):
    heard = [(s, e, t) for s, e, t in edge_bounds]
    return align_words(orig_words, heard, dur)
