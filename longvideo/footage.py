"""Lấy footage: Pexels, Pixabay (có cache) hoặc video người dùng nhập."""
import os
import random
import threading
import time
from pathlib import Path

import requests

from .config import CACHE
from .util import duration

VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
_lock = threading.Lock()
_search_cache: dict = {}


def is_image(p) -> bool:
    return Path(p).suffix.lower() in IMAGE_EXT


def local_clips(media_dir: Path):
    """Ảnh + video người dùng đã tải vào dự án."""
    if not media_dir.exists():
        return []
    return sorted(p for p in media_dir.iterdir() if p.suffix.lower() in VIDEO_EXT | IMAGE_EXT)


def _get(url, **kw):
    for attempt in range(3):
        try:
            r = requests.get(url, timeout=30, **kw)
            if r.status_code == 429:
                time.sleep(3 * (attempt + 1))
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException:
            time.sleep(1)
    return {}


_dl_locks: dict = {}


def _download(link, dest: Path) -> Path | None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    # các cảnh dựng song song có thể cùng cần 1 clip -> mỗi file chỉ một luồng tải, luồng kia chờ rồi dùng chung
    with _lock:
        lk = _dl_locks.setdefault(str(dest), threading.Lock())
    with lk:
        if dest.exists() and dest.stat().st_size > 10_000:
            return dest
        tmp = dest.with_suffix(".part")
        try:
            with requests.get(link, stream=True, timeout=60) as r:
                r.raise_for_status()
                with open(tmp, "wb") as fh:
                    for chunk in r.iter_content(1 << 20):
                        fh.write(chunk)
            tmp.replace(dest)
            return dest
        except Exception:
            tmp.unlink(missing_ok=True)
            return None


# ---------- Pexels ----------
def pexels_search(query, portrait=False):
    key = os.environ.get("PEXELS_API_KEY")
    if not key:
        return []
    data = _get("https://api.pexels.com/videos/search", headers={"Authorization": key},
                params={"query": query, "per_page": 20,
                        "orientation": "portrait" if portrait else "landscape"})
    out = []
    for v in data.get("videos", []):
        files = [f for f in v.get("video_files", []) if f.get("file_type") == "video/mp4"
                 and f.get("width") and f.get("height")]
        if files and v.get("duration", 0) >= 3:
            out.append({"id": f"pexels{v['id']}", "files": [(f["width"], f["height"], f["link"]) for f in files]})
    return out


# ---------- Pixabay ----------
def pixabay_search(query, portrait=False):
    key = os.environ.get("PIXABAY_API_KEY")
    if not key:
        return []
    data = _get("https://pixabay.com/api/videos/", params={"key": key, "q": query[:100], "per_page": 20,
                                                            "safesearch": "true"})
    out = []
    for h in data.get("hits", []):
        files = [(f["width"], f["height"], f["url"]) for f in h.get("videos", {}).values()
                 if f.get("url") and f.get("width")]
        if files and h.get("duration", 0) >= 3:
            out.append({"id": f"pixabay{h['id']}", "files": files})
    return out


def _best_file(files, target_w, portrait):
    key = (lambda f: f[1]) if portrait else (lambda f: f[0])
    ok = [f for f in files if key(f) >= target_w * 0.9] or files
    return min(ok, key=lambda f: abs(key(f) - target_w))


SEARCH = {"pexels": pexels_search, "pixabay": pixabay_search}


class FootagePicker:
    """Chọn clip cho từng đoạn. mode = random | sequential; tránh lặp trong cùng một video."""

    def __init__(self, media_dir: Path, sources, portrait, target_w, mode="random"):
        self.portrait, self.target_w, self.mode = portrait, target_w, mode
        self.locals = local_clips(media_dir) if "local" in sources else []
        self.sources = [s for s in sources if s != "local" or self.locals]
        self.used: set[str] = set()
        self.durs: dict[str, float] = {}
        self.rr = 0           # vòng quay nguồn (sequential)
        self.local_idx = 0    # vị trí clip local (sequential)

    def _dur(self, p):
        k = str(p)
        if is_image(p):   # ảnh tĩnh: hiển thị bao lâu cũng được
            return 1e6
        if k not in self.durs:
            try:
                self.durs[k] = duration(p)
            except Exception:
                self.durs[k] = 0
        return self.durs[k]

    def _local(self):
        if not self.locals:
            return None
        if self.mode == "sequential":
            p = self.locals[self.local_idx % len(self.locals)]
            self.local_idx += 1
            return p
        pool = [p for p in self.locals if str(p) not in self.used] or self.locals
        return random.choice(pool)

    def _search(self, source, kw):
        k = (source, kw.lower().strip(), self.portrait)
        with _lock:
            if k in _search_cache:
                return _search_cache[k]
        vids = SEARCH[source](kw, self.portrait)
        if vids:  # không cache kết quả rỗng (có thể do lỗi mạng/429)
            with _lock:
                _search_cache[k] = vids
        return vids

    def _remote(self, source, kw):
        vids = list(self._search(source, kw))
        if self.mode == "random":
            random.shuffle(vids)
        with _lock:
            fresh = [v for v in vids if v["id"] not in self.used]
        # clip chưa dùng trước; nếu từ khóa đã dùng hết clip thì trả None để thử từ khóa chung hơn
        for v in fresh[:4]:
            w, h, link = _best_file(v["files"], self.target_w, self.portrait)
            p = _download(link, CACHE / source / f"{v['id']}_{w}x{h}.mp4")
            if p:
                return p, v["id"]
        return None, None

    def _take(self, p, vid):
        with _lock:
            self.used.add(str(p))
            if vid:
                self.used.add(vid)
        return p, self._dur(p)

    def pick(self, keywords):
        """keywords xếp từ cụ thể -> chung. Thử từ khóa cụ thể trên mọi nguồn trước rồi mới lùi dần.
        Trả về (path, độ dài clip) hoặc (None, 0) nếu không có footage."""
        if not self.sources:
            return None, 0
        with _lock:
            if self.mode == "sequential":
                order = [self.sources[(self.rr + i) % len(self.sources)] for i in range(len(self.sources))]
                self.rr += 1
            else:
                order = random.sample(self.sources, len(self.sources))
            local_pick = self._local() if order[0] == "local" else None
        # _take tự khóa _lock -> phải gọi NGOÀI khối with (Lock không cho khóa lồng -> treo)
        if order[0] == "local":  # lượt này dùng ảnh/video tự nhập
            return self._take(local_pick, None)
        remote = [x for x in order if x != "local"]
        kws = [k for k in keywords if k and k.strip()]
        for kw in kws:
            for src in remote:
                p, vid = self._remote(src, kw)
                if p:
                    return self._take(p, vid)
        if "local" in order:
            with _lock:
                p = self._local()
            return self._take(p, None)
        # hết clip mới: chấp nhận dùng lại clip đã dùng của từ khóa cụ thể nhất
        for kw in kws:
            for src in remote:
                vids = self._search(src, kw)
                for v in vids[:4]:
                    w, h, link = _best_file(v["files"], self.target_w, self.portrait)
                    p = _download(link, CACHE / src / f"{v['id']}_{w}x{h}.mp4")
                    if p:
                        return self._take(p, v["id"])
        return None, 0
