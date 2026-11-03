import json
import os
import subprocess
import threading
import time
from pathlib import Path


def run(cmd, cwd=None):
    # utf-8 + replace: tên file tiếng Việt trong thông báo lỗi ffmpeg không làm vỡ việc đọc kết quả trên Windows
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise RuntimeError(f"{cmd[0]} lỗi:\n{r.stderr[-1500:]}")
    return r.stdout


def pid_alive(pid) -> bool:
    """Tiến trình còn chạy không (an toàn trên Windows: KHÔNG dùng os.kill(pid, 0) vì nó giết tiến trình)."""
    if not pid:
        return False
    if os.name == "nt":
        import ctypes
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x1000, False, int(pid))   # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = k.GetExitCodeProcess(h, ctypes.byref(code))
        k.CloseHandle(h)
        return bool(ok) and code.value == 259   # STILL_ACTIVE
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False


def duration(path) -> float:
    out = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
               "-of", "default=nw=1:nk=1", str(path)])
    return float(out.strip() or 0)


def video_size(path):
    out = run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
               "stream=width,height", "-of", "csv=p=0", str(path)])
    w, h = out.strip().split(",")[:2]
    return int(w), int(h)


def fmt_ts(sec: float) -> str:
    sec = int(sec)
    h, m, s = sec // 3600, sec % 3600 // 60, sec % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def read_json(p, default=None):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except Exception:
        return default


def write_json(p, data):
    # tên tmp riêng cho từng luồng để các luồng không giẫm lên nhau
    tmp = Path(f"{p}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    # Windows: ghi đè thất bại nếu giao diện đang mở đọc file đúng lúc đó -> thử lại
    for i in range(20):
        try:
            tmp.replace(p)
            return
        except PermissionError:
            time.sleep(0.05 * (i + 1))
    tmp.unlink(missing_ok=True)
    raise PermissionError(f"Không ghi được {p} (file đang bị chương trình khác giữ)")
