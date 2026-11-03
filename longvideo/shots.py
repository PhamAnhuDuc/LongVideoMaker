"""Shot list theo từng câu: mỗi câu lời đọc có từ khóa hình riêng, cắt clip đúng lúc chuyển câu."""
import re

from .tts import clean_text

MIN_SHOT = 2.0   # câu ngắn hơn (giây) thì gộp với câu bên cạnh, tránh cắt hình quá gấp
LEAD = 0.15      # chuyển hình sớm hơn chữ đầu câu một chút cho tự nhiên


def split_sentences(text: str):
    return [s.strip() for s in re.split(r"(?<=[.!?…])\s+", text.strip()) if s.strip()]


def _words(t):
    return clean_text(t).split()


def shots_for(sc: dict):
    """Danh sách shot [{text, kw}] khớp với narration hiện tại.
    Nếu narration đã bị sửa khiến shots cũ không khớp -> tách câu lại, giữ từ khóa theo thứ tự."""
    shots = sc.get("shots") or []
    if shots and sum((_words(x.get("text", "")) for x in shots), []) == _words(sc["narration"]):
        return [{"text": x["text"], "kw": [k for k in x.get("kw", []) if k]} for x in shots]
    sents = split_sentences(sc["narration"]) or [sc["narration"]]
    old = [x.get("kw", []) for x in shots]
    return [{"text": t, "kw": old[i] if i < len(old) else []} for i, t in enumerate(sents)]


def _word_time_fn(meta: dict, n: int, dur: float):
    """Hàm: chỉ số từ -> thời điểm (giây) trong file giọng đọc."""
    words = meta.get("words")
    if words and len(words) == n:
        return lambda i: words[i][0] if i < n else dur
    # dựng mốc từ phụ đề theo câu: (chỉ số từ, thời gian)
    anchors, idx = [(0, 0.0)], 0
    for s, e, txt in meta.get("cues") or []:
        anchors.append((idx, s))
        idx += len(txt.split())
        anchors.append((idx, e))
    anchors.append((idx, dur))
    scale = n / idx if idx else 1.0
    anchors = [(a * scale, t) for a, t in anchors]

    def f(i):
        if i >= n:
            return dur
        for (a0, t0), (a1, t1) in zip(anchors, anchors[1:]):
            if a0 <= i <= a1:
                return t0 if a1 == a0 else t0 + (t1 - t0) * (i - a0) / (a1 - a0)
        return dur * i / max(1, n)
    return f


def timeline(sc: dict, meta: dict, total: float):
    """[(start, end, [từ khóa ưu tiên…])] phủ kín 0..total, mỗi phần ứng với 1 câu (hoặc vài câu ngắn gộp)."""
    shots = shots_for(sc)
    fallback = [k for k in sc.get("keywords", []) if k]
    n = sum(len(_words(x["text"])) for x in shots)
    at = _word_time_fn(meta, n, meta.get("dur", total))
    out, idx = [], 0
    for k, x in enumerate(shots):
        start = 0.0 if k == 0 else max(0.0, at(idx) - LEAD)
        kws = list(dict.fromkeys(x["kw"] + fallback))  # cụ thể -> chung -> từ khóa cả cảnh
        out.append([start, None, kws])
        idx += len(_words(x["text"]))
    for k in range(len(out)):
        out[k][1] = out[k + 1][0] if k + 1 < len(out) else total
    # gộp câu quá ngắn vào câu trước (hoặc câu sau nếu là câu đầu)
    merged = []
    for s, e, kws in out:
        if merged and (e - s < MIN_SHOT or merged[-1][1] - merged[-1][0] < MIN_SHOT):
            prev = merged[-1]
            if prev[1] - prev[0] < e - s:   # giữ từ khóa của câu dài hơn
                prev[2] = kws
            prev[1] = e
        else:
            merged.append([s, e, kws])
    return [(s, e, kws or ["nature"]) for s, e, kws in merged if e - s > 0.01]
