"""Thư viện nhạc nền: nhạc ambient tự tổng hợp bằng FFmpeg (không dính bản quyền) + file bạn thả vào assets/music/."""
import random
from pathlib import Path

from .config import MUSIC_DIR
from .util import run

EXT = {".mp3", ".wav", ".m4a", ".ogg"}

# (tên, [các hợp âm = tần số Hz], lowpass, tremolo Hz)
BUILTIN = {
    "Calm Pad": ([[110, 220, 261.63, 329.63], [87.31, 174.61, 220, 261.63], [130.81, 261.63, 329.63, 392],
                  [98, 196, 246.94, 293.66]], 1400, 0.15),
    "Warm Drift": ([[130.81, 261.63, 329.63, 392], [110, 220, 261.63, 329.63], [87.31, 174.61, 220, 261.63],
                    [98, 196, 246.94, 293.66]], 1000, 0.1),
    "Deep Space": ([[73.42, 146.83, 220, 293.66], [58.27, 116.54, 174.61, 233.08], [65.41, 130.81, 196, 261.63],
                    [73.42, 146.83, 220, 293.66]], 700, 0.1),
    "Bright Morning": ([[98, 196, 246.94, 293.66], [73.42, 146.83, 220, 293.66], [82.41, 164.81, 246.94, 329.63],
                        [65.41, 130.81, 261.63, 329.63]], 2200, 0.25),
}


def list_tracks():
    return sorted(p for p in MUSIC_DIR.iterdir() if p.suffix.lower() in EXT)


def pick(mode: str, track: str, project_bgm: Path | None):
    """Trả về đường dẫn nhạc nền theo cấu hình (hoặc None)."""
    if mode == "upload":
        return project_bgm
    if mode == "random":
        tr = list_tracks()
        return random.choice(tr) if tr else None
    if mode == "library":
        p = MUSIC_DIR / track
        return p if p.exists() else None
    return None


def _make(name, chords, lowpass, trem):
    seg, step = 16.0, 14.0
    total = step * (len(chords) - 1) + seg
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
    chains, labels, idx = [], [], 0
    for ci, chord in enumerate(chords):
        mix = []
        for f in chord:
            cmd += ["-f", "lavfi", "-t", str(seg), "-i", f"sine=f={f}:r=44100"]
            mix.append(f"[{idx}:a]")
            idx += 1
        delay = int(ci * step * 1000)
        chains.append("".join(mix) + f"amix=inputs={len(chord)}:normalize=0,volume=0.5,"
                      f"afade=t=in:d=4,afade=t=out:st={seg - 4}:d=4,adelay={delay}|{delay}[c{ci}]")
        labels.append(f"[c{ci}]")
    chains.append("".join(labels) + f"amix=inputs={len(chords)}:normalize=0,"
                  f"tremolo=f={trem}:d=0.35,lowpass=f={lowpass},aecho=0.8:0.6:700|1100:0.35|0.25,"
                  f"alimiter=limit=0.8,aformat=channel_layouts=stereo[out]")
    out = MUSIC_DIR / f"{name}.mp3"
    run(cmd + ["-filter_complex", ";".join(chains), "-map", "[out]", "-t", f"{total:.1f}",
               "-b:a", "128k", str(out)])
    return out


def ensure_library():
    """Tạo các bài nhạc dựng sẵn nếu chưa có."""
    for name, (chords, lp, trem) in BUILTIN.items():
        if not (MUSIC_DIR / f"{name}.mp3").exists():
            try:
                _make(name, chords, lp, trem)
            except Exception as e:
                print("Không tạo được nhạc", name, e)


if __name__ == "__main__":
    ensure_library()
    print([p.name for p in list_tracks()])
