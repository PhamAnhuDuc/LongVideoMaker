"""Viết kịch bản dài bằng Gemini: dàn ý -> từng chương -> các cảnh."""
import json
import math
import os
import re
import threading
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


DEFAULT_MODEL = "gemini-3.5-flash"
# model Google đã ngừng cấp cho tài khoản mới -> tự chuyển sang model mặc định
RETIRED = {"gemini-2.5-flash", "gemini-2.0-flash", "gemini-1.5-flash"}
# Gói miễn phí giới hạn số lần gọi MỖI NGÀY cho TỪNG model -> hết model này thì chuyển ngay model khác
FALLBACKS = ["gemini-3.5-flash", "gemini-3.6-flash", "gemini-3.7-flash", "gemini-3.8-flash",
             "gemini-flash-latest", "gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemini-flash-lite-latest"]
_SWITCHED = {}     # model đã quá tải -> model dự phòng đang dùng (nhớ trong lần chạy này)
_EXHAUSTED = set()  # model đã hết hạn mức hôm nay
_state_lock = threading.Lock()


_ALL_OUT = ("Tất cả model Gemini miễn phí đã hết hạn mức hôm nay (mỗi model ~20 lần gọi/ngày). "
            "Đợi sang ngày mới (khoảng 14-15h chiều giờ Việt Nam) hoặc bật thanh toán cho key Gemini.")


def _retry_delay(e) -> float:
    """Số giây Google bảo chờ (lỗi 429 theo phút), mặc định 10s."""
    m = re.search(r"retry in ([\d.]+)s", str(e)) or re.search(r"'retryDelay': '(\d+)s'", str(e))
    return min(float(m.group(1)) + 1, 65) if m else 10.0


def _thinking(model, level):
    """Giảm 'suy nghĩ' của model Gemini 3 để viết nhanh hơn nhiều (văn kể chuyện không cần suy luận sâu)."""
    from google.genai import types
    if level and model.startswith("gemini-3"):
        return types.ThinkingConfig(thinking_level=level)
    return None


def ask_json(prompt: str, model: str, temperature=0.8, tries=5):
    from google.genai import types
    from google.genai import errors
    client = _client()
    model = DEFAULT_MODEL if model.strip() in RETIRED else model.strip()
    asked = model
    with _state_lock:
        model = _SWITCHED.get(model, model)   # đã quá tải / hết hạn mức lần trước -> dùng luôn model dự phòng
        backups = [m for m in FALLBACKS if m != model and m not in _EXHAUSTED]
        if model in _EXHAUSTED:
            if not backups:
                raise RuntimeError(_ALL_OUT)
            model = backups.pop(0)
    levels = ["MINIMAL", "LOW", None]   # model nào không hỗ trợ MINIMAL thì lùi dần
    last, busy = None, 0
    tries += len(backups) * 2

    def switch(reason):
        nonlocal model, busy, levels
        with _state_lock:
            while backups and backups[0] in _EXHAUSTED:
                backups.pop(0)
            if not backups:
                return False
            model, busy, levels = backups.pop(0), 0, ["MINIMAL", "LOW", None]
            _SWITCHED[asked] = model
        print(f"[gemini] {reason}, chuyển sang {model}", flush=True)
        return True

    for i in range(tries):
        try:
            r = client.models.generate_content(
                model=model, contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json", temperature=temperature,
                    thinking_config=_thinking(model, levels[0])))
            return _parse_json(r.text)
        except errors.ClientError as e:   # lỗi 4xx: sai key/model -> báo ngay, không chờ
            last = e
            if e.code == 400 and "thinking level" in str(e).lower() and len(levels) > 1:
                levels.pop(0)
                continue
            if e.code == 404 and switch(f"model {model} không dùng được"):
                continue
            if e.code != 429:
                raise RuntimeError(f"Gemini lỗi: {e}") from e
            if "PerDay" in str(e):   # hết hạn mức NGÀY của model này -> chờ vô ích, đổi model ngay
                with _state_lock:
                    _EXHAUSTED.add(model)
                if switch(f"{model} hết hạn mức hôm nay"):
                    continue
                raise RuntimeError(_ALL_OUT) from e
            time.sleep(_retry_delay(e))   # vượt giới hạn/phút -> chờ đúng thời gian Google yêu cầu
        except errors.ServerError as e:  # 503 quá tải / 504 hết giờ
            last, busy = e, busy + 1
            if busy >= 2 and switch(f"{model} quá tải"):
                continue
            time.sleep(min(2 ** busy * 2, 20))
        except Exception as e:  # JSON lỗi... -> thử lại
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
{f'Đoạn cuối chương trước (để nối mạch): {prev_tail}' if prev_tail else
 (f'Chương trước: "{chs[idx-1]["title"]}" - mở đầu chương này bằng câu chuyển ý tự nhiên từ nội dung đó.' if idx > 0 else '')}
{f'Chương sau: "{chs[idx+1]["title"]}" - câu cuối chương này gợi mở dẫn sang nội dung đó.' if idx < len(chs) - 1 else ''}

