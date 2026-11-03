# Long Video Maker

Chủ đề → kịch bản dài (Gemini, theo từng chương) → giọng đọc (edge-tts) → footage (Pexels / video tự nhập)
→ phụ đề + nhạc nền → video 15–60 phút (FFmpeg).

## Chạy
```bash
cd LongVideoMaker
.venv/bin/streamlit run app.py     # hoặc: python -m venv .venv && .venv/bin/pip install -r requirements.txt
```
Cần FFmpeg. Nhập Gemini key (https://aistudio.google.com/apikey) và Pexels key (https://www.pexels.com/api/) ở thanh bên,
bấm "Lưu vào .env" để dùng lại.

## Vì sao làm được video dài
- Dàn ý → viết riêng từng chương (nối mạch bằng đoạn cuối chương trước) nên không bị cụt/lặp.
- Mỗi cảnh (~10–20s) được TTS, dựng thành clip riêng, rồi nối bằng concat (không re-encode) → ít RAM.
- Render chạy nền (worker), ghi `status.json`; chạy lại sẽ bỏ qua cảnh đã dựng (cache theo hash).
- Sửa lời/từ khóa trong UI rồi render lại: chỉ cảnh bị sửa được làm lại.
