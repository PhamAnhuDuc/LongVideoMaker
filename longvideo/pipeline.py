"""Điều phối: tạo kịch bản và render video theo project (có thể chạy tiếp khi bị ngắt)."""
import hashlib
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from . import llm, music
from .compose import (concat_scenes, pinned_segments, plan_segments, render_scene, scene_cues,
                      tail_head, write_ass)
from .config import PROJECTS, Settings
from .footage import FootagePicker, local_clips
from .shots import shots_for, timeline
from .tts import clean_text, gemini_chapter, is_gemini, proportional, synth, whisper_word_times
from .util import fmt_ts, read_json, write_json

# số cảnh dựng cùng lúc và số luồng CPU cho mỗi ffmpeg (tổng ~ số luồng CPU)
_CPU = os.cpu_count() or 4
SCENE_WORKERS = max(1, min(4, _CPU // 3))
SCENE_THREADS = max(1, _CPU // SCENE_WORKERS)


class Project:
    def __init__(self, name):
        self.dir = PROJECTS / name
        self.name = name
        self.media = self.dir / "media"
        self.work = self.dir / "work"
        for d in (self.media, self.work):
            d.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def create(topic):
        slug = re.sub(r"[^a-z0-9]+", "-", topic.lower().encode("ascii", "ignore").decode())[:30].strip("-")
        name = f"{time.strftime('%m%d-%H%M')}-{slug or 'video'}"
        return Project(name)

    @property
    def data_file(self):
        return self.dir / "project.json"

    def load(self):
        d = read_json(self.data_file, {})
        return Settings.from_dict(d.get("settings", {})), d.get("script")

    def save(self, settings: Settings, script):
        write_json(self.data_file, {"settings": settings.to_dict(), "script": script})

    @property
    def bgm(self):
        for ext in ("mp3", "wav", "m4a", "ogg"):
            p = self.dir / f"bgm.{ext}"
            if p.exists():
                return p
        return None

    @property
    def output(self):
        return self.dir / "final.mp4"


def list_projects():
    ps = [p.name for p in PROJECTS.iterdir() if (p / "project.json").exists()]
    return sorted(ps, reverse=True)


def generate_script(proj: Project, progress):
    s, _ = proj.load()
    progress(0.02, "Đang lập dàn ý…")
    script = llm.make_outline(s)
    proj.save(s, script)
    n = len(script["chapters"])
    # viết 3 chương cùng lúc (nhanh ~3 lần); mạch truyện nối qua dàn ý + tên chương trước/sau
    progress(0.05, f"Đang viết {n} chương (3 chương cùng lúc)…")
    outline = {"title": script["title"], "chapters": [{**c, "scenes": []} for c in script["chapters"]]}
    with ThreadPoolExecutor(3) as ex:
        futs = {ex.submit(llm.write_chapter, s, outline, i, False): i for i in range(n)}
        for k, f in enumerate(as_completed(futs)):
            i = futs[f]
            script["chapters"][i]["scenes"] = f.result()
            proj.save(s, script)
            progress(0.05 + 0.93 * (k + 1) / n, f"Xong {k+1}/{n} chương (vừa xong: {script['chapters'][i]['title']})")
    words = sum(len(x["narration"].split()) for c in script["chapters"] for x in c["scenes"])
    progress(1.0, f"Xong kịch bản: {words} từ, ước tính ~{words / 170:.0f} phút.")


def generate_keywords(proj: Project, progress):
    s, script = proj.load()
    err = llm.make_keywords(s, script, lambda f, m: progress(f, m))
    proj.save(s, script)
    if err:
        raise RuntimeError(err + " — Các cảnh thiếu từ khóa tạm dùng tiêu đề/chủ đề. Hãy bấm 🎯 Tạo từ khóa "
                                 "theo câu để thử lại (hoặc đổi model Gemini), hoặc gõ từ khóa tay.")
    progress(1.0, "Đã tạo từ khóa footage.")


def run_full(proj: Project, progress):
    """Tạo kịch bản (nếu chưa có) rồi render — dùng cho tạo hàng loạt."""
    _, script = proj.load()
    if not script or not any(c["scenes"] for c in script["chapters"]):
        generate_script(proj, lambda f, m: progress(f * 0.3, m))
    render(proj, lambda f, m: progress(0.3 + f * 0.7, m))


def _scene_hash(s: Settings, sc, w, h, pool=()):
    style = (s.subtitles, s.sub_font, s.sub_bold, s.sub_color, s.sub_outline_color, s.sub_size, s.sub_outline,
             s.sub_position, s.sub_margin, s.transition, s.transition_dur, s.max_clip_sec, s.clip_mode,
             s.voice_volume, tuple(s.footage_sources), s.sub_timing, s.media_mode, pool)
    shots = json.dumps([x["kw"] for x in shots_for(sc)], ensure_ascii=False)
    key = f"v4|{sc['narration']}|{sc.get('keywords')}|{shots}|{sc.get('media')}|{s.voice}|{s.rate}|{w}x{h}|{style}"
    return hashlib.md5(key.encode()).hexdigest()


def render(proj: Project, progress):
    s, script = proj.load()
    if not script:
        raise RuntimeError("Chưa có kịch bản.")
    w, h = s.size()
    portrait = h > w
    scenes = []
    for ci, ch in enumerate(script["chapters"]):
        for si, sc in enumerate(ch["scenes"]):
            scenes.append((ci, si, sc, proj.work / f"c{ci+1:02d}_s{si+1:03d}"))
    total = len(scenes)
    pinned = sorted({f for _, _, sc, _ in scenes for f in (sc.get("media") or [])})
    if s.media_mode == "mine":   # toàn bộ video chỉ dùng ảnh/video người dùng tải lên
        if not local_clips(proj.media):
            raise RuntimeError("Đang chọn 'Chỉ dùng ảnh/video của tôi' nhưng dự án chưa có ảnh/video nào. "
                               "Hãy tải file lên ở mục 📁 Ảnh & video của bạn, hoặc chuyển về chế độ Tự động.")
        picker = FootagePicker(proj.media, ["local"], portrait, h if portrait else w, s.clip_mode)
        pool_key = ()
    else:
        # Tự động: file đã chèn cho cảnh nào chỉ dùng ở cảnh đó; các cảnh còn lại tự tìm footage
        picker = FootagePicker(proj.media, s.footage_sources, portrait, h if portrait else w, s.clip_mode,
                               exclude=pinned)
        pool_key = tuple(pinned)
        unpinned = [sc for _, _, sc, _ in scenes if not sc.get("media")]
        if unpinned and not picker.sources:
            raise RuntimeError("Có cảnh chưa chèn ảnh/video nhưng chưa bật nguồn tự tìm nào (Pixabay/Pexels). "
                               "Hãy chọn nguồn footage ở tab 2️⃣ mục Hình ảnh.")

    # Pha 1: giọng đọc (song song) + mốc thời gian từ - bỏ qua cảnh đã làm
    gem = is_gemini(s.voice)
    timing_key = f"{s.sub_timing}{s.whisper_model}" if s.subtitles else "sentence"

    def tts_hash(ci, sc):
        if gem:  # Gemini đọc cả chương một lần -> audio của cảnh phụ thuộc cả chương
            body = "|".join(x["narration"] for x in script["chapters"][ci]["scenes"])
            return hashlib.md5(f"{body}{s.voice}{s.rate}{s.voice_style}{timing_key}".encode()).hexdigest()
        return hashlib.md5(f"{sc['narration']}{s.voice}{s.rate}{s.sub_timing}{s.whisper_model}".encode()).hexdigest()

    def is_done(ci, sc, d):
        meta = read_json(d / "meta.json", {})
        return meta.get("tts_hash") == tts_hash(ci, sc) and (d / "voice.mp3").exists()

    def tts_job(item):
        ci, si, sc, d = item
        d.mkdir(exist_ok=True)
        meta = read_json(d / "meta.json", {})
        if meta.get("tts_hash") == tts_hash(ci, sc) and (d / "voice.mp3").exists():
            return item, meta
        dur, cues, words = synth(sc["narration"], s.voice, s.rate, d / "voice.mp3",
                                 s.sub_timing, s.language, s.whisper_model)
        meta = {"tts_hash": tts_hash(ci, sc), "dur": dur, "cues": cues, "words": words}
        write_json(d / "meta.json", meta)
        return item, meta

    def gemini_chapter_job(ci):
        items = [it for it in scenes if it[0] == ci]
        if all(is_done(c, sc, d) for c, _, sc, d in items):
            return
        for *_, d in items:
            d.mkdir(exist_ok=True)
        durs = gemini_chapter([sc["narration"] for _, _, sc, _ in items], s.voice, s.rate,
                              [d / "voice.mp3" for *_, d in items], s.voice_style)
        for (c, _, sc, d), dur in zip(items, durs):
            words = None
            if s.subtitles and s.sub_timing != "sentence":
                words = whisper_word_times(sc["narration"], d / "voice.mp3", dur, s.language, s.whisper_model)
            meta = {"tts_hash": tts_hash(c, sc), "dur": dur,
                    "cues": proportional(clean_text(sc["narration"]), dur), "words": words}
            write_json(d / "meta.json", meta)

    if s.sub_timing != "sentence" and s.subtitles:
        progress(0.0, "Nạp model Whisper (lần đầu sẽ tải về, hơi lâu)…")
        try:
            from . import align
            align.preload(s.whisper_model)
        except Exception as e:   # thiếu thư viện / không tải được model -> vẫn render, phụ đề theo câu
            print("Không nạp được Whisper, dùng phụ đề theo câu:", e, flush=True)
    if gem:  # mỗi chương một lần gọi Gemini; xong thì tts_job bên dưới chỉ đọc lại meta đã lưu
        n_ch = len(script["chapters"])
        progress(0.0, f"Giọng Gemini: đọc {n_ch} chương…")
        with ThreadPoolExecutor(2) as ex:
            futs = [ex.submit(gemini_chapter_job, ci) for ci in range(n_ch)]
            for k, f in enumerate(as_completed(futs)):
                f.result()
                progress(0.25 * (k + 1) / n_ch, f"Giọng Gemini: xong chương {k+1}/{n_ch}")
    metas = {}
    # edge-tts chủ yếu chờ mạng -> chạy nhiều luồng; Whisper ăn CPU/RAM nên giữ ít
    tts_workers = 2 if (s.sub_timing != "sentence" and s.subtitles) else 4
    with ThreadPoolExecutor(tts_workers) as ex:
        futs = [ex.submit(tts_job, it) for it in scenes]
        for k, f in enumerate(as_completed(futs)):
            item, meta = f.result()
            metas[item[3].name] = meta
            progress(0.25 * (k + 1) / total, f"Giọng đọc {k+1}/{total}")

    # Pha 2: dựng các cảnh song song (cảnh này tải footage trong lúc cảnh khác đang mã hóa)
    files, t = [], 0.0
    ch_start = {}
    for ci, si, sc, d in scenes:
        ch_start.setdefault(ci, t)
        files.append(d / "scene.mp4")
        t += metas[d.name]["dur"] + 0.35

    t_eff = s.transition_dur if s.transition != "none" else 0.0

    def plan_job(item):
        """Chọn clip cho cảnh (tải footage). Lưu lại để cảnh sau biết clip cuối mà nối tiếp."""
        ci, si, sc, d = item
        sdur = metas[d.name]["dur"] + 0.35
        base = _scene_hash(s, sc, w, h, pool_key)
        meta = read_json(d / "meta.json", {})
        if meta.get("plan_hash") == base and meta.get("segs"):
            return d.name, meta["segs"]
        segs = None
        if sc.get("media"):   # ảnh/video người dùng chọn riêng cho cảnh này
            segs = pinned_segments(sc["media"], proj.media, sdur, t_eff)
        if not segs:
            segs = plan_segments(timeline(sc, metas[d.name], sdur), picker, s.max_clip_sec, t_eff)
        meta.update(plan_hash=base, segs=segs)
        write_json(d / "meta.json", meta)
        return d.name, segs

    plans = {}
    with ThreadPoolExecutor(6) as ex:   # chủ yếu chờ mạng tải footage
        futs = [ex.submit(plan_job, it) for it in scenes]
        for k, f in enumerate(as_completed(futs)):
            name, segs = f.result()
            plans[name] = segs
            progress(0.25 + 0.15 * (k + 1) / total, f"Tìm footage {k+1}/{total}")

    def rendered_segs(k):
        """Danh sách đoạn thực sự dựng cho cảnh k: thêm đoạn nối từ cảnh trước nếu có chuyển cảnh."""
        segs = plans[scenes[k][3].name]
        if k == 0 or not t_eff:
            return segs
        return [tail_head(rendered_segs_plain(k - 1), t_eff)] + segs

    def rendered_segs_plain(k):
        # cảnh trước cũng có đoạn nối ở đầu -> chỉ số (chẵn/lẻ zoom ảnh) của đoạn cuối lệch 1
        segs = plans[scenes[k][3].name]
        return ([None] if k > 0 and t_eff else []) + segs

    def scene_job(k):
        ci, si, sc, d = scenes[k]
        sdur = metas[d.name]["dur"] + 0.35
        out = d / "scene.mp4"
        segs = rendered_segs(k)
        f_in = 0.8 if k == 0 else 0.0
        f_out = 1.0 if k == total - 1 else 0.0
        hsh = hashlib.md5(f"{_scene_hash(s, sc, w, h, pool_key)}|{json.dumps(segs)}|{f_in}|{f_out}".encode()).hexdigest()
        meta = read_json(d / "meta.json", {})
        if out.exists() and meta.get("render_hash") == hsh:
            return
        ass = None
        if s.subtitles:
            ass = d / "sub.ass"
            write_ass(scene_cues(metas[d.name], portrait), ass, w, h, s)
        render_scene(segs, d / "voice.mp3", ass, out, w, h, sdur, s, threads=SCENE_THREADS,
                     fade_in=f_in, fade_out=f_out)
        meta["render_hash"] = hsh
        write_json(d / "meta.json", meta)

    with ThreadPoolExecutor(SCENE_WORKERS) as ex:
        futs = [ex.submit(scene_job, k) for k in range(total)]
        for k, f in enumerate(as_completed(futs)):
            f.result()
            progress(0.40 + 0.55 * (k + 1) / total, f"Dựng cảnh {k+1}/{total}")

    progress(0.96, "Ghép video cuối…")
    bgm = music.pick(s.bgm_mode, s.bgm_track, proj.bgm)
    concat_scenes(files, proj.output, bgm, s.bgm_volume, proj.work / "list.txt", t)
    lines = [f"{fmt_ts(ch_start[i])} {script['chapters'][i]['title']}" for i in sorted(ch_start)]
    (proj.dir / "chapters.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    progress(1.0, f"Hoàn tất! Độ dài {fmt_ts(t)}.")
