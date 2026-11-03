"""Cấu hình chung, đường dẫn, đọc .env."""
import os
from dataclasses import dataclass, asdict, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROJECTS = ROOT / "projects"
CACHE = ROOT / "cache"
ENV_FILE = ROOT / ".env"
PROJECTS.mkdir(exist_ok=True)
CACHE.mkdir(exist_ok=True)


def load_env():
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def save_env(values: dict):
    cur = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                cur[k.strip()] = v.strip()
    cur.update({k: v for k, v in values.items() if v})
    ENV_FILE.write_text("\n".join(f"{k}={v}" for k, v in cur.items()) + "\n", encoding="utf-8")
    ENV_FILE.chmod(0o600)


load_env()

VOICES = {
    "vi": ["vi-VN-HoaiMyNeural", "vi-VN-NamMinhNeural"],
    "en": ["en-US-AriaNeural", "en-US-GuyNeural", "en-US-JennyNeural",
           "en-GB-SoniaNeural", "en-GB-RyanNeural", "en-AU-NatashaNeural"],
}
# Giọng Gemini TTS (đọc được mọi ngôn ngữ, chỉnh phong cách bằng lời). Mã giọng lưu dạng "gemini:<tên>".
GEMINI_TTS_MODEL = "gemini-3.8-flash-tts"
GEMINI_TTS_FALLBACK = "gemini-3.8-flash-lite-tts"
GEMINI_VOICES = {
    "Charon": "nam, trầm, kể chuyện", "Orus": "nam, chắc, rõ ràng", "Fenrir": "nam, hào hứng",
    "Iapetus": "nam, trong", "Algieba": "nam, mượt", "Alnilam": "nam, vững", "Enceladus": "nam, thì thầm",
    "Rasalgethi": "nam, truyền cảm", "Sadaltager": "nam, hiểu biết", "Gacrux": "nam, trưởng thành",
    "Kore": "nữ, chắc, rõ ràng", "Aoede": "nữ, nhẹ nhàng", "Leda": "nữ, trẻ trung", "Zephyr": "nữ, tươi sáng",
    "Despina": "nữ, mượt", "Erinome": "nữ, trong", "Sulafat": "nữ, ấm", "Achernar": "nữ, mềm",
    "Vindemiatrix": "nữ, dịu dàng", "Pulcherrima": "nữ, mạnh mẽ", "Puck": "nam, vui tươi",
}
for _lang in VOICES:
    VOICES[_lang] = VOICES[_lang] + [f"gemini:{n}" for n in GEMINI_VOICES]


def voice_label(v: str) -> str:
    if v.startswith("gemini:"):
        n = v[7:]
        return f"✨ Gemini · {n} ({GEMINI_VOICES.get(n, '')})"
    return f"Microsoft · {v}"


# số từ/phút ước lượng khi đọc (chỉ để lập kế hoạch; thời lượng thật đo sau khi TTS)
WORDS_PER_MIN = {"vi": 190, "en": 150}
LANG_NAME = {"vi": "Tiếng Việt", "en": "English"}


SOURCES = ["pexels", "pixabay", "local"]
TRANSITIONS = ["none", "fade", "fadeblack", "smoothleft", "slideleft", "circleopen", "wipeleft", "random"]
TRANSITION_NAMES = {"none": "Không (cắt thẳng)", "fade": "Hòa mờ (mượt nhất)", "dissolve": "Hòa mờ (mượt nhất)",
                    "fadeblack": "Mờ qua màn đen", "smoothleft": "Trượt mềm sang trái", "slideleft": "Đẩy sang trái",
                    "circleopen": "Mở vòng tròn", "wipeleft": "Gạt sang trái", "random": "Ngẫu nhiên"}
MUSIC_DIR = ROOT / "assets" / "music"
MUSIC_DIR.mkdir(parents=True, exist_ok=True)


@dataclass
class Settings:
    topic: str = ""
    language: str = "vi"            # vi | en
    minutes: int = 15
    tone: str = "hấp dẫn, kể chuyện, dễ hiểu"
    audience: str = "khán giả phổ thông"
    notes: str = ""
    gemini_model: str = "gemini-3.5-flash"
    # giọng đọc
    voice: str = "vi-VN-HoaiMyNeural"
    rate: int = 0                   # % tốc độ đọc, -30..+30
    voice_style: str = ""           # chỉ cho giọng Gemini: mô tả cách đọc
    voice_volume: int = 100         # % âm lượng giọng
    # hình
    aspect: str = "16:9"            # 16:9 | 9:16
    height: int = 720               # chiều cao video ngang (720/1080)
    footage_sources: list = field(default_factory=lambda: ["pixabay"])
    media_mode: str = "auto"        # auto: cảnh đã chèn dùng file của tôi, còn lại tự tìm | mine: chỉ dùng file của tôi
    clip_mode: str = "random"       # random | sequential
    max_clip_sec: float = 7.0       # độ dài tối đa mỗi clip
    transition: str = "fade"
    transition_dur: float = 0.5
    # phụ đề
    subtitles: bool = True
    sub_timing: str = "sentence"    # sentence | word | whisper
    whisper_model: str = "base"
    sub_font: str = "DejaVu Sans"
    sub_bold: bool = True
    sub_color: str = "#FFFFFF"
    sub_outline_color: str = "#000000"
    sub_size: float = 5.8           # % chiều cao khung hình
    sub_outline: float = 3.0
    sub_position: str = "bottom"    # bottom | middle | top
    sub_margin: float = 7.0         # % chiều cao
    # nhạc nền
    bgm_mode: str = "none"          # none | random | library | upload
    bgm_track: str = ""
    bgm_volume: float = 0.10

    def size(self):
        h = self.height
        w = round(h * 16 / 9 / 2) * 2
        return (w, h) if self.aspect == "16:9" else (h, w)

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, d):
        d = dict(d)
        if "footage_source" in d:  # tương thích dự án cũ
            old = d.pop("footage_source")
            d.setdefault("footage_sources", ["pexels", "local"] if old == "both" else [old])
        names = cls.__dataclass_fields__.keys()
        return cls(**{k: v for k, v in d.items() if k in names})
