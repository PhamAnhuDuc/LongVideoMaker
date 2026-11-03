import os
import signal
import subprocess
import sys

import streamlit as st

from longvideo import music
from longvideo.compose import preview_subtitle
from longvideo.config import (CACHE, LANG_NAME, ROOT, SOURCES, TRANSITIONS, VOICES, WORDS_PER_MIN,
                              Settings, save_env)
from longvideo.footage import local_clips
from longvideo.pipeline import Project, list_projects
from longvideo.script_parse import parse_script
from longvideo.tts import synth
from longvideo.util import fmt_ts, read_json

st.set_page_config(page_title="Long Video Maker", page_icon="🎬", layout="wide")

# ---------- Thanh bên: khóa API + chọn dự án ----------
with st.sidebar:
    st.header("🔑 API")
    gk = st.text_input("Gemini API key", value=os.environ.get("GEMINI_API_KEY", ""), type="password")
    pk = st.text_input("Pexels API key", value=os.environ.get("PEXELS_API_KEY", ""), type="password")
    xk = st.text_input("Pixabay API key (tùy chọn)", value=os.environ.get("PIXABAY_API_KEY", ""), type="password")
    if xk:
        os.environ["PIXABAY_API_KEY"] = xk
    if gk:
        os.environ["GEMINI_API_KEY"] = gk
    if pk:
        os.environ["PEXELS_API_KEY"] = pk
    if st.button("Lưu vào .env"):
        save_env({"GEMINI_API_KEY": gk, "PEXELS_API_KEY": pk, "PIXABAY_API_KEY": xk})
        st.success("Đã lưu")
    st.divider()
    st.header("📁 Dự án")
    projects = list_projects()
    if "pending_choice" in st.session_state:
        st.session_state["proj_choice"] = st.session_state.pop("pending_choice")
    choice = st.selectbox("Mở dự án", ["➕ Dự án mới"] + projects, key="proj_choice")

is_new = choice == "➕ Dự án mới"
proj = None if is_new else Project(choice)
settings, script = (Settings(), None) if is_new else proj.load()


