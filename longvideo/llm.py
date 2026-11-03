"""Viết kịch bản dài bằng Gemini: dàn ý -> từng chương -> các cảnh."""
import json
import math
import os
import re
import time

from .config import LANG_NAME, WORDS_PER_MIN, Settings


def _client():
    from google import genai
    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not key:
        raise RuntimeError("Thiếu GEMINI_API_KEY (nhập ở thanh bên hoặc file .env).")
    return genai.Client(api_key=key)


def _parse_json(text: str):
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    return json.loads(text)


def ask_json(prompt: str, model: str, temperature=0.8, tries=5):
    from google.genai import types
    client = _client()
    last = None
    for i in range(tries):
        try:
            r = client.models.generate_content(
                model=model, contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json", temperature=temperature))
            return _parse_json(r.text)
        except Exception as e:  # 429/503/JSON lỗi -> thử lại
            last = e
            time.sleep(min(2 ** i * 2, 30))
    raise RuntimeError(f"Gemini lỗi sau {tries} lần thử: {last}")


def plan(s: Settings):
    n = max(3, round(s.minutes / 2.5))
    total_words = s.minutes * WORDS_PER_MIN[s.language]
    return n, total_words // n


def make_outline(s: Settings):
    n, per = plan(s)
    lang = LANG_NAME[s.language]
    prompt = f"""Bạn là biên kịch video YouTube dài. Lập dàn ý cho một video thuyết minh dài khoảng {s.minutes} phút.
Chủ đề: {s.topic}
Ngôn ngữ video: {lang}
Giọng điệu: {s.tone}
Khán giả: {s.audience}
Ghi chú thêm: {s.notes or 'không'}

Yêu cầu: đúng {n} chương, có mở đầu cuốn hút ở chương 1 và kết luận ở chương cuối, các chương nối tiếp logic, không lặp ý.
Trả về JSON: {{"title": "tiêu đề video ({lang})", "description": "mô tả 2-3 câu ({lang})",
"chapters": [{{"title": "tên chương ({lang})", "summary": "các ý chính sẽ nói trong chương, 2-3 câu ({lang})"}}]}}"""
    out = ask_json(prompt, s.gemini_model)
    chapters = out["chapters"]
    return {"title": out.get("title", s.topic), "description": out.get("description", ""),
            "chapters": [{"title": c["title"], "summary": c.get("summary", ""), "scenes": []}
                         for c in chapters]}


def _chapter_prompt(s, project, idx, target, prev_tail, short_note=""):
    lang = LANG_NAME[s.language]
    chs = project["chapters"]
    outline = "\n".join(f"{i+1}. {c['title']} - {c['summary']}" for i, c in enumerate(chs))
    ch = chs[idx]
    pos = ("Đây là chương MỞ ĐẦU: bắt đầu bằng hook cuốn hút, giới thiệu chủ đề." if idx == 0 else
           "Đây là chương CUỐI: tổng kết, kết luận, lời kêu gọi hành động ngắn." if idx == len(chs) - 1 else
           "Đây là chương giữa: KHÔNG chào hỏi, KHÔNG tóm tắt cả video, nối mạch tự nhiên từ chương trước.")
    return f"""Bạn là biên kịch video thuyết minh. Viết lời đọc cho MỘT chương của video "{project['title']}".
Ngôn ngữ lời đọc: {lang}. Giọng điệu: {s.tone}. Khán giả: {s.audience}. {s.notes}

Dàn ý toàn video:
{outline}

Chương cần viết: #{idx+1} "{ch['title']}" - {ch['summary']}
{pos}
Đoạn cuối chương trước (để nối mạch): {prev_tail or '(không có)'}

Quy tắc:
- Tổng độ dài chương KHOẢNG {target} từ (không ít hơn {int(target*0.9)} từ). {short_note}
- Chia thành các cảnh; mỗi cảnh 30-55 từ, là 2-4 câu hoàn chỉnh. Văn nói tự nhiên, có số liệu/ví dụ cụ thể, tránh lặp ý.
- Chỉ chữ thuần để đọc: không markdown, không emoji, không ghi chú sân khấu, không viết "Chương X".
- Mỗi cảnh có 2-3 cụm từ khóa TIẾNG ANH, cụ thể và quay phim được (vd "busy city street at night"), dùng để tìm video minh họa.
Trả về JSON: {{"scenes": [{{"narration": "...", "keywords": ["...", "..."]}}]}}"""


def write_chapter(s: Settings, project: dict, idx: int):
    _, target = plan(s)
    prev_tail = ""
    if idx > 0 and project["chapters"][idx - 1]["scenes"]:
        prev_tail = " ".join(x["narration"] for x in project["chapters"][idx - 1]["scenes"][-2:])
    best, note = [], ""
    for _ in range(3):
        out = ask_json(_chapter_prompt(s, project, idx, target, prev_tail, note), s.gemini_model)
        scenes = [{"narration": re.sub(r"[*_#`]", "", x["narration"]).strip(),
                   "keywords": [k for k in x.get("keywords", []) if k][:3] or [project["title"]]}
                  for x in out.get("scenes", []) if x.get("narration", "").strip()]
        words = sum(len(x["narration"].split()) for x in scenes)
        if words > sum(len(x["narration"].split()) for x in best):
            best = scenes
        if words >= target * 0.85:
            break
        note = f"LẦN TRƯỚC CHỈ {words} TỪ - QUÁ NGẮN, hãy viết dài và chi tiết hơn, đủ {target} từ."
    return best


def make_keywords(s: Settings, project: dict, progress=None):
    """Tạo từ khóa footage (tiếng Anh) cho các cảnh chưa có từ khóa (dùng cho kịch bản tự dán)."""
    todo = [(ci, si) for ci, c in enumerate(project["chapters"]) for si, x in enumerate(c["scenes"])
            if not x.get("keywords")]
    for b in range(0, len(todo), 15):
        batch = todo[b:b + 15]
        items = "\n".join(f"{k}. {project['chapters'][ci]['scenes'][si]['narration'][:300]}"
                          for k, (ci, si) in enumerate(batch))
        prompt = f"""Với mỗi đoạn lời đọc dưới đây (video "{project['title']}"), đề xuất 2-3 cụm từ khóa TIẾNG ANH
cụ thể, quay phim được (vd "busy city street at night"), dùng để tìm video stock minh họa.
Các đoạn:
{items}
Trả về JSON: {{"keywords": [["...","..."], ...]}} đúng {len(batch)} phần tử theo thứ tự."""
        try:
            kws = ask_json(prompt, s.gemini_model, temperature=0.4)["keywords"]
        except Exception:
            kws = []
        for k, (ci, si) in enumerate(batch):
            got = [x for x in (kws[k] if k < len(kws) else []) if isinstance(x, str) and x][:3]
            project["chapters"][ci]["scenes"][si]["keywords"] = got or [project["title"]]
        if progress:
            progress(min(1.0, (b + len(batch)) / max(1, len(todo))), f"Từ khóa {b + len(batch)}/{len(todo)}")
