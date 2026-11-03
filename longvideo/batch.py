"""Hàng đợi tạo nhiều video: python -m longvideo.batch <project1> <project2> ...
Chạy tuần tự (tránh bị giới hạn tốc độ của Gemini/edge-tts), lỗi một video không dừng cả hàng đợi."""
import os
import sys
import traceback

from .config import PROJECTS
from .pipeline import Project, run_full
from .util import read_json, write_json


def main():
    names = sys.argv[1:]
    pid = os.getpid()
    for n in names:
        write_json(Project(n).dir / "status.json", {"job": "full", "state": "queued", "frac": 0,
                                                    "msg": "Đang chờ trong hàng đợi", "pid": pid})
    for n in names:
        proj = Project(n)
        status = proj.dir / "status.json"
        cur = read_json(status, {})
        if cur.get("state") == "stopped":
            continue

        def progress(frac, msg, status=status):
            write_json(status, {"job": "full", "state": "running", "frac": frac, "msg": msg, "pid": pid})

        try:
            progress(0, "Bắt đầu…")
            run_full(proj, progress)
            write_json(status, {"job": "full", "state": "done", "frac": 1.0, "msg": "Hoàn tất", "pid": pid})
        except Exception as e:
            (proj.dir / "worker.log").write_text(traceback.format_exc(), encoding="utf-8")
            write_json(status, {"job": "full", "state": "error", "frac": 0, "msg": str(e)[:1500], "pid": pid})


if __name__ == "__main__":
    main()