Quy tắc:
- Tổng độ dài chương KHOẢNG {target} từ (không ít hơn {int(target*0.9)} từ). {short_note}
- Chia thành các cảnh; mỗi cảnh 30-55 từ, là 2-4 câu hoàn chỉnh. Văn nói tự nhiên, có số liệu/ví dụ cụ thể, tránh lặp ý.
- Chỉ chữ thuần để đọc: không markdown, không emoji, không ghi chú sân khấu, không viết "Chương X".
- Mỗi cảnh tách thành từng CÂU (shots). Mỗi câu kèm từ khóa TIẾNG ANH để tìm video stock minh họa ĐÚNG nội dung câu đó:
  kw = [cụ thể, chung hơn], mỗi từ khóa 1-3 từ, là thứ quay phim được (vật, người, nơi chốn, hành động),
  vd câu nói về kim tự tháp -> ["egyptian pyramid", "desert"]; câu về lãi suất -> ["bank interest", "money"].
  Ý trừu tượng thì chọn hình ẩn dụ cụ thể (vd "suy nghĩ" -> ["man thinking", "person window"]).
- "keywords" của cảnh: 1-2 từ khóa chung cho cả cảnh (dự phòng).
Trả về JSON: {{"scenes": [{{"shots": [{{"text": "một câu", "kw": ["...", "..."]}}], "keywords": ["..."]}}]}}"""


def _clean_kw(lst, n=3):
    return [k.strip() for k in (lst or []) if isinstance(k, str) and k.strip()][:n]


def _scene(x: dict, title: str):
    """Chuẩn hóa 1 cảnh Gemini trả về: narration = ghép các câu trong shots."""
    shots = [{"text": re.sub(r"[*_#`]", "", sh.get("text", "")).strip(), "kw": _clean_kw(sh.get("kw"))}
             for sh in x.get("shots") or [] if isinstance(sh, dict)]
    shots = [sh for sh in shots if sh["text"]]
    narration = " ".join(sh["text"] for sh in shots) or re.sub(r"[*_#`]", "", x.get("narration", "")).strip()
    kws = _clean_kw(x.get("keywords"), 2) or next((sh["kw"][-1:] for sh in shots if sh["kw"]), [])
    sc = {"narration": narration, "keywords": kws or [title]}
    if shots:
        sc["shots"] = shots
    return sc


def write_chapter(s: Settings, project: dict, idx: int, use_prev=True):
    """use_prev=False: không chờ chương trước (để viết các chương song song) - nối mạch qua dàn ý."""
    _, target = plan(s)
    prev_tail = ""
    if use_prev and idx > 0 and project["chapters"][idx - 1]["scenes"]:
        prev_tail = " ".join(x["narration"] for x in project["chapters"][idx - 1]["scenes"][-2:])
    best, note = [], ""
    for _ in range(3):
        out = ask_json(_chapter_prompt(s, project, idx, target, prev_tail, note), s.gemini_model)
        scenes = [_scene(x, project["title"]) for x in out.get("scenes", [])]
        scenes = [x for x in scenes if x["narration"]]
        words = sum(len(x["narration"].split()) for x in scenes)
        if words > sum(len(x["narration"].split()) for x in best):
            best = scenes
        if words >= target * 0.85:
            break
        note = f"LẦN TRƯỚC CHỈ {words} TỪ - QUÁ NGẮN, hãy viết dài và chi tiết hơn, đủ {target} từ."
    return best


def make_keywords(s: Settings, project: dict, progress=None):
    """Tạo từ khóa footage theo TỪNG CÂU cho các cảnh chưa có (dùng cho kịch bản tự dán / kịch bản cũ)."""
    from .shots import shots_for
    todo = [(ci, si) for ci, c in enumerate(project["chapters"]) for si, x in enumerate(c["scenes"])
            if not x.get("shots") or not all(sh.get("kw") for sh in x["shots"])]
    batch_size = 10
    for b in range(0, len(todo), batch_size):
        batch = todo[b:b + batch_size]
        items, shot_lists = [], []
        for k, (ci, si) in enumerate(batch):
            sh = shots_for(project["chapters"][ci]["scenes"][si])
            shot_lists.append(sh)
            items.append(f"Cảnh {k}:\n" + "\n".join(f"  {k}.{j}: {x['text'][:250]}" for j, x in enumerate(sh)))
        prompt = f"""Video "{project['title']}". Với MỖI CÂU lời đọc dưới đây, đề xuất từ khóa TIẾNG ANH để tìm video stock
minh họa đúng nội dung câu đó: [cụ thể, chung hơn], mỗi từ khóa 1-3 từ, quay phim được (vật, người, nơi, hành động).
Ý trừu tượng thì chọn hình ẩn dụ cụ thể. Vd câu về kim tự tháp -> ["egyptian pyramid", "desert"].
{chr(10).join(items)}
Trả về JSON: {{"shots": {{"0.0": ["...", "..."], "0.1": [...], ...}}, "scenes": {{"0": ["từ khóa chung cả cảnh"], ...}}}}
Phải có đủ mọi khóa câu ở trên."""
        try:
            out = ask_json(prompt, s.gemini_model, temperature=0.4)
        except Exception:
            out = {}
        got_shots, got_scenes = out.get("shots") or {}, out.get("scenes") or {}
        for k, (ci, si) in enumerate(batch):
            sc = project["chapters"][ci]["scenes"][si]
            general = _clean_kw(got_scenes.get(str(k)), 2) or _clean_kw(sc.get("keywords"), 2)
            sc["shots"] = [{"text": x["text"], "kw": x["kw"] or _clean_kw(got_shots.get(f"{k}.{j}"))}
                           for j, x in enumerate(shot_lists[k])]
            sc["keywords"] = general or next((x["kw"][-1:] for x in sc["shots"] if x["kw"]), []) or [project["title"]]
        if progress:
            progress(min(1.0, (b + len(batch)) / max(1, len(todo))), f"Từ khóa {b + len(batch)}/{len(todo)}")
