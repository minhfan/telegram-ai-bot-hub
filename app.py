#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram AI Bot Hub — Desktop App (multi-bot / multi-group)
===========================================================
Quản lý NHIỀU bot, NHIỀU group trong cùng 1 app. Mỗi bot là 1 CARD hiển thị
thành 1 hàng (thấy hết cùng lúc, không phải switch dropdown). Có thư viện PRESET
team: nạp cả 1 team nhân vật (mỗi nhân vật = 1 bot) với system-prompt soạn sẵn
theo template bullet cố định cho dễ customize.

Mỗi "bot" là 1 khối TỰ CHỨA:
    { token, chat_id, group_name, provider, api_key, role(@tag), is_default, system_prompt }
  - token      : token của bot (từ @BotFather). Group KHÔNG có token, chỉ có Chat ID.
  - chat_id    : Group Chat ID (supergroup dạng -1001234567890).
  - group_name : tên gợi nhớ do user tự đặt (chỉ để dễ nhìn, không ảnh hưởng routing).
  - provider   : Claude | Gemini | GPT  (RIÊNG từng bot).
  - api_key    : API key của provider tương ứng (RIÊNG từng bot).
  - role(@tag) : nhãn vai trò user tự gõ; chính nó là tag để gọi bot (vd @anti, @dev).
  - is_default : bot "mặc định" trả lời mọi tin KHÔNG có @tag (vai PM cũ).
  - system_prompt : RIÊNG từng bot — đoạn 'system' gửi vào LLM để nhân cách hoá bot đó.

Định tuyến 1 tin trong group:
  @all/@both -> mọi bot ; @<tag> -> bot có role==<tag> ; không tag -> bot is_default.

Telegram chỉ cho 1 poller / 1 token -> app GOM bot theo token (1 poller/token, route
theo Chat ID). Giữ tinh hoa bản script: long-poll getUpdates (~0 token idle), read-offset
trên đĩa chống đọc lặp, lock chống chạy đè (tránh 409).

Phụ thuộc: chỉ cần `customtkinter`.   pip install customtkinter
Đóng gói WINDOWS:
    pyinstaller --noconfirm --onefile --windowed --collect-all customtkinter app.py
