"""Giọng đọc bằng edge-tts + tạo phụ đề theo câu."""
import asyncio
import re
import time
from pathlib import Path

import edge_tts

from .util import duration


def clean_text(t: str) -> str:
    t = re.sub(r"[*_#`~<>\[\]]", "", t)
    return re.sub(r"\s+", " ", t).strip()


async def _synth(text, voice, rate, out: Path, boundary="SentenceBoundary"):
    com = edge_tts.Communicate(text, voice, rate=f"{rate:+d}%", boundary=boundary)
    cues = []
    with open(out, "wb") as f:
        async for ch in com.stream():
            if ch["type"] == "audio":
                f.write(ch["data"])
            elif ch["type"] == boundary:
                cues.append((ch["offset"] / 1e7, (ch["offset"] + ch["duration"]) / 1e7, ch["text"]))
    return cues


def synth(text, voice, rate, out: Path, timing="sentence", lang="vi", whisper_model="base", tries=6):
    """Trả về (độ dài giây, [(start,end,câu)], [(start,end,từ)] hoặc None)."""
    from . import align
    text = clean_text(text)
    orig_words = text.split()
    words = None
    d = cues = None
    if timing == "word":  # thử mốc từng từ của edge-tts (chỉ một số giọng hỗ trợ)
        try:
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
                cues = asyncio.run(_synth(text, voice, rate, out))
                d = duration(out)
                if d <= 0:
                    raise RuntimeError("audio rỗng")
                break
            except Exception as e:
                last = e
                time.sleep(1.5 * (i + 1))
        else:
            raise RuntimeError(f"edge-tts lỗi: {last}")
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