def start_job(p: Project, job: str):
    p.dir.mkdir(exist_ok=True)
    proc = subprocess.Popen([sys.executable, "-m", "longvideo.worker", p.name, job], cwd=ROOT,
                            env=os.environ.copy(), start_new_session=True,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    from longvideo.util import write_json
    write_json(p.dir / "status.json", {"job": job, "state": "running", "frac": 0, "msg": "Khởi động…", "pid": proc.pid})


st.title("🎬 Long Video Maker")
st.caption("Chủ đề → kịch bản dài (Gemini) → giọng đọc → footage → video 15–60 phút")

tab1, tab2, tab3 = st.tabs(["1️⃣ Chủ đề & kịch bản", "2️⃣ Giọng · hình · nhạc", "3️⃣ Render & tải về"])

# ---------- Tab 1 ----------
with tab1:
    c1, c2 = st.columns([2, 1])
    topic = c1.text_area("Chủ đề video", settings.topic, height=100,
                         placeholder="VD: Lịch sử sao Hỏa và hành trình đưa con người lên đó")
    lang = c2.radio("Ngôn ngữ", ["vi", "en"], index=["vi", "en"].index(settings.language),
                    format_func=LANG_NAME.get, horizontal=True)
    minutes = c2.slider("Độ dài mục tiêu (phút)", 15, 60, max(15, settings.minutes), step=5)
    c3, c4 = st.columns(2)
    tone = c3.text_input("Giọng điệu", settings.tone)
    audience = c4.text_input("Khán giả", settings.audience)
    notes = st.text_input("Ghi chú thêm (tùy chọn)", settings.notes)
    model = st.text_input("Model Gemini", settings.gemini_model)

# ---------- Tab 2 ----------
SUB_FONTS = ["DejaVu Sans", "Liberation Sans", "Noto Sans", "Arial", "Roboto", "Open Sans"]
SUB_SAMPLE = {"vi": "Đây là phụ đề mẫu để xem thử kiểu chữ.", "en": "This is a sample subtitle to preview the style."}
VOICE_SAMPLE = {"vi": "Xin chào, đây là giọng đọc thử của video này.", "en": "Hello, this is a preview of the narrator voice."}
with tab2:
    voices = VOICES[lang]
    st.subheader("🎙️ Giọng đọc")
    c1, c2 = st.columns(2)
    voice = c1.selectbox("Giọng đọc", voices, index=voices.index(settings.voice) if settings.voice in voices else 0)
    rate = c1.slider("Tốc độ đọc (%)", -30, 30, settings.rate)
    voice_volume = c2.slider("Âm lượng giọng (%)", 50, 150, settings.voice_volume)
    sample = c2.text_input("Câu đọc thử", VOICE_SAMPLE[lang])
    if c2.button("▶️ Nghe thử giọng"):
        try:
            out = CACHE / "voice_preview.mp3"
            with st.spinner("Đang tạo giọng…"):
                synth(sample, voice, rate, out)
            st.session_state["voice_preview"] = out.read_bytes()
        except Exception as e:
            st.error(f"Không tạo được giọng thử: {e}")
    if st.session_state.get("voice_preview"):
        c2.audio(st.session_state["voice_preview"], format="audio/mp3")

    st.divider()
    st.subheader("🖼️ Hình ảnh")
    c1, c2 = st.columns(2)
    aspect = c1.radio("Tỉ lệ khung hình", ["16:9", "9:16"], index=["16:9", "9:16"].index(settings.aspect),
                      horizontal=True)
    height = c1.radio("Độ phân giải", [720, 1080], index=[720, 1080].index(settings.height), horizontal=True,
                      format_func=lambda x: f"{x}p")
    sources = c2.multiselect("Nguồn footage", SOURCES, default=[x for x in settings.footage_sources if x in SOURCES],
                             format_func={"pexels": "Pexels", "pixabay": "Pixabay", "local": "Video tự nhập"}.get)
    clip_mode = c2.radio("Cách chọn clip", ["random", "sequential"], horizontal=True,
                         index=["random", "sequential"].index(settings.clip_mode),
                         format_func={"random": "Ngẫu nhiên", "sequential": "Lần lượt"}.get)
    c3, c4, c5 = st.columns(3)
    max_clip = c3.slider("Độ dài tối đa mỗi clip (giây)", 3.0, 15.0, float(settings.max_clip_sec), 0.5)
    transition = c4.selectbox("Chuyển cảnh", TRANSITIONS,
                              index=TRANSITIONS.index(settings.transition) if settings.transition in TRANSITIONS else 0)
    trans_dur = c5.slider("Thời lượng chuyển cảnh (s)", 0.2, 1.5, float(settings.transition_dur), 0.1,
                          disabled=transition == "none")

    st.divider()
    st.subheader("💬 Phụ đề")
    subs = st.checkbox("Chèn phụ đề", settings.subtitles)
    sub_timing, sub_font, sub_bold = settings.sub_timing, settings.sub_font, settings.sub_bold
    sub_color, sub_outline_color = settings.sub_color, settings.sub_outline_color
    sub_size, sub_outline, sub_position, sub_margin = (settings.sub_size, settings.sub_outline,
                                                       settings.sub_position, settings.sub_margin)
    whisper_model = settings.whisper_model
    if subs:
        c1, c2, c3 = st.columns(3)
        timing_opts = ["sentence", "word", "whisper"]
        sub_timing = c1.selectbox("Cách căn thời gian", timing_opts,
                                  index=timing_opts.index(settings.sub_timing) if settings.sub_timing in timing_opts else 0,
                                  format_func={"sentence": "Theo câu (nhanh)", "word": "Theo từng từ (nếu giọng hỗ trợ)",
                                               "whisper": "Whisper (chính xác, chậm)"}.get)
        if sub_timing == "whisper":
            whisper_model = c1.selectbox("Model Whisper", ["tiny", "base", "small"],
                                         index=["tiny", "base", "small"].index(settings.whisper_model)
                                         if settings.whisper_model in ("tiny", "base", "small") else 1)
        fonts = SUB_FONTS if settings.sub_font in SUB_FONTS else [settings.sub_font] + SUB_FONTS
        sub_font = c2.selectbox("Font", fonts, index=fonts.index(settings.sub_font))
        sub_bold = c3.checkbox("In đậm", settings.sub_bold)
        c1, c2, c3, c4 = st.columns(4)
        sub_color = c1.color_picker("Màu chữ", settings.sub_color)
        sub_outline_color = c2.color_picker("Màu viền", settings.sub_outline_color)
        sub_size = c3.slider("Cỡ chữ (% chiều cao)", 3.0, 10.0, float(settings.sub_size), 0.1)
        sub_outline = c4.slider("Độ dày viền", 0.0, 8.0, float(settings.sub_outline), 0.5)
        c1, c2 = st.columns(2)
        sub_position = c1.radio("Vị trí", ["bottom", "middle", "top"], horizontal=True,
                                index=["bottom", "middle", "top"].index(settings.sub_position),
                                format_func={"bottom": "Dưới", "middle": "Giữa", "top": "Trên"}.get)
        sub_margin = c2.slider("Cách mép (% chiều cao)", 0.0, 30.0, float(settings.sub_margin), 0.5,
                               disabled=sub_position == "middle")

    cur = Settings(topic=topic, language=lang, minutes=minutes, tone=tone, audience=audience, notes=notes,
                   gemini_model=model, voice=voice, rate=rate, voice_volume=voice_volume, aspect=aspect,
                   height=height, footage_sources=sources, clip_mode=clip_mode, max_clip_sec=max_clip,
                   transition=transition, transition_dur=trans_dur, subtitles=subs, sub_timing=sub_timing,
                   whisper_model=whisper_model, sub_font=sub_font, sub_bold=sub_bold, sub_color=sub_color,
                   sub_outline_color=sub_outline_color, sub_size=sub_size, sub_outline=sub_outline,
                   sub_position=sub_position, sub_margin=sub_margin, bgm_mode=settings.bgm_mode,
                   bgm_track=settings.bgm_track, bgm_volume=settings.bgm_volume)
    if subs:
        try:
            png = CACHE / "sub_preview.png"
            preview_subtitle(cur, SUB_SAMPLE[lang], png)
            st.image(str(png), caption="Xem thử phụ đề", width=480)
        except Exception as e:
            st.warning(f"Không tạo được ảnh xem thử (kiểm tra font): {e}")

    st.divider()
    st.subheader("🎵 Nhạc nền")
    music.ensure_library()
    modes = ["none", "random", "library", "upload"]
    bgm_mode = st.radio("Nhạc nền", modes, horizontal=True,
                        index=modes.index(settings.bgm_mode) if settings.bgm_mode in modes else 0,
                        format_func={"none": "Không", "random": "Ngẫu nhiên", "library": "Chọn bài",
                                     "upload": "Tải lên"}.get)
    cur.bgm_mode = bgm_mode
    if bgm_mode == "library":
        names = [p.name for p in music.list_tracks()]
        if names:
            cur.bgm_track = st.selectbox("Bài nhạc", names, index=names.index(settings.bgm_track)
                                         if settings.bgm_track in names else 0)
            st.audio(str(music.MUSIC_DIR / cur.bgm_track))
        else:
            st.info("Thư viện nhạc trống (assets/music/).")
    if bgm_mode != "none":
        cur.bgm_volume = st.slider("Âm lượng nhạc nền", 0.0, 0.5, float(settings.bgm_volume), 0.01)

    if is_new:
        st.info("Hãy nhập chủ đề rồi bấm **Tạo kịch bản** (hoặc dán kịch bản) ở tab 1 để tạo dự án, "
                "sau đó quay lại đây tải video/nhạc lên.")
    else:
        if "local" in sources:
            ups = st.file_uploader("Tải video của bạn (nhiều file)", type=["mp4", "mov", "mkv", "webm"],
                                   accept_multiple_files=True)
            for u in ups or []:
                dest = proj.media / u.name
                if not dest.exists():
                    dest.write_bytes(u.getbuffer())
            st.caption(f"Đang có {len(local_clips(proj.media))} video trong dự án.")
        if bgm_mode == "upload":
            bg = st.file_uploader("Nhạc nền của bạn", type=["mp3", "wav", "m4a", "ogg"])
            if bg:
                for old in ("mp3", "wav", "m4a", "ogg"):
                    (proj.dir / f"bgm.{old}").unlink(missing_ok=True)
                (proj.dir / f"bgm.{bg.name.rsplit('.', 1)[-1].lower()}").write_bytes(bg.getbuffer())
            if proj.bgm:
                st.audio(str(proj.bgm))
        if st.button("💾 Lưu cài đặt"):
            proj.save(cur, script)
            st.success("Đã lưu")

# ---------- Trạng thái tác vụ nền ----------
def status_box(p: Project, job_filter=None):
    stt = read_json(p.dir / "status.json")
    if not stt or (job_filter and stt["job"] not in ((job_filter,) if isinstance(job_filter, str) else job_filter)):
        return None
    if stt["state"] == "running":
        st.progress(min(stt["frac"], 1.0), text=stt["msg"])
        if st.button("⏹ Dừng", key=f"stop_{stt['job']}"):
            try:
                os.killpg(os.getpgid(stt["pid"]), signal.SIGTERM)
            except Exception:
                pass
            from longvideo.util import write_json
            write_json(p.dir / "status.json", {**stt, "state": "stopped", "msg": "Đã dừng"})
            st.rerun()
    elif stt["state"] == "error":
        st.error(stt["msg"])
    elif stt["state"] == "done":
        st.success(stt["msg"])
    return stt["state"]


# ---------- Tab 1 (tiếp): tạo + sửa kịch bản ----------
with tab1:
    if st.button("✨ Tạo kịch bản", type="primary", disabled=not topic.strip()):
        p = proj or Project.create(topic)
        p.save(cur, None)
        start_job(p, "script")
        st.session_state["pending_choice"] = p.name
        st.rerun()

    with st.expander("📋 Hoặc dán kịch bản có sẵn (bỏ qua bước Gemini viết)"):
        st.caption("Dòng bắt đầu bằng `#` = tên chương. Các đoạn cách nhau dòng trống = các cảnh "
                   "(đoạn dài tự tách ~45 từ). Không có `#` thì tự chia chương.")
        pasted = st.text_area("Kịch bản", height=260, key="pasted_script",
                              placeholder="# Mở đầu\nĐoạn lời đọc thứ nhất...\n\nĐoạn thứ hai...\n\n# Chương 2\n...")
        ptitle = st.text_input("Tiêu đề video (tùy chọn)", key="pasted_title")
        auto_kw = st.checkbox("Tự tạo từ khóa footage bằng Gemini (cần Gemini key)", value=True)
        if st.button("📥 Dùng kịch bản này", disabled=not pasted.strip()):
            parsed = parse_script(pasted, ptitle or topic.strip())
            if not parsed["chapters"]:
                st.error("Không đọc được nội dung kịch bản.")
            else:
                p = proj or Project.create(ptitle or topic or parsed["title"])
                for ch in parsed["chapters"]:  # chưa có từ khóa: dùng chủ đề/tiêu đề làm dự phòng
                    for sc in ch["scenes"]:
                        sc["keywords"] = []
                p.save(cur, parsed)
                if auto_kw and os.environ.get("GEMINI_API_KEY"):
                    start_job(p, "keywords")
                else:
                    for ch in parsed["chapters"]:
                        for sc in ch["scenes"]:
                            sc["keywords"] = [x for x in [topic.strip() or parsed["title"]] if x]
                    p.save(cur, parsed)
                    if auto_kw:
                        st.warning("Chưa có Gemini key nên từ khóa tạm lấy theo tiêu đề; hãy sửa tay từ khóa (tiếng Anh).")
                st.session_state["pending_choice"] = p.name
                st.rerun()

    if proj:
        @st.fragment(run_every=3)
        def script_status():
            s = status_box(proj, ("script", "keywords"))
            if s == "running":
                pass
        script_status()

        script = proj.load()[1]
        if script:
            words = sum(len(x["narration"].split()) for c in script["chapters"] for x in c["scenes"])
            st.subheader(script["title"])
            st.caption(f"{script['description']}  \n**{len(script['chapters'])} chương · {words} từ · "
                       f"ước tính ~{words / WORDS_PER_MIN[settings.language]:.0f} phút**")
            edited = False
            for ci, ch in enumerate(script["chapters"]):
                with st.expander(f"Chương {ci+1}: {ch['title']}  ({len(ch['scenes'])} cảnh)"):
                    for si, sc in enumerate(ch["scenes"]):
                        a, b = st.columns([3, 1])
                        n = a.text_area(f"Cảnh {si+1}", sc["narration"], key=f"n{ci}_{si}", height=90)
                        k = b.text_area("Từ khóa (mỗi dòng 1)", "\n".join(sc["keywords"]), key=f"k{ci}_{si}", height=90)
                        kws = [x.strip() for x in k.splitlines() if x.strip()]
                        if n != sc["narration"] or kws != sc["keywords"]:
                            sc["narration"], sc["keywords"] = n, kws
                            edited = True
            if edited:
                proj.save(settings, script)
            if st.button("🔄 Viết lại toàn bộ kịch bản"):
                start_job(proj, "script")
                st.rerun()

# ---------- Tab 3 ----------
with tab3:
    if not proj or not script:
        st.info("Cần có kịch bản trước.")
    else:
        if st.button("🎬 Bắt đầu render", type="primary"):
            proj.save(cur, script)
            start_job(proj, "render")
            st.rerun()
        st.caption("Render chạy nền, có thể đóng trình duyệt. Chạy lại sẽ bỏ qua các cảnh đã dựng xong.")

        @st.fragment(run_every=3)
        def render_status():
            status_box(proj, "render")
        render_status()

        if proj.output.exists():
            st.video(str(proj.output))
            with open(proj.output, "rb") as f:
                st.download_button("⬇️ Tải video", f, file_name=f"{proj.name}.mp4", mime="video/mp4")
            ch = proj.dir / "chapters.txt"
            if ch.exists():
                st.text_area("Mốc chương (dán vào mô tả YouTube)", ch.read_text(encoding="utf-8"), height=150)
