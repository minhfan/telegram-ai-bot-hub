# Telegram AI Bot Hub

A desktop control panel for running a roster of AI Telegram bots — many bots, many groups,
each bot with its own LLM provider, API key, and personality. Built on long-polling, so it
costs ~0 tokens while idle. One file, `app.py` (CustomTkinter UI + engine).

Bảng điều khiển đội bot Telegram trên desktop: nhiều bot, nhiều group, mỗi bot một provider
+ API key + nhân cách riêng. Dùng long-polling nên **idle ~0 token**.

## Tính năng

- **Group → Bot (2 tầng):** mỗi group nhập Chat ID + tên **1 lần dùng chung**; bên trong là các bot.
- **Đa provider mỗi bot:** Claude · Gemini · GPT (chọn riêng + API key riêng cho từng con).
- **Routing:** bot `mặc định` trả mọi tin không tag (vai PM); bot khác gọi bằng `@role`; `@all` gọi cả nhóm.
- **20 preset team** (Dev, Marketing, Tài chính, Y tế, … và Avengers, One Piece, Doraemon, Star Wars, …) —
  mỗi nhân vật có icon + system-prompt soạn sẵn theo template bullet, sửa thoải mái.
- **Giữ tinh hoa script gốc:** long-poll `getUpdates`, read-offset chống đọc lặp, lock chống chạy đè (tránh 409).

## Chạy thử (Mac / Windows / Linux)

```bash
pip install customtkinter
python app.py
```

Điền **Bot Token** (từ @BotFather — mỗi bot 1 token), **Group Chat ID** (supergroup `-100…`),
chọn **Provider** + **API Key**, đặt **Role/@tag** + **System Prompt**, rồi **▶ Khởi động**.
Bot phải là **admin** của group thì mới đọc được tin (privacy mode).

## Đóng gói .exe (Windows)

Push lên GitHub → tab **Actions** → workflow **Build .exe (Windows)** tự chạy (hoặc bấm *Run workflow*) →
tải **TelegramAIBotHub.exe** ở mục Artifacts. Build trên Mac không ra `.exe` được (không cross-compile),
nên dùng CI Windows này.

Build tay trên máy Windows:

```bash
pip install customtkinter pyinstaller
pyinstaller --noconfirm --onefile --windowed --collect-all customtkinter --name TelegramAIBotHub app.py
```

> `--collect-all customtkinter` là bắt buộc — thiếu nó .exe sẽ crash vì thiếu file theme.

## Bảo mật

API key + bot token được lưu local ở `~/.chronos_forge/settings.json` (plaintext, như config gốc).
Đừng commit/chia sẻ file đó. Lộ token thì rotate ở @BotFather.
