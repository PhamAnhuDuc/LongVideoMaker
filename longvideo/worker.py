"""Chạy tác vụ nền: python -m longvideo.worker <project> <script|render>.
Ghi tiến độ vào status.json để giao diện đọc (tắt trình duyệt vẫn chạy tiếp)."""
import os
import sys
import traceback

from .pipeline import Project, generate_keywords, generate_script, render, run_full
from .util import write_json


def main():
    name, job = sys.argv[1], sys.argv[2]
    proj = Project(name)
    status = proj.dir / "status.json"

    def progress(frac, msg):
        write_json(status, {"job": job, "state": "running", "frac": frac, "msg": msg, "pid": os.getpid()})

    try:
        progress(0, "Bắt đầu…")
        {"script": generate_script, "render": render, "keywords": generate_keywords, "full": run_full}[job](proj, progress)
        write_json(status, {"job": job, "state": "done", "frac": 1.0, "msg": "Hoàn tất", "pid": os.getpid()})
    except Exception as e:
        (proj.dir / "worker.log").write_text(traceback.format_exc(), encoding="utf-8")
        write_json(status, {"job": job, "state": "error", "frac": 0, "msg": str(e)[:1500], "pid": os.getpid()})
        sys.exit(1)


if __name__ == "__main__":
    main()