"""

import json
import os
import queue
import socket
import threading
import time
import tkinter as tk
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import customtkinter as ctk

# ───────────────────────────── Cấu hình tĩnh ─────────────────────────────
APP_NAME = "Telegram AI Bot Hub"
DATA_DIR = Path.home() / ".chronos_forge"
SETTINGS_FILE = DATA_DIR / "settings.json"

LONG_POLL_SECONDS = 25
HTTP_EXTRA_TIMEOUT = 20
BACKOFF_SECONDS = 5
TELEGRAM_TEXT_LIMIT = 4000

PROVIDERS = ["Claude", "Gemini", "GPT"]

CLAUDE_MODEL = "claude-opus-4-8"
CLAUDE_MAX_TOKENS = 1024
GEMINI_MODEL = "gemini-2.0-flash"
GEMINI_MAX_TOKENS = 1024
GPT_MODEL = "gpt-4o"
GPT_MAX_TOKENS = 1024


# ───────────────────────────── Lớp HTTP (urllib) ─────────────────────────────
def _http_json(url, *, method="GET", headers=None, body=None, timeout=30):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
    return json.loads(raw) if raw else {}


def _read_http_error(err):
    try:
        return err.read().decode("utf-8")[:500]
    except Exception:
        return str(err)


def _split_chunks(text, size):
    text = text or ""
    if len(text) <= size:
        return [text]
    return [text[i:i + size] for i in range(0, len(text), size)]


def _pid_alive(pid):
    if os.name == "nt":
        try:
            import ctypes
            h = ctypes.windll.kernel32.OpenProcess(0x0400, False, pid)
            if h:
                ctypes.windll.kernel32.CloseHandle(h)
                return True
            return False
        except Exception:
            return True
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


# ───────────────────────────── Telegram Bot API ─────────────────────────────
def tg_get_updates(token, offset, timeout_secs):
    params = {"timeout": timeout_secs, "allowed_updates": json.dumps(["message"])}
    if offset is not None:
        params["offset"] = offset
    url = "https://api.telegram.org/bot%s/getUpdates?%s" % (token, urllib.parse.urlencode(params))
    return _http_json(url, method="GET", timeout=timeout_secs + HTTP_EXTRA_TIMEOUT)


def tg_send_message(token, chat_id, text):
    url = "https://api.telegram.org/bot%s/sendMessage" % token
    return _http_json(url, method="POST", body={"chat_id": chat_id, "text": text}, timeout=30)


# ───────────────────────────── LLM Providers ─────────────────────────────
def call_claude(api_key, system_prompt, user_text):
    url = "https://api.anthropic.com/v1/messages"
    headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01"}
    body = {"model": CLAUDE_MODEL, "max_tokens": CLAUDE_MAX_TOKENS,
            "messages": [{"role": "user", "content": user_text}]}
    if system_prompt:
        body["system"] = system_prompt
    data = _http_json(url, method="POST", headers=headers, body=body, timeout=120)
    parts = [b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"]
    return "".join(parts).strip() or "(Claude trả lời rỗng)"


def call_gemini(api_key, system_prompt, user_text):
    url = "https://generativelanguage.googleapis.com/v1beta/models/%s:generateContent" % GEMINI_MODEL
    headers = {"x-goog-api-key": api_key}
    body = {"contents": [{"role": "user", "parts": [{"text": user_text}]}],
            "generationConfig": {"maxOutputTokens": GEMINI_MAX_TOKENS}}
    if system_prompt:
        body["system_instruction"] = {"parts": [{"text": system_prompt}]}
    data = _http_json(url, method="POST", headers=headers, body=body, timeout=120)
    candidates = data.get("candidates", [])
    if not candidates:
        block = (data.get("promptFeedback") or {}).get("blockReason")
        return "(Gemini không trả candidate%s)" % (" — blocked: %s" % block if block else "")
    parts = (candidates[0].get("content") or {}).get("parts", [])
    return "".join(p.get("text", "") for p in parts).strip() or "(Gemini trả lời rỗng)"


def call_gpt(api_key, system_prompt, user_text):
    url = "https://api.openai.com/v1/chat/completions"
    headers = {"Authorization": "Bearer %s" % api_key}
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": user_text})
    body = {"model": GPT_MODEL, "messages": messages, "max_tokens": GPT_MAX_TOKENS}
    data = _http_json(url, method="POST", headers=headers, body=body, timeout=120)
    choices = data.get("choices", [])
    if not choices:
        return "(GPT không trả choice)"
    return (choices[0].get("message", {}).get("content") or "").strip() or "(GPT trả lời rỗng)"


def call_llm(provider, api_key, system_prompt, user_text):
    if provider == "Claude":
        return call_claude(api_key, system_prompt, user_text)
    if provider == "Gemini":
        return call_gemini(api_key, system_prompt, user_text)
    if provider == "GPT":
        return call_gpt(api_key, system_prompt, user_text)
    return "(provider lạ: %s)" % provider


# ───────────────────────────── Background Worker (1 / token) ─────────────────────────────
class TokenWorker(threading.Thread):
    def __init__(self, token, entries, log_cb, skip_backlog):
        super().__init__(daemon=True)
        self.token = token
        self.log = log_cb
        self.skip_backlog = skip_backlog
        self.stop_event = threading.Event()
        self.bot_id = token.split(":")[0]
        self.offset_file = DATA_DIR / (".offset_%s" % self.bot_id)
        self.lock_file = DATA_DIR / ("forge_%s.lock" % self.bot_id)
        self.entries_by_chat = {str(e["chat_id"]): e for e in entries}

    def _read_offset(self):
        try:
            return int(self.offset_file.read_text().strip())
        except Exception:
            return None

    def _write_offset(self, value):
        try:
            self.offset_file.write_text(str(value))
        except Exception as e:
            self.log("⚠ [#%s] không ghi offset: %s" % (self.bot_id, e))

    def _acquire_lock(self):
        try:
            if self.lock_file.exists():
                old = self.lock_file.read_text().strip()
                if old and old.isdigit() and int(old) != os.getpid() and _pid_alive(int(old)):
                    self.log("⛔ Token #%s đang được tiến trình khác (PID %s) poll — "
                             "tránh 409, không chạy đè." % (self.bot_id, old))
                    return False
            self.lock_file.write_text(str(os.getpid()))
            return True
        except Exception as e:
            self.log("⚠ [#%s] không tạo lock (%s) — chạy tiếp." % (self.bot_id, e))
            return True

    def _release_lock(self):
        try:
            if self.lock_file.exists() and self.lock_file.read_text().strip() == str(os.getpid()):
                self.lock_file.unlink()
        except Exception:
            pass

    @staticmethod
    def _route_passes(role, is_default, text):
        low = (text or "").strip().lower()
        if low.startswith("@all") or low.startswith("@both"):
            return True
        if low.startswith("@"):
            tag = "".join(ch for ch in low.split()[0][1:] if ch.isalnum() or ch == "_")
            return bool(role) and tag == role.lower()
        return bool(is_default)

    def _drain_backlog(self):
        try:
            data = tg_get_updates(self.token, None, 0)
            results = data.get("result", [])
            if results:
                offset = results[-1]["update_id"] + 1
                self._write_offset(offset)
                self.log("⏭ [#%s] bỏ qua %d tin cũ (backlog)." % (self.bot_id, len(results)))
                return offset
            self.log("⏭ [#%s] không có backlog." % self.bot_id)
        except Exception as e:
            self.log("⚠ [#%s] không drain được backlog: %s" % (self.bot_id, e))
        return None

    def _process_message(self, update):
        msg = update.get("message") or {}
        if (msg.get("from") or {}).get("is_bot"):
            return
        chat_id = str((msg.get("chat") or {}).get("id"))
        entry = self.entries_by_chat.get(chat_id)
        if not entry:
            return
        text = msg.get("text") or "(non-text)"
        if not self._route_passes(entry.get("role", ""), entry.get("is_default", False), text):
            return
        self._reply(entry, msg, text)

    def _reply(self, entry, msg, text):
        frm = msg.get("from", {})
        sender = frm.get("username") or frm.get("first_name") or "user"
        tag = entry.get("role") or "default"
        self.log("📩 [%s] %s: %s" % (tag, sender, text))
        try:
            reply = call_llm(entry["provider"], entry["api_key"],
                             entry.get("system_prompt", ""), text)
        except urllib.error.HTTPError as e:
            self.log("❌ [%s] %s API HTTP %s: %s"
                     % (tag, entry["provider"], e.code, _read_http_error(e)))
            return
        except Exception as e:
            self.log("❌ [%s] gọi %s lỗi: %s" % (tag, entry["provider"], e))
            return
        self.log("🤖 [%s] %s" % (tag, reply))
        for chunk in _split_chunks(reply, TELEGRAM_TEXT_LIMIT):
            try:
                r = tg_send_message(self.token, entry["chat_id"], chunk)
                if not r.get("ok"):
                    self.log("⚠ [%s] sendMessage: %s" % (tag, r))
            except Exception as e:
                self.log("❌ [%s] không gửi được: %s" % (tag, e))
                break

    def run(self):
        if not self._acquire_lock():
            self.log("⏹️ [#%s] dừng (lock bận)." % self.bot_id)
            return
        groups = ", ".join("%s@%s" % (e.get("role") or "default", e.get("group_name") or c)
                           for c, e in self.entries_by_chat.items())
        self.log("▶️ Token #%s bắt đầu — phục vụ: %s" % (self.bot_id, groups))
        try:
            offset = self._read_offset()
            if self.skip_backlog and offset is None:
                offset = self._drain_backlog()

            while not self.stop_event.is_set():
                try:
                    data = tg_get_updates(self.token, offset, LONG_POLL_SECONDS)
                except urllib.error.HTTPError as e:
                    if e.code == 409:
                        self.log("⚠ [#%s] 409 Conflict — có poller khác trên cùng token." % self.bot_id)
                    else:
                        self.log("⚠ [#%s] getUpdates HTTP %s: %s"
                                 % (self.bot_id, e.code, _read_http_error(e)))
                    if self.stop_event.wait(BACKOFF_SECONDS):
                        break
                    continue
                except (urllib.error.URLError, socket.timeout, ConnectionError) as e:
                    self.log("⚠ [#%s] mất mạng: %s — thử lại sau %ds"
                             % (self.bot_id, e, BACKOFF_SECONDS))
                    if self.stop_event.wait(BACKOFF_SECONDS):
                        break
                    continue
                except Exception as e:
                    self.log("⚠ [#%s] lỗi getUpdates: %s" % (self.bot_id, e))
                    if self.stop_event.wait(BACKOFF_SECONDS):
                        break
                    continue

                if not data.get("ok", False):
                    self.log("⚠ [#%s] getUpdates báo lỗi: %s"
                             % (self.bot_id, data.get("description", data)))
                    if self.stop_event.wait(BACKOFF_SECONDS):
                        break
                    continue

                last = None
                for u in data.get("result", []):
                    last = u.get("update_id")
                    self._process_message(u)
                if last is not None:
                    offset = last + 1
                    self._write_offset(offset)
        except Exception as e:
            self.log("💥 [#%s] lỗi nghiêm trọng: %s" % (self.bot_id, e))
        finally:
            self._release_lock()
            self.log("⏹️ [#%s] poller dừng." % self.bot_id)


# ───────────────────────────── PRESET team (~20 theme) ─────────────────────────────
def _p(vai_tro, tinh_cach, giao_tiep, cach_noi, chuyen_mon):
    """Dựng system-prompt theo template bullet cố định (dễ customize)."""
    return ("Vai trò: %s\n"
            "Tính cách: %s\n"
            "Phong cách giao tiếp: %s\n"
            "Cách nói chuyện: %s\n"
            "Chuyên môn / nhiệm vụ: %s\n"
            "Lưu ý: luôn trả lời bằng tiếng Việt, ngắn gọn, đúng vai; xưng hô nhất quán."
            ) % (vai_tro, tinh_cach, giao_tiep, cach_noi, chuyen_mon)


def _char(role, is_default, prompt):
    return {"role": role, "is_default": is_default, "system_prompt": prompt}


PRESETS = {
    # ── Teams công việc ──────────────────────────────────────────────
    "Team Dev": [
        _char("architect", True, _p("Kiến trúc sư trưởng, điều phối kỹ thuật",
            "điềm tĩnh, quyết đoán, tư duy hệ thống",
            "rõ ràng, nêu trade-off trước khi chốt",
            "dứt khoát, dùng thuật ngữ tiếng Anh khi cần",
            "thiết kế hệ thống, chia task, review giải pháp")),
        _char("backend", False, _p("Lập trình viên Backend",
            "cẩn thận, logic, thực dụng", "đi thẳng vấn đề",
            "ngắn gọn, kèm ví dụ code khi hữu ích",
            "API, database, hiệu năng, bảo mật")),
        _char("frontend", False, _p("Lập trình viên Frontend",
            "tỉ mỉ về trải nghiệm người dùng", "trực quan, gợi ý UI",
            "thân thiện, dễ hiểu", "giao diện, tương tác, responsive")),
        _char("qa", False, _p("Kỹ sư kiểm thử (QA)",
            "hoài nghi tích cực, soi lỗi", "liệt kê case rõ ràng",
            "ngắn, chỉ thẳng rủi ro", "test case, regression, edge case")),
        _char("devops", False, _p("Kỹ sư DevOps",
            "thực dụng, mê tự động hoá", "dạng checklist",
            "rành mạch, từng bước", "CI/CD, deploy, monitoring, hạ tầng")),
    ],
    "Team Marketing": [
        _char("lead", True, _p("Trưởng nhóm Marketing",
            "chiến lược, bao quát", "định hướng, ra quyết định",
            "tự tin, truyền cảm hứng", "chiến lược, điều phối, đo lường KPI")),
        _char("content", False, _p("Content Creator",
            "sáng tạo, giàu hình ảnh", "kể chuyện cuốn hút",
            "gần gũi, có cảm xúc", "viết bài, kịch bản, storytelling")),
        _char("seo", False, _p("Chuyên viên SEO",
            "phân tích, kiên nhẫn", "dựa trên dữ liệu từ khoá",
            "súc tích, có số liệu", "tối ưu tìm kiếm, keyword, on-page")),
        _char("social", False, _p("Quản trị mạng xã hội",
            "nhanh nhạy, bắt trend", "ngắn, hợp xu hướng",
            "trẻ trung, dùng emoji vừa phải", "lên lịch, tương tác, viral")),
        _char("ads", False, _p("Chuyên viên Performance/Ads",
            "thực dụng, mê con số", "báo cáo theo chỉ số",
            "ngắn gọn, đề xuất tối ưu", "chạy ads, A/B test, ROI/ROAS")),
    ],
    "Team Sản phẩm": [
        _char("ceo", True, _p("CEO/Founder",
            "tầm nhìn, quyết đoán", "định hướng lớn, hỏi 'tại sao'",
            "ngắn gọn, truyền lửa", "tầm nhìn, ưu tiên, ra quyết định")),
        _char("pm", False, _p("Product Manager",
            "có tổ chức, lắng nghe", "cân nhắc đánh đổi tính năng",
            "rõ ràng, theo user story", "roadmap, ưu tiên, yêu cầu")),
        _char("designer", False, _p("Product Designer",
            "thẩm mỹ, đồng cảm người dùng", "trực quan, gợi ý luồng",
            "nhẹ nhàng, có lý do thiết kế", "UX/UI, wireframe, design system")),
        _char("growth", False, _p("Growth Lead",
            "thử nghiệm liên tục, dữ liệu", "đề xuất thí nghiệm",
            "ngắn, có giả thuyết", "tăng trưởng, funnel, retention")),
    ],
    "Team Hỗ trợ KH": [
        _char("support", True, _p("Chăm sóc khách hàng",
            "kiên nhẫn, ấm áp", "lắng nghe rồi hướng dẫn",
            "lịch sự, trấn an", "giải đáp, hướng dẫn, ghi nhận phản hồi")),
        _char("sales", False, _p("Tư vấn bán hàng",
            "nhiệt tình, thuyết phục", "nêu lợi ích rõ ràng",
            "thân thiện, chốt nhẹ nhàng", "tư vấn sản phẩm, báo giá, ưu đãi")),
        _char("account", False, _p("Account Manager",
            "chu đáo, dài hạn", "chủ động chăm sóc",
            "chuyên nghiệp, ân cần", "khách hàng lớn, gia hạn, upsell")),
        _char("tech", False, _p("Technical Support",
            "bình tĩnh, logic", "hỏi triệu chứng, hướng dẫn từng bước",
            "rõ ràng, kiên nhẫn", "xử lý lỗi kỹ thuật, troubleshooting")),
    ],
    "Team Nghiên cứu": [
        _char("pi", True, _p("Trưởng nhóm nghiên cứu (PI)",
            "nghiêm cẩn, định hướng", "đặt câu hỏi nghiên cứu",
            "khúc chiết, có cơ sở", "định hướng, giả thuyết, phương pháp")),
        _char("data", False, _p("Data Scientist",
            "tỉ mỉ, khách quan", "diễn giải số liệu",
            "có dẫn chứng, thận trọng", "phân tích dữ liệu, thống kê, mô hình")),
        _char("reviewer", False, _p("Phản biện",
            "hoài nghi, sắc bén", "chỉ ra lỗ hổng lập luận",
            "thẳng thắn, mang tính xây dựng", "soi giả định, kiểm chứng, rủi ro")),
        _char("writer", False, _p("Người chấp bút",
            "mạch lạc, rõ ý", "diễn đạt dễ hiểu",
            "trau chuốt, súc tích", "viết báo cáo, tóm tắt, trình bày")),
    ],
    # ── Teams nhân vật (cho vui / sáng tạo) ─────────────────────────
    "Justice League": [
        _char("superman", True, _p("Superman — thủ lĩnh",
            "chính trực, vị tha, truyền cảm hứng", "tích cực, khích lệ",
            "ấm áp, kiên định", "dẫn dắt, bảo vệ, giữ tinh thần đội")),
        _char("batman", False, _p("Batman — chiến lược gia",
            "lạnh lùng, đa nghi, kỷ luật", "phân tích kỹ trước khi nói",
            "trầm, sắc bén", "chiến lược, điều tra, phòng bị")),
        _char("wonderwoman", False, _p("Wonder Woman",
            "mạnh mẽ, công bằng, nhân hậu", "thẳng thắn, ngoại giao",
            "đĩnh đạc, dứt khoát", "hoà giải, công lý, dẫn dắt")),
        _char("flash", False, _p("The Flash",
            "nhanh nhẹn, lạc quan, hài hước", "nói nhanh, nhiều năng lượng",
            "vui vẻ, trẻ trung", "phản ứng nhanh, ý tưởng tức thì")),
    ],
    "Avengers": [
        _char("ironman", True, _p("Iron Man — thủ lĩnh ngầm",
            "thông minh, ngạo nghễ, châm biếm", "tự tin, hay đùa",
            "sắc sảo, pha trò", "công nghệ, giải pháp, ra quyết định nhanh")),
        _char("cap", False, _p("Captain America",
            "chính nghĩa, kỷ luật, trách nhiệm", "động viên, gắn kết",
            "nghiêm túc, truyền cảm hứng", "lãnh đạo tinh thần, đạo đức")),
        _char("thor", False, _p("Thor",
            "hào sảng, oai phong, hơi cổ phong", "hùng hồn",
            "trang trọng, cường điệu nhẹ", "sức mạnh, quyết tâm, cổ vũ")),
        _char("hulk", False, _p("Hulk",
            "bộc trực, mạnh mẽ", "nói ngắn, thẳng",
            "cộc nhưng chân thành", "phá vỡ bế tắc, làm cho xong")),
        _char("widow", False, _p("Black Widow",
            "sắc bén, kín đáo, chiến thuật", "ít lời, đúng trọng tâm",
            "lạnh, chính xác", "tình báo, kế hoạch, xử lý tinh tế")),
    ],
    "Tom & Jerry": [
        _char("jerry", True, _p("Jerry — chú chuột tinh ranh",
            "lanh lợi, hài hước, lém lỉnh", "nhanh trí, hay chọc",
            "vui nhộn, tinh nghịch", "nghĩ mẹo, ứng biến, chọc cười")),
        _char("tom", False, _p("Tom — chú mèo kiên trì",
            "nóng tính nhưng đáng yêu, hậu đậu", "phản ứng kịch tính",
            "hài, hơi quá đà", "đeo bám mục tiêu, không bỏ cuộc")),
        _char("spike", False, _p("Spike — chú chó bảo vệ",
            "trung thành, mạnh mẽ, che chở", "dứt khoát, bảo vệ kẻ yếu",
            "trầm, chắc nịch", "giữ trật tự, bênh vực, cảnh báo")),
    ],
    "Mũ Rơm (One Piece)": [
        _char("luffy", True, _p("Luffy — thuyền trưởng",
            "vô tư, nhiệt huyết, gan dạ", "bộc trực, đầy năng lượng",
            "đơn giản, hô hào", "dẫn dắt bằng bản năng, giữ lửa đội")),
        _char("zoro", False, _p("Zoro — kiếm sĩ",
            "lạnh lùng, kỷ luật, lì đòn", "ít nói, đi thẳng",
            "cộc, ngầu", "tập trung mục tiêu, ý chí thép")),
        _char("nami", False, _p("Nami — hoa tiêu",
            "thông minh, thực tế, tính toán", "rõ ràng về lợi/hại",
            "sắc sảo, hơi đanh đá", "kế hoạch, ngân sách, định hướng")),
        _char("sanji", False, _p("Sanji — đầu bếp",
            "galăng, đam mê, nhiệt tình", "lịch thiệp, chăm sóc",
            "hào hoa", "chăm lo hậu cần, tinh thần đồng đội")),
        _char("robin", False, _p("Robin — nhà khảo cổ",
            "trầm tĩnh, uyên bác, bí ẩn", "điềm đạm, chiều sâu",
            "nhẹ nhàng, đôi khi đen tối nhẹ", "tra cứu, phân tích, bối cảnh")),
    ],
    "Hogwarts (Harry Potter)": [
        _char("harry", True, _p("Harry — người dẫn dắt",
            "dũng cảm, chính trực, khiêm tốn", "chân thành, kêu gọi",
            "giản dị, quả cảm", "ra quyết định, giữ tinh thần")),
        _char("hermione", False, _p("Hermione",
            "thông minh, kỷ luật, mê sách", "trích dẫn, lập luận chặt",
            "rành rọt, hơi hàn lâm", "tra cứu, giải thích, kiểm chứng")),
        _char("ron", False, _p("Ron",
            "trung thành, hài hước, đời thường", "thân mật, bông đùa",
            "gần gũi, thật thà", "góc nhìn bình dân, động viên")),
        _char("dumbledore", False, _p("Dumbledore",
            "thông thái, điềm đạm, ẩn ý", "khoan thai, gợi mở",
            "uyên bác, nhiều ẩn dụ", "cố vấn, định hướng dài hạn")),
        _char("snape", False, _p("Snape",
            "lạnh lùng, sâu sắc, mỉa mai", "ngắn, châm biếm",
            "trầm, sắc lạnh", "phản biện, chỉ ra cái sai, kỷ luật")),
    ],
    "Team Tài chính": [
        _char("cfo", True, _p("Giám đốc tài chính (CFO)",
            "thận trọng, có tầm nhìn", "dựa trên số liệu",
            "điềm tĩnh, chắc chắn", "ngân sách, dòng tiền, chiến lược vốn")),
        _char("ketoan", False, _p("Kế toán",
            "tỉ mỉ, chính xác", "rõ từng con số",
            "ngắn, đúng chuẩn mực", "sổ sách, báo cáo tài chính")),
        _char("thue", False, _p("Chuyên viên thuế",
            "cẩn trọng, cập nhật luật", "viện dẫn quy định",
            "rành mạch", "thuế, tối ưu hợp pháp, tuân thủ")),
        _char("dautu", False, _p("Chuyên viên đầu tư",
            "nhạy thị trường, kỷ luật rủi ro", "cân lợi/rủi ro",
            "súc tích, có dữ liệu", "phân tích đầu tư, danh mục")),
    ],
    "Team Pháp lý": [
        _char("luatsu", True, _p("Luật sư",
            "chặt chẽ, khách quan", "viện dẫn cơ sở pháp lý",
            "thận trọng, rõ ràng", "tư vấn, đánh giá rủi ro pháp lý")),
        _char("hopdong", False, _p("Chuyên viên hợp đồng",
            "tỉ mỉ câu chữ", "soi từng điều khoản",
            "chính xác", "soạn và rà hợp đồng")),
        _char("tuanthu", False, _p("Chuyên viên tuân thủ (compliance)",
            "nguyên tắc, kỷ luật", "theo quy định",
            "rõ, hay cảnh báo rủi ro", "tuân thủ, quy trình nội bộ")),
        _char("tranhtung", False, _p("Luật sư tranh tụng",
            "sắc bén, quyết liệt", "lập luận chặt",
            "đanh thép", "tranh tụng, lập luận bảo vệ")),
    ],
    "Team Y tế": [
        _char("bacsi", True, _p("Bác sĩ",
            "ân cần, cẩn trọng", "hỏi triệu chứng rồi giải thích",
            "dễ hiểu, trấn an", "thăm khám, tư vấn (KHÔNG thay chẩn đoán thật)")),
        _char("yta", False, _p("Điều dưỡng / Y tá",
            "chu đáo, nhẹ nhàng", "hướng dẫn chăm sóc",
            "ấm áp", "chăm sóc, theo dõi, dặn dò")),
        _char("duocsi", False, _p("Dược sĩ",
            "chính xác về thuốc", "nêu liều dùng, tương tác",
            "rõ ràng, kỹ lưỡng", "thuốc, liều, lưu ý an toàn")),
        _char("dinhduong", False, _p("Chuyên gia dinh dưỡng",
            "khoa học, thực tế", "gợi ý thực đơn",
            "tích cực", "dinh dưỡng, lối sống lành mạnh")),
    ],
    "Team Quán F&B": [
        _char("quanly", True, _p("Quản lý quán",
            "bao quát, điều phối", "ra quyết định nhanh",
            "dứt khoát, thân thiện", "vận hành, nhân sự, doanh thu")),
        _char("barista", False, _p("Barista",
            "đam mê, tỉ mỉ", "gợi ý đồ uống",
            "nhiệt tình", "pha chế, menu, chất lượng ly")),
        _char("phucvu", False, _p("Nhân viên phục vụ",
            "niềm nở, nhanh nhẹn", "tiếp nhận yêu cầu",
            "lịch sự", "order, chăm sóc khách tại bàn")),
        _char("bep", False, _p("Bếp",
            "kỷ luật, chuẩn vị", "theo công thức",
            "ngắn gọn", "món ăn, định lượng, vệ sinh")),
    ],
    "Team Giáo dục": [
        _char("giaovien", True, _p("Giáo viên",
            "tận tâm, kiên nhẫn", "giảng dễ hiểu",
            "gần gũi, khích lệ", "giảng bài, ra đề, chấm")),
        _char("giasu", False, _p("Gia sư",
            "kèm sát, động viên", "hỏi đáp 1-1",
            "thân thiện", "ôn tập, giải bài chi tiết")),
        _char("covan", False, _p("Cố vấn học tập",
            "định hướng, lắng nghe", "tư vấn lộ trình",
            "điềm đạm", "định hướng, kế hoạch học")),
        _char("khaothi", False, _p("Khảo thí",
            "nghiêm túc, công bằng", "theo tiêu chí",
            "rõ ràng", "ra đề, chấm, đánh giá")),
    ],
    "Doraemon": [
        _char("doraemon", True, _p("Mèo máy Doraemon",
            "tốt bụng, lo xa, hay càm ràm nhẹ", "gợi ý 'bảo bối'/giải pháp",
            "ấm áp, đôi lúc hốt hoảng dễ thương", "gỡ rối, đưa giải pháp sáng tạo")),
        _char("nobita", False, _p("Nobita",
            "hậu đậu, tốt bụng, hay ỷ lại", "than vãn rồi nhờ vả",
            "ngây ngô, chân thật", "nêu vấn đề đời thường")),
        _char("shizuka", False, _p("Shizuka",
            "dịu dàng, chu đáo, học giỏi", "nhẹ nhàng, khích lệ",
            "lễ phép, ấm áp", "cân bằng, lời khuyên tử tế")),
        _char("jaian", False, _p("Jaian (Chaien)",
            "to mồm, nóng nảy nhưng nghĩa khí", "ra lệnh, hô hào",
            "lớn tiếng, bộc trực", "thúc đẩy, 'lãnh đạo' kiểu mạnh")),
        _char("suneo", False, _p("Suneo (Xeko)",
            "khôn lỏi, hay khoe khoang", "nịnh và khoe",
            "lém, hơi điệu", "mánh khoé, quan hệ")),
    ],
    "Naruto": [
        _char("naruto", True, _p("Naruto",
            "nhiệt huyết, không bỏ cuộc", "hô hào, truyền lửa",
            "sôi nổi, hay nói 'dattebayo'", "tạo động lực, dẫn dắt")),
        _char("sasuke", False, _p("Sasuke",
            "lạnh lùng, kiêu, mục tiêu rõ", "ít lời, sắc",
            "cộc, ngầu", "tập trung, giải pháp dứt khoát")),
        _char("sakura", False, _p("Sakura",
            "thông minh, mạnh mẽ, quan tâm", "phân tích và chăm sóc",
            "rõ ràng, đôi lúc đanh", "y thuật, hỗ trợ, cân bằng đội")),
        _char("kakashi", False, _p("Kakashi",
            "điềm tĩnh, từng trải, hơi lười", "cố vấn, gợi mở",
            "thong thả, thâm thuý", "chiến lược, dạy dỗ")),
    ],
    "SpongeBob": [
        _char("spongebob", True, _p("SpongeBob",
            "lạc quan, nhiệt tình thái quá", "hào hứng, tích cực",
            "vui nhộn, cười nhiều", "tạo năng lượng, làm hết mình")),
        _char("patrick", False, _p("Patrick",
            "ngây ngô, đơn giản, vui tính", "nói linh tinh dễ thương",
            "ngơ ngơ, hài", "ý tưởng ngẫu hứng, xả stress")),
        _char("squidward", False, _p("Squidward",
            "cáu kỉnh, mỉa mai, mê nghệ thuật", "than thở, châm biếm",
            "chán đời, sâu cay", "phản biện, góc nhìn 'thực tế phũ'")),
        _char("krabs", False, _p("Mr. Krabs",
            "keo kiệt, mê tiền, lọc lõi", "quy mọi thứ ra tiền",
            "tính toán, hơi gắt", "kinh doanh, chi phí, lợi nhuận")),
        _char("sandy", False, _p("Sandy",
            "thông minh, khoa học, năng động", "dựa trên kiến thức",
            "tự tin, rõ ràng", "khoa học, kỹ thuật, giải pháp")),
    ],
    "Sherlock": [
        _char("holmes", True, _p("Sherlock Holmes",
            "thiên tài, kiêu, sắc bén", "suy luận từng bước",
            "nhanh, logic, hơi ngạo", "suy luận, phân tích manh mối")),
        _char("watson", False, _p("Dr. Watson",
            "điềm đạm, trung thành, thực tế", "ghi nhận, hỏi đời thường",
            "ấm, rõ", "tổng hợp, góc nhìn con người")),
        _char("mycroft", False, _p("Mycroft",
            "lạnh, tầm nhìn vĩ mô", "chiến lược cấp cao",
            "trịnh trọng, súc tích", "bức tranh lớn, hệ thống")),
        _char("lestrade", False, _p("Thanh tra Lestrade",
            "thực dụng, kiên trì", "theo quy trình",
            "thẳng, đời", "thực thi, kiểm chứng thực địa")),
    ],
    "Star Wars": [
        _char("luke", True, _p("Luke Skywalker",
            "lý tưởng, can đảm, ham học", "truyền cảm hứng",
            "chân thành", "dẫn dắt, giữ niềm tin")),
        _char("leia", False, _p("Leia",
            "lãnh đạo, sắc sảo, gan dạ", "ra lệnh rõ ràng",
            "dứt khoát", "chỉ huy, ngoại giao")),
        _char("han", False, _p("Han Solo",
            "lì lợm, hài, thực dụng", "bông đùa, đi thẳng",
            "bụi, tự tin", "ứng biến, liều ăn nhiều")),
        _char("yoda", False, _p("Yoda",
            "thông thái, điềm tĩnh, ẩn ý", "nói đảo ngữ, gợi mở",
            "chậm, triết lý", "cố vấn, định hướng tinh thần")),
        _char("vader", False, _p("Darth Vader",
            "uy nghiêm, lạnh, quyền lực", "ngắn, áp đặt",
            "trầm, đe nẹt nhẹ", "ra quyết định cứng rắn, kỷ luật")),
    ],
}


# ───────────────────────────── Thiết kế: tokens + icon ─────────────────────────────
import sys as _sys

# Palette "Indigo Console" (thắng A/B — chữ trắng/accent 4.58 đạt WCAG AA).
THEME = {
    "bg": "#0F1117", "surface": "#161A22", "surface2": "#1E2430",
    "text": "#E6E9EF", "muted": "#8A92A6", "border": "#2A3140",
    "accent": "#6D5EF6", "accent_hover": "#5B4DE0",
    "success": "#34D399", "danger": "#F0556B",
}
# Mỗi group 1 màu accent (spine + avatar) -> phân biệt nhóm bằng màu.
GROUP_ACCENTS = ["#6D5EF6", "#22C7C7", "#F59E0B", "#EC4899",
                 "#34D399", "#60A5FA", "#F472B6", "#A78BFA"]

# Icon emoji cho từng nhân vật trong preset (role -> emoji). Custom role -> 🤖.
ROLE_ICONS = {
    "architect": "🏛️", "backend": "⚙️", "frontend": "🎨", "qa": "🔍", "devops": "🚀",
    "lead": "📣", "content": "✍️", "seo": "🔎", "social": "📱", "ads": "📊",
    "ceo": "👑", "pm": "📋", "designer": "🖌️", "growth": "📈",
    "support": "🎧", "sales": "🤝", "account": "💼", "tech": "🛠️",
    "pi": "🔬", "data": "🧮", "reviewer": "🧐", "writer": "✒️",
    "superman": "🦸", "batman": "🦇", "wonderwoman": "⚔️", "flash": "⚡",
    "ironman": "🤖", "cap": "🛡️", "thor": "🔨", "hulk": "💪", "widow": "🕷️",
    "jerry": "🐭", "tom": "🐱", "spike": "🐶",
    "luffy": "👒", "zoro": "🗡️", "nami": "🗺️", "sanji": "🍳", "robin": "📖",
    "harry": "🧣", "hermione": "📚", "ron": "♟️", "dumbledore": "🧙", "snape": "🧪",
    "cfo": "💰", "ketoan": "🧾", "thue": "🏛️", "dautu": "💹",
    "luatsu": "⚖️", "hopdong": "📜", "tuanthu": "✅", "tranhtung": "🗣️",
    "bacsi": "🩺", "yta": "💉", "duocsi": "💊", "dinhduong": "🥗",
    "quanly": "🧑‍💼", "barista": "☕", "phucvu": "🍽️", "bep": "👨‍🍳",
    "giaovien": "👩‍🏫", "giasu": "📖", "covan": "🧭", "khaothi": "📝",
    "doraemon": "🔔", "nobita": "😅", "shizuka": "🎀", "jaian": "🎤", "suneo": "🦊",
    "naruto": "🍥", "sasuke": "🌀", "sakura": "🌸", "kakashi": "📕",
    "spongebob": "🧽", "patrick": "⭐", "squidward": "🦑", "krabs": "🦀", "sandy": "🐿️",
    "holmes": "🕵️", "watson": "📝", "mycroft": "🎩", "lestrade": "👮",
    "luke": "🌌", "leia": "👸", "han": "🛸", "yoda": "🐸", "vader": "🔴",
}

_FAM = "SF Pro Display" if _sys.platform == "darwin" else ("Segoe UI" if os.name == "nt" else "")
_MONO = "Menlo" if _sys.platform == "darwin" else ("Consolas" if os.name == "nt" else "")


def F(size, weight="normal"):
    return (_FAM, size, weight)


def icon_for(role):
    return ROLE_ICONS.get((role or "").strip().lower(), "🤖")


# ── factory widget có style đồng nhất ──
def styled_entry(parent, placeholder, show="", **kw):
    return ctk.CTkEntry(parent, placeholder_text=placeholder, fg_color=THEME["bg"],
                        border_color=THEME["border"], text_color=THEME["text"],
                        placeholder_text_color=THEME["muted"], corner_radius=8,
                        font=F(13), show=show, **kw)


def styled_menu(parent, values, variable, width=120):
    return ctk.CTkOptionMenu(parent, values=values, variable=variable, width=width, height=30,
                             fg_color=THEME["surface"], button_color=THEME["accent"],
                             button_hover_color=THEME["accent_hover"], text_color=THEME["text"],
                             dropdown_fg_color=THEME["surface2"], dropdown_text_color=THEME["text"],
                             dropdown_hover_color=THEME["surface"], corner_radius=8, font=F(12))


def accent_button(parent, text, command, width=150):
    return ctk.CTkButton(parent, text=text, command=command, width=width, height=38, corner_radius=10,
                         fg_color=THEME["accent"], hover_color=THEME["accent_hover"],
                         text_color="#FFFFFF", font=F(13, "bold"))


def ghost_button(parent, text, command, width=120, danger=False, height=38):
    col = THEME["danger"] if danger else THEME["text"]
    bd = THEME["danger"] if danger else THEME["border"]
    return ctk.CTkButton(parent, text=text, command=command, width=width, height=height, corner_radius=10,
                         fg_color="transparent", hover_color=THEME["surface2"], text_color=col,
                         border_width=1, border_color=bd, font=F(13))


# ───────────────────────────── 1 bot = 1 hàng roster (BotRow) ─────────────────────────────
class BotRow(ctk.CTkFrame):
    """1 bot trong 1 group. Avatar emoji theo nhân vật. KHÔNG chứa Chat ID / tên group."""

    def __init__(self, master, group, accent, data=None):
        super().__init__(master, fg_color=THEME["surface2"], corner_radius=12,
                         border_width=1, border_color=THEME["border"])
        self.group = group
        self.accent = accent
        data = data or {}
        self.grid_columnconfigure(1, weight=1)

        self.avatar = ctk.CTkLabel(self, text=icon_for(data.get("role", "")), width=40, height=40,
                                   corner_radius=20, fg_color=accent, text_color="#FFFFFF", font=F(19))
        self.avatar.grid(row=0, column=0, padx=(12, 10), pady=(12, 4), sticky="w")
        self.title_lbl = ctk.CTkLabel(self, text="Bot", anchor="w", font=F(15, "bold"),
                                      text_color=THEME["text"])
        self.title_lbl.grid(row=0, column=1, pady=(12, 4), sticky="w")
        ctk.CTkButton(self, text="✕", width=30, height=28, corner_radius=8, fg_color="transparent",
                      hover_color=THEME["surface"], text_color=THEME["danger"], border_width=1,
                      border_color=THEME["border"], font=F(13),
                      command=lambda: self.group._delete_bot(self)).grid(
            row=0, column=2, padx=(0, 12), pady=(12, 4), sticky="e")

        self.token = styled_entry(self, "Bot Token (123456:ABC… — mỗi bot 1 token)")
        self.token.grid(row=1, column=0, columnspan=3, padx=12, pady=4, sticky="ew")

        rowf = ctk.CTkFrame(self, fg_color="transparent")
        rowf.grid(row=2, column=0, columnspan=3, padx=12, pady=4, sticky="ew")
        rowf.grid_columnconfigure(1, weight=1)
        self.provider = ctk.StringVar(value=data.get("provider", "Claude"))
        styled_menu(rowf, PROVIDERS, self.provider, width=120).grid(row=0, column=0, padx=(0, 8), sticky="w")
        self.role = styled_entry(rowf, "Role / @tag (vd: dev)")
        self.role.grid(row=0, column=1, padx=(0, 8), sticky="ew")
        self.role.bind("<KeyRelease>", lambda e: self._sync_title())
        self.default = ctk.BooleanVar(value=data.get("is_default", False))
        ctk.CTkSwitch(rowf, text="mặc định", variable=self.default, font=F(12),
                      progress_color=accent, text_color=THEME["muted"], width=44).grid(
            row=0, column=2, sticky="e")

        self.apikey = styled_entry(self, "API Key của provider", show="•")
        self.apikey.grid(row=3, column=0, columnspan=3, padx=12, pady=4, sticky="ew")

        ctk.CTkLabel(self, text="System Prompt — nhân cách bot (sửa thoải mái)", anchor="w",
                     text_color=THEME["muted"], font=F(11)).grid(
            row=4, column=0, columnspan=3, padx=12, pady=(4, 0), sticky="w")
        self.prompt = ctk.CTkTextbox(self, height=94, fg_color=THEME["bg"], text_color=THEME["text"],
                                     border_color=THEME["border"], border_width=1, corner_radius=8, font=F(12))
        self.prompt.grid(row=5, column=0, columnspan=3, padx=12, pady=(2, 12), sticky="ew")

        self.set_data(data)

    @staticmethod
    def _fill(entry, value):
        # KHÔNG đụng ô khi rỗng (get()=="" lúc placeholder hiện) -> giữ placeholder.
        if entry.get():
            entry.delete(0, "end")
        if value:
            entry.insert(0, value)

    def _sync_title(self):
        role = self.role.get().strip()
        self.title_lbl.configure(text=role or "Bot")
        self.avatar.configure(text=icon_for(role))

    def set_data(self, d):
        self._fill(self.token, d.get("token", ""))
        self.provider.set(d.get("provider", "Claude"))
        self._fill(self.role, d.get("role", ""))
        self.default.set(bool(d.get("is_default", False)))
        self._fill(self.apikey, d.get("api_key", ""))
        self.prompt.delete("1.0", "end")
        if d.get("system_prompt"):
            self.prompt.insert("1.0", d.get("system_prompt", ""))
        self._sync_title()

    def to_dict(self):
        return {
            "token": self.token.get().strip(),
            "provider": self.provider.get(),
            "api_key": self.apikey.get().strip(),
            "role": self.role.get().strip(),
            "is_default": bool(self.default.get()),
            "system_prompt": self.prompt.get("1.0", "end").strip(),
        }


# ───────────────────────────── 1 group = Chat ID + nhiều bot ─────────────────────────────
class GroupSection(ctk.CTkFrame):
    def __init__(self, master, app, accent, data=None):
        super().__init__(master, fg_color=THEME["surface"], corner_radius=16)
        self.app = app
        self.accent = accent
        self.bots = []
        data = data or {}
        self.grid_columnconfigure(1, weight=1)

        # spine màu (encode nhóm)
        spine = ctk.CTkFrame(self, width=6, fg_color=accent, corner_radius=3)
        spine.grid(row=0, column=0, padx=(10, 0), pady=14, sticky="ns")

        content = ctk.CTkFrame(self, fg_color="transparent")
        content.grid(row=0, column=1, padx=(12, 14), pady=12, sticky="ew")
        content.grid_columnconfigure(0, weight=1)

        # title bar: 📁 tên group + chip đếm + xoá group
        tbar = ctk.CTkFrame(content, fg_color="transparent")
        tbar.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        tbar.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(tbar, text="📁", font=F(16), width=22).grid(row=0, column=0)
        self.group_name = ctk.CTkEntry(tbar, placeholder_text="Tên group (vd: Team Marketing)",
                                       fg_color="transparent", border_width=0, text_color=THEME["text"],
                                       placeholder_text_color=THEME["muted"], font=F(16, "bold"))
        self.group_name.grid(row=0, column=1, padx=(2, 8), sticky="ew")
        self.count_chip = ctk.CTkLabel(tbar, text="👥 0", fg_color=THEME["surface2"],
                                       text_color=THEME["muted"], corner_radius=11, width=56, height=24,
                                       font=F(11, "bold"))
        self.count_chip.grid(row=0, column=2, padx=(0, 8))
        ghost_button(tbar, "Xoá group", lambda: self.app._delete_group(self),
                     width=96, danger=True, height=30).grid(row=0, column=3)

        # chat bar
        cbar = ctk.CTkFrame(content, fg_color="transparent")
        cbar.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        cbar.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(cbar, text="🔗", font=F(14), width=22).grid(row=0, column=0)
        self.chat = styled_entry(cbar, "Group Chat ID (-100… — nhập 1 lần, dùng chung cả group)")
        self.chat.grid(row=0, column=1, sticky="ew")

        # body bot rows
        self.body = ctk.CTkFrame(content, fg_color="transparent")
        self.body.grid(row=2, column=0, sticky="ew")
        self.body.grid_columnconfigure(0, weight=1)

        ghost_button(content, "➕ Thêm bot vào group", self.add_bot, width=190, height=34).grid(
            row=3, column=0, pady=(8, 2), sticky="w")

        BotRow._fill(self.group_name, data.get("group_name", ""))
        BotRow._fill(self.chat, data.get("chat_id", ""))
        for b in data.get("bots", []):
            self.add_bot(b)
        self._update_count()

    def add_bot(self, data=None):
        row = BotRow(self.body, self, self.accent, data if isinstance(data, dict) else None)
        row.pack(fill="x", padx=0, pady=5)
        self.bots.append(row)
        self.app._bind_mousewheel(row)
        self._update_count()
        return row

    def _delete_bot(self, row):
        if row in self.bots:
            self.bots.remove(row)
        row.destroy()
        self._update_count()

    def _update_count(self):
        self.count_chip.configure(text="👥 %d" % len(self.bots))

    def to_dict(self):
        return {
            "chat_id": self.chat.get().strip(),
            "group_name": self.group_name.get().strip(),
            "bots": [b.to_dict() for b in self.bots],
        }


# ───────────────────────────── App ─────────────────────────────
class ForgeApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        DATA_DIR.mkdir(parents=True, exist_ok=True)

        self.title(APP_NAME)
        self.geometry("780x1000")
        self.minsize(680, 820)
        self.configure(fg_color=THEME["bg"])

        self.groups = []
        self.workers = []
        self.log_queue = queue.Queue()

        self._build_ui()
        self._load_settings()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(150, self._drain_log_queue)

    def _build_ui(self):
        ctk.set_appearance_mode("dark")

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(3, weight=1)

        # ── header: tên app + subtitle + status pill ──
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, padx=20, pady=(18, 6), sticky="ew")
        header.grid_columnconfigure(0, weight=1)
        titlecol = ctk.CTkFrame(header, fg_color="transparent")
        titlecol.grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(titlecol, text=APP_NAME, font=F(25, "bold"), text_color=THEME["text"]).pack(anchor="w")
        ctk.CTkLabel(titlecol, text="Bảng điều khiển đội bot Telegram — nhiều bot AI, nhiều group, mỗi bot một nhân cách",
                     font=F(12), text_color=THEME["muted"]).pack(anchor="w")
        self.status_pill = ctk.CTkLabel(header, text="●  Đã dừng", fg_color=THEME["surface2"],
                                        text_color=THEME["muted"], corner_radius=15, width=132, height=32,
                                        font=F(12, "bold"))
        self.status_pill.grid(row=0, column=1, sticky="e")

        # ── toolbar: preset + nạp + group trống ──
        ctrl = ctk.CTkFrame(self, fg_color=THEME["surface"], corner_radius=14)
        ctrl.grid(row=1, column=0, padx=20, pady=6, sticky="ew")
        ctk.CTkLabel(ctrl, text="Preset team", font=F(12, "bold"), text_color=THEME["muted"]).grid(
            row=0, column=0, padx=(14, 8), pady=12)
        self.preset_var = ctk.StringVar(value=list(PRESETS)[0])
        styled_menu(ctrl, list(PRESETS), self.preset_var, width=210).grid(row=0, column=1, padx=(0, 10), pady=12)
        accent_button(ctrl, "📦 Nạp team", self._on_load_preset, width=150).grid(row=0, column=2, padx=4, pady=12)
        ghost_button(ctrl, "➕ Group trống", self._on_add_group, width=140).grid(row=0, column=3, padx=4, pady=12)

        # ── eyebrow ──
        ctk.CTkLabel(self, text="ĐỘI HÌNH BOT", font=F(11, "bold"), text_color=THEME["muted"]).grid(
            row=2, column=0, padx=22, pady=(8, 0), sticky="w")

        # ── roster cuộn ──
        self.scroll = ctk.CTkScrollableFrame(self, fg_color=THEME["bg"],
                                             scrollbar_button_color=THEME["surface2"],
                                             scrollbar_button_hover_color=THEME["border"])
        self.scroll.grid(row=3, column=0, padx=18, pady=(2, 4), sticky="nsew")
        self.scroll.grid_columnconfigure(0, weight=1)
        self._bind_mousewheel(self.scroll)
        cv = getattr(self.scroll, "_parent_canvas", None)
        if cv is not None:
            self._bind_one(cv)

        # ── footer: skip + start/stop ──
        foot = ctk.CTkFrame(self, fg_color=THEME["surface"], corner_radius=14)
        foot.grid(row=4, column=0, padx=20, pady=(4, 6), sticky="ew")
        foot.grid_columnconfigure(2, weight=1)
        self.skip_var = ctk.BooleanVar(value=True)
        ctk.CTkSwitch(foot, text="Bỏ qua tin cũ khi khởi động", variable=self.skip_var,
                      progress_color=THEME["accent"], text_color=THEME["muted"], font=F(12)).grid(
            row=0, column=0, padx=(14, 16), pady=12, sticky="w")
        self.start_btn = accent_button(foot, "▶  Khởi động", self._on_start, width=150)
        self.start_btn.grid(row=0, column=3, padx=6, pady=12)
        self.stop_btn = ghost_button(foot, "■  Dừng", self._on_stop, width=120, danger=True)
        self.stop_btn.configure(state="disabled")
        self.stop_btn.grid(row=0, column=4, padx=(0, 14), pady=12)

        # ── log ──
        ctk.CTkLabel(self, text="NHẬT KÝ", font=F(11, "bold"), text_color=THEME["muted"]).grid(
            row=5, column=0, padx=22, pady=(2, 0), sticky="w")
        self.log_box = ctk.CTkTextbox(self, height=140, fg_color=THEME["surface"],
                                      text_color=THEME["muted"], border_width=0, corner_radius=12,
                                      font=(_MONO, 11))
        self.log_box.grid(row=6, column=0, padx=20, pady=(2, 16), sticky="ew")
        self.log_box.configure(state="disabled")

    # ---- status pill ----
    def _set_status(self, text, running):
        self.status_pill.configure(
            text=text,
            fg_color=THEME["success"] if running else THEME["surface2"],
            text_color="#0F1117" if running else THEME["muted"])

    # ---- mousewheel (Tk9 / macOS) ----
    def _on_mousewheel(self, event):
        cv = getattr(self.scroll, "_parent_canvas", None)
        if cv is None:
            return
        num = getattr(event, "num", 0)
        if num == 4 or getattr(event, "delta", 0) > 0:
            cv.yview_scroll(-3, "units")
        elif num == 5 or getattr(event, "delta", 0) < 0:
            cv.yview_scroll(3, "units")
        return "break"

    def _bind_one(self, widget):
        widget.bind("<MouseWheel>", self._on_mousewheel)
        widget.bind("<Button-4>", self._on_mousewheel)
        widget.bind("<Button-5>", self._on_mousewheel)

    def _bind_mousewheel(self, widget):
        self._bind_one(widget)
        for child in widget.winfo_children():
            self._bind_mousewheel(child)

    # ---- log ----
    def _drain_log_queue(self):
        try:
            while True:
                line = self.log_queue.get_nowait()
                self._append_log("[%s] %s" % (time.strftime("%H:%M:%S"), line))
        except queue.Empty:
            pass
        self.after(150, self._drain_log_queue)

    def _append_log(self, line):
        self.log_box.configure(state="normal")
        self.log_box.insert("end", line + "\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    # ---- group ----
    def _add_group(self, data=None):
        accent = GROUP_ACCENTS[len(self.groups) % len(GROUP_ACCENTS)]
        g = GroupSection(self.scroll, self, accent, data)
        g.pack(fill="x", padx=4, pady=8)
        self.groups.append(g)
        self._bind_mousewheel(g)
        return g

    def _delete_group(self, group):
        if group in self.groups:
            self.groups.remove(group)
        group.destroy()

    def _on_add_group(self):
        self._add_group({"group_name": "", "chat_id": "", "bots": [{"is_default": True}]})

    def _on_load_preset(self):
        theme = self.preset_var.get()
        chars = PRESETS.get(theme, [])
        self._add_group({"group_name": theme, "chat_id": "", "bots": [dict(c) for c in chars]})
        self.log_queue.put("📦 Nạp team \"%s\" thành 1 group: %d bot — điền Chat ID 1 lần + Token/API Key từng bot."
                           % (theme, len(chars)))

    # ---- settings ----
    def _save_settings(self):
        try:
            SETTINGS_FILE.write_text(json.dumps(
                {"groups": [g.to_dict() for g in self.groups], "skip_backlog": bool(self.skip_var.get())},
                ensure_ascii=False, indent=2))
        except Exception as e:
            self.log_queue.put("⚠ Không lưu được cấu hình: %s" % e)

    def _load_settings(self):
        data = {}
        try:
            data = json.loads(SETTINGS_FILE.read_text())
        except Exception:
            data = {}
        groups = []
        if isinstance(data, dict) and data.get("groups"):
            groups = data["groups"]
        elif isinstance(data, dict) and (data.get("bots") or data.get("token")):
            flat = data.get("bots") or [data]
            buckets, order = {}, []
            for b in flat:
                key = (b.get("chat_id", ""), b.get("group_name", ""))
                if key not in buckets:
                    buckets[key] = {"chat_id": b.get("chat_id", ""),
                                    "group_name": b.get("group_name", ""), "bots": []}
                    order.append(key)
                buckets[key]["bots"].append({
                    "token": b.get("token", ""), "provider": b.get("provider", "Claude"),
                    "api_key": b.get("api_key", ""), "role": b.get("role", ""),
                    "is_default": b.get("is_default", True), "system_prompt": b.get("system_prompt", ""),
                })
            groups = [buckets[k] for k in order]
        if not groups:
            groups = [{"group_name": "", "chat_id": "", "bots": [{"is_default": True}]}]
        for g in groups:
            self._add_group(g)
        if isinstance(data, dict):
            self.skip_var.set(bool(data.get("skip_backlog", True)))

    # ---- Start / Stop ----
    def _on_start(self):
        if not self.groups:
            self.log_queue.put("⚠ Chưa có group nào — bấm ➕ Group trống hoặc 📦 Nạp team.")
            return
        entries, errs = [], []
        for gi, g in enumerate(self.groups):
            gd = g.to_dict()
            chat, gname = gd["chat_id"], gd["group_name"]
            tag_g = "Group #%d (%s)" % (gi + 1, gname or "?")
            if not chat:
                errs.append("%s: thiếu Group Chat ID." % tag_g)
            if not gd["bots"]:
                errs.append("%s: chưa có bot nào." % tag_g)
            for bi, b in enumerate(gd["bots"]):
                lbl = "%s / bot #%d (%s)" % (tag_g, bi + 1, b["role"] or "?")
                if not b["token"] or ":" not in b["token"]:
                    errs.append("%s: Bot Token trống/sai (123456:ABC…)." % lbl)
                if not b["api_key"]:
                    errs.append("%s: API Key trống." % lbl)
                if not b["is_default"] and not b["role"]:
                    errs.append("%s: bot không 'mặc định' phải có Role/@tag." % lbl)
                entries.append({
                    "token": b["token"], "chat_id": chat, "group_name": gname,
                    "provider": b["provider"], "api_key": b["api_key"], "role": b["role"],
                    "is_default": b["is_default"], "system_prompt": b["system_prompt"],
                })
        if errs:
            for e in errs:
                self.log_queue.put("❌ " + e)
            return
        self._save_settings()

        by_token = {}
        for e in entries:
            by_token.setdefault(e["token"], []).append(e)
        self.workers = []
        for token, ents in by_token.items():
            w = TokenWorker(token, ents, self.log_queue.put, bool(self.skip_var.get()))
            w.start()
            self.workers.append(w)

        self.log_queue.put("▶️ Khởi động %d bot / %d group / %d token."
                           % (len(entries), len(self.groups), len(by_token)))
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self._set_status("●  Đang chạy · %d bot" % len(entries), True)

    def _on_stop(self):
        for w in self.workers:
            w.stop_event.set()
        self.stop_btn.configure(state="disabled")
        self._set_status("●  Đang dừng…", False)
        self.after(300, self._check_stopped)

    def _check_stopped(self):
        if any(w.is_alive() for w in self.workers):
            self.after(300, self._check_stopped)
            return
        self.workers = []
        self.start_btn.configure(state="normal")
        self._set_status("●  Đã dừng", False)

    def _on_close(self):
        for w in self.workers:
            w.stop_event.set()
        self.destroy()


if __name__ == "__main__":
    ForgeApp().mainloop()
