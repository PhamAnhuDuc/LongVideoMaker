"""Biến kịch bản người dùng dán vào thành cấu trúc chương/cảnh."""
import re


def _split_long(par, max_words=48):
    sents = [s.strip() for s in re.split(r"(?<=[.!?…])\s+", par) if s.strip()]
    chunks, cur = [], []
    for s in sents:
        if cur and len(" ".join(cur + [s]).split()) > max_words:
            chunks.append(" ".join(cur))
            cur = []
        cur.append(s)
    if cur:
        chunks.append(" ".join(cur))
    return chunks


def parse_script(text: str, title: str = "", per_chapter: int = 12):
    """Quy ước: dòng bắt đầu bằng '#' = tên chương; đoạn cách nhau dòng trống = các cảnh."""
    chapters, cur = [], None
    items, buf = [], []
    for line in text.replace("\r", "").split("\n") + [""]:
        line = line.strip()
        if line.startswith("#") or not line:
            if buf:
                items.append(("p", " ".join(buf)))
                buf = []
            if line:
                items.append(("h", line.lstrip("#").strip()))
        else:
            buf.append(re.sub(r"[*_`]", "", line))

    for kind, t in items:
        if kind == "h":
            cur = {"title": t, "summary": "", "scenes": []}
            chapters.append(cur)
        else:
            if cur is None:
                cur = {"title": "Phần 1", "summary": "", "scenes": []}
                chapters.append(cur)
            for c in _split_long(t):
                cur["scenes"].append({"narration": c, "keywords": []})
    chapters = [c for c in chapters if c["scenes"]]
    if len(chapters) == 1 and chapters[0]["title"] == "Phần 1" and len(chapters[0]["scenes"]) > per_chapter:
        scenes = chapters[0]["scenes"]
        chapters = [{"title": f"Phần {i // per_chapter + 1}", "summary": "",
                     "scenes": scenes[i:i + per_chapter]} for i in range(0, len(scenes), per_chapter)]
    first = chapters[0]["scenes"][0]["narration"] if chapters else ""
    return {"title": title or (first[:60] + "…" if first else "Video"), "description": "", "chapters": chapters}
