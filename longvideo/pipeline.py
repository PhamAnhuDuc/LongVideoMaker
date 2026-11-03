"""Điều phối: tạo kịch bản và render video theo project (có thể chạy tiếp khi bị ngắt)."""
import hashlib
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from . import llm, music
from .compose import concat_scenes, pinned_segments, plan_segments, render_scene, scene_cues, write_ass
from .config import PROJECTS, Settings
from .footage import FootagePicker
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
    for i in range(n):
        progress(0.05 + 0.93 * i / n, f"Viết chương {i+1}/{n}: {script['chapters'][i]['title']}")
        script["chapters"][i]["scenes"] = llm.write_chapter(s, script, i)
        proj.save(s, script)
    words = sum(len(x["narration"].split()) for c in script["chapters"] for x in c["scenes"])
    progress(1.0, f"Xong kịch bản: {words} từ, ước tính ~{words / 170:.0f} phút.")


def generate_keywords(proj: Project, progress):
    s, script = proj.load()
    llm.make_keywords(s, script, lambda f, m: progress(f, m))
    proj.save(s, script)
    progress(1.0, "Đã tạo từ khóa footage.")


def run_full(proj: Project, progress):
    """Tạo kịch bản (nếu chưa có) rồi render — dùng cho tạo hàng loạt."""
    _, script = proj.load()
    if not script or not any(c["scenes"] for c in script["chapters"]):
        generate_script(proj, lambda f, m: progress(f * 0.3, m))
    render(proj, lambda f, m: progress(0.3 + f * 0.7, m))


def _scene_hash(s: Settings, sc, w, h):
    style = (s.subtitles, s.sub_font, s.sub_bold, s.sub_color, s.sub_outline_color, s.sub_size, s.sub_outline,
             s.sub_position, s.sub_margin, s.transition, s.transition_dur, s.max_clip_sec, s.clip_mode,
             s.voice_volume, tuple(s.footage_sources), s.sub_timing)
    shots = json.dumps([x["kw"] for x in shots_for(sc)], ensure_ascii=False)
    key = f"{sc['narration']}|{sc.get('keywords')}|{shots}|{sc.get('media')}|{s.voice}|{s.rate}|{w}x{h}|{style}"
    return hashlib.md5(key.encode()).hexdigest()


def render(proj: Project, progress):
    s, script = proj.load()
    if not script:
        raise RuntimeError("Chưa có kịch bản.")
    w, h = s.size()
    portrait = h > w
    picker = FootagePicker(proj.media, s.footage_sources, portrait, h if portrait else w, s.clip_mode)
    scenes = []
    for ci, ch in enumerate(script["chapters"]):
        for si, sc in enumerate(ch["scenes"]):
            scenes.append((ci, si, sc, proj.work / f"c{ci+1:02d}_s{si+1:03d}"))
    total = len(scenes)

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

    def scene_job(item):
        ci, si, sc, d = item
        sdur = metas[d.name]["dur"] + 0.35
        out = d / "scene.mp4"
        hsh = _scene_hash(s, sc, w, h)
        meta = read_json(d / "meta.json", {})
        if out.exists() and meta.get("render_hash") == hsh:
            return
        t_eff = s.transition_dur if s.transition != "none" else 0.0
        segs = None
        if sc.get("media"):   # ảnh/video người dùng chọn riêng cho cảnh này
            segs = pinned_segments(sc["media"], proj.media, sdur, t_eff)
        if not segs:
            segs = plan_segments(timeline(sc, metas[d.name], sdur), picker, s.max_clip_sec, t_eff)
        ass = None
        if s.subtitles:
            ass = d / "sub.ass"
            write_ass(scene_cues(metas[d.name], portrait), ass, w, h, s)
        render_scene(segs, d / "voice.mp3", ass, out, w, h, sdur, s, threads=SCENE_THREADS)
        meta["render_hash"] = hsh
        write_json(d / "meta.json", meta)

    with ThreadPoolExecutor(SCENE_WORKERS) as ex:
        futs = [ex.submit(scene_job, it) for it in scenes]
        for k, f in enumerate(as_completed(futs)):
            f.result()
            progress(0.25 + 0.70 * (k + 1) / total, f"Dựng cảnh {k+1}/{total}")

    progress(0.96, "Ghép video cuối…")
    bgm = music.pick(s.bgm_mode, s.bgm_track, proj.bgm)
    concat_scenes(files, proj.output, bgm, s.bgm_volume, proj.work / "list.txt", t)
    lines = [f"{fmt_ts(ch_start[i])} {script['chapters'][i]['title']}" for i in sorted(ch_start)]
    (proj.dir / "chapters.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    progress(1.0, f"Hoàn tất! Độ dài {fmt_ts(t)}.")
