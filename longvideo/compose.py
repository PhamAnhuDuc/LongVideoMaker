"""Dựng từng cảnh (footage + chuyển cảnh + giọng + phụ đề) rồi ghép thành video dài."""
import math
import random
import tempfile
from pathlib import Path

from .config import Settings
from .footage import is_image
from .tts import split_cues, word_cues
from .util import run

REAL_TRANSITIONS = ["fade", "dissolve", "wipeleft", "slideleft", "circleopen", "smoothleft"]


def _ass_time(t):
    cs = int(round(t * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def _ass_color(hexcol: str) -> str:
    h = hexcol.lstrip("#")
    r, g, b = h[0:2], h[2:4], h[4:6]
    return f"&H00{b}{g}{r}".upper()


def scene_cues(meta: dict, portrait: bool):
    """Các dòng phụ đề của cảnh: theo từng từ nếu có, không thì theo câu."""
    max_words = 6 if portrait else 9
    if meta.get("words"):
        return word_cues([tuple(w) for w in meta["words"]], max_words)
    return split_cues([tuple(c) for c in meta["cues"]], max_words)


def write_ass(cues, path: Path, w, h, st: Settings):
    portrait = h > w
    line_chars = 24 if portrait else 44
    font = max(10, int(h * st.sub_size / 100))
    margin = int(h * st.sub_margin / 100)
    align = {"bottom": 2, "middle": 5, "top": 8}.get(st.sub_position, 2)
    outline = max(0.0, st.sub_outline * h / 720)
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {w}
PlayResY: {h}
WrapStyle: 2

[V4+ Styles]
Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding
Style: Default,{st.sub_font},{font},{_ass_color(st.sub_color)},&H000000FF,{_ass_color(st.sub_outline_color)},&H80000000,{-1 if st.sub_bold else 0},0,0,0,100,100,0,0,1,{outline:.1f},1,{align},40,40,{margin},1

[Events]
Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text
"""
    lines = []
    for s0, e0, text in cues:
        rows, cur = [], ""
        for wd in text.split():
            if cur and len(cur) + len(wd) + 1 > line_chars:
                rows.append(cur)
                cur = wd
            else:
                cur = f"{cur} {wd}".strip()
        rows.append(cur)
        lines.append(f"Dialogue: 0,{_ass_time(s0)},{_ass_time(e0)},Default,,0,0,0,," + "\\N".join(rows))
    path.write_text(head + "\n".join(lines) + "\n", encoding="utf-8")


def preview_subtitle(st: Settings, text: str, out_png: Path):
    """Ảnh xem thử kiểu phụ đề."""
    w, h = st.size()
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        write_ass([(0, 5, text)], td / "p.ass", w, h, st)
        run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
             f"color=c=0x37506b:s={w}x{h}:d=1",
             "-vf", "ass=p.ass", "-frames:v", "1", str(out_png.resolve())], cwd=td)


def plan_segments(timeline, picker, max_clip, trans):
    """timeline = [(start, end, từ khóa)] theo từng câu. Mỗi câu một clip đúng chủ đề;
    câu dài hơn max_clip thì chia đều thành vài clip cùng từ khóa."""
    segs = []
    for s0, e0, kws in timeline:
        length = e0 - s0
        parts = max(1, math.ceil(length / max_clip - 0.15))
        for _ in range(parts):
            d = length / parts
            path, cdur = picker.pick(kws)
            need = d + trans
            if path is None:
                segs.append({"path": None, "start": 0, "dur": round(d, 3), "loop": False})
            else:
                loop = cdur < need + 0.2
                start = 0 if loop else (random.uniform(0, max(0, cdur - need - 0.1)) if picker.mode == "random" else 0)
                segs.append({"path": str(path), "start": round(start, 2), "dur": round(d, 3), "loop": loop})
    return segs


def pinned_segments(files, media_dir: Path, total, trans):
    """Cảnh có ảnh/video người dùng chọn sẵn: chia đều thời lượng cảnh cho các file theo thứ tự."""
    from .util import duration
    paths = [media_dir / f for f in files if (media_dir / f).exists()]
    if not paths:
        return None
    d = total / len(paths)
    segs = []
    for p in paths:
        if is_image(p):
            segs.append({"path": str(p), "start": 0, "dur": round(d, 3), "loop": False})
            continue
        try:
            cdur = duration(p)
        except Exception:
            cdur = 0
        loop = cdur < d + trans + 0.2
        segs.append({"path": str(p), "start": 0, "dur": round(d, 3), "loop": loop})
    return segs


def _ken_burns(w, h, dur, fps, i):
    """Ảnh tĩnh -> zoom chậm (xen kẽ phóng to / thu nhỏ). Phóng ảnh gấp đôi trước để zoom không bị rung."""
    frames = max(1, int(dur * fps))
    step = 0.12 / frames
    z = f"min(1+{step:.6f}*on,1.12)" if i % 2 == 0 else f"max(1.12-{step:.6f}*on,1)"
    return (f"scale={w * 2}:{h * 2}:force_original_aspect_ratio=increase,crop={w * 2}:{h * 2},"
            f"zoompan=z='{z}':d=1:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={w}x{h}:fps={fps}")


def render_scene(segs, audio: Path, ass: Path | None, out: Path, w, h, total, st: Settings, fps=30, threads=0):
    n = len(segs)
    t = st.transition_dur if (st.transition != "none" and n > 1) else 0.0
    if t:
        t = min(t, 0.4 * min(s["dur"] for s in segs))
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
    chains = []
    for i, sg in enumerate(segs):
        dur = sg["dur"] + (t if i < n - 1 else 0)
        if sg["path"] is None:
            cmd += ["-f", "lavfi", "-t", f"{dur:.3f}", "-i", f"color=c=0x101828:s={w}x{h}:r={fps}"]
        elif is_image(sg["path"]):
            cmd += ["-loop", "1", "-framerate", str(fps), "-t", f"{dur:.3f}", "-i", sg["path"]]
            chains.append(f"[{i}:v]{_ken_burns(w, h, dur, fps, i)},setsar=1,format=yuv420p,"
                          f"setpts=PTS-STARTPTS[v{i}]")
            continue
        elif sg["loop"]:
            cmd += ["-stream_loop", "-1", "-t", f"{dur:.3f}", "-i", sg["path"]]
        else:
            cmd += ["-ss", str(sg["start"]), "-t", f"{dur:.3f}", "-i", sg["path"]]
        chains.append(f"[{i}:v]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},"
                      f"setsar=1,fps={fps},format=yuv420p,setpts=PTS-STARTPTS[v{i}]")
    if n == 1:
        join = "[v0]null[vc]"
    elif t:
        parts, prev, acc = [], "v0", 0.0
        for i in range(1, n):
            acc += segs[i - 1]["dur"]
            name = random.choice(REAL_TRANSITIONS) if st.transition == "random" else st.transition
            parts.append(f"[{prev}][v{i}]xfade=transition={name}:duration={t:.3f}:offset={acc:.3f}[x{i}]")
            prev = f"x{i}"
        join = ";".join(parts) + f";[{prev}]null[vc]"
    else:
        join = "".join(f"[v{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=0[vc]"
    # Ép đầu ra về yuv420p dải tv: ảnh JPEG/footage 4:4:4 nếu lọt qua sẽ thành H.264 High 4:4:4,
    # trình duyệt không phát được và ghép -c copy với cảnh 4:2:0 làm video đứt giữa chừng.
    fix = "scale=out_range=tv,format=yuv420p"
    last = f"[vc]ass={ass.name},{fix}[vo]" if ass else f"[vc]{fix}[vo]"
    vol = f"volume={st.voice_volume / 100:.2f}," if st.voice_volume != 100 else ""
    cmd += ["-i", str(audio), "-filter_complex", ";".join(chains + [join, last]),
            "-map", "[vo]", "-map", f"{n}:a", "-af", f"{vol}apad,aresample=44100", "-t", f"{total:.3f}",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-r", str(fps),
            "-pix_fmt", "yuv420p", "-profile:v", "high", "-color_range", "tv",
            *(["-threads", str(threads), "-filter_complex_threads", str(threads)] if threads else []),
            "-c:a", "aac", "-b:a", "160k", "-ac", "2", "-movflags", "+faststart", str(out.resolve())]
    run(cmd, cwd=ass.parent if ass else None)


def concat_scenes(scene_files, out: Path, bgm: Path | None, bgm_volume: float, listfile: Path, total: float):
    listfile.write_text("".join(f"file '{str(p.resolve()).replace(chr(39), chr(39) + chr(92) + chr(39) + chr(39))}'\n"
                                for p in scene_files), encoding="utf-8")
    base = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(listfile)]
    if not bgm:
        run(base + ["-c", "copy", "-movflags", "+faststart", str(out)])
        return
    fade_out = max(0.0, total - 3)
    run(base + ["-stream_loop", "-1", "-i", str(bgm), "-filter_complex",
                f"[1:a]volume={bgm_volume},afade=t=in:d=2,afade=t=out:st={fade_out:.2f}:d=3[b];"
                f"[0:a][b]amix=inputs=2:duration=first:normalize=0[a]",
                "-map", "0:v", "-map", "[a]", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                "-movflags", "+faststart", str(out)])
