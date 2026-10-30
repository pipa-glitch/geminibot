import os
import sys
import io
import re
import html
import json
import copy
import time
import shutil
import urllib.parse
import subprocess
import asyncio
import threading
import aiohttp
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from PIL import Image
import streamlit as st

TMP_PKGS = "/tmp/pypkgs"
os.makedirs(TMP_PKGS, exist_ok=True)
if TMP_PKGS not in sys.path:
    sys.path.insert(0, TMP_PKGS)

try:
    from gemini_webapi import GeminiClient
    from gemini_webapi.constants import AvailableModel
except ImportError:
    subprocess.check_call([sys.executable, "-m", "pip", "install", "--target", TMP_PKGS, "-U", "gemini-webapi"])
    from gemini_webapi import GeminiClient
    from gemini_webapi.constants import AvailableModel

try:
    from gemini_webapi.utils import rotate_1psidts
except ImportError:
    rotate_1psidts = None

# Патч свойства AvailableModel.model_header, чтобы библиотека не сбрасывала заголовки моделей и уровень рассуждений!
def _patched_model_header(self) -> dict[str, str]:
    if not getattr(self, "model_hash", None) or getattr(self, "tier_id", None) is None:
        return {}
    r_slot = getattr(self, "_reasoning_slot", 1)
    return {
        "x-goog-ext-525001261-jspb": (
            f'[1,null,null,null,"{self.model_hash}",null,null,0,'
            f'[4,5,6,8],null,null,{r_slot},null,null,{self.tier_id}]'
        ),
        "x-goog-ext-73010989-jspb": "[0]",
        "x-goog-ext-73010990-jspb": "[0,0,0]",
    }

AvailableModel.model_header = property(_patched_model_header)

HAS_PPTX = False
try:
    import pptx
    from pptx import Presentation
    from pptx.util import Inches, Pt
    from pptx.dml.color import RGBColor
    HAS_PPTX = True
except ImportError:
    try:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "--target", TMP_PKGS, "python-pptx"])
        import pptx
        from pptx import Presentation
        from pptx.util import Inches, Pt
        from pptx.dml.color import RGBColor
        HAS_PPTX = True
    except Exception:
        pass

try:
    from curl_cffi import CurlHttpVersion
    from curl_cffi.requests import AsyncSession

    if not getattr(AsyncSession, "_http2_patched", False):
        _orig_init = AsyncSession.__init__

        def _http2_init(self, *args, **kwargs):
            kwargs["http_version"] = CurlHttpVersion.V2_0
            _orig_init(self, *args, **kwargs)

        AsyncSession.__init__ = _http2_init
        AsyncSession._http2_patched = True
except Exception:
    pass

from loguru import logger
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart, Command
from aiogram.utils.keyboard import ReplyKeyboardBuilder, InlineKeyboardBuilder
from google import genai
from google.genai import types as gtypes

def get_secret(key_name, default_val=""):
    try:
        return st.secrets.get(key_name, default_val)
    except Exception:
        return default_val

TELEGRAM_TOKEN = get_secret("TELEGRAM_TOKEN", "9099534534:AAExrwposBIWvrkBZKbN32FMfUl0ecaVFrk")
SECURE_1PSID = get_secret("SECURE_1PSID", "")
SECURE_1PSIDTS = get_secret("SECURE_1PSIDTS", "")
GEMINI_KEYS = list(get_secret("GEMINI_KEYS", []))

OWNER_USERNAME = "targetturtle"
ACCESS_FILE = "access_db.json"

# Полный реестр моделей (display_name, model_name, model_hash, tier_id)
FULL_MODELS_CATALOG = {
    "flash-lite": ("3.5 Flash-Lite", "gemini-flash-lite", "cf41b0e0dd7d53e5", 6),
    "flash": ("3.8 Flash", "gemini-flash", "56fdd199312815e2", 11),
    "pro": ("3.1 Pro", "gemini-pro", "e6fa609c3fa255c0", 4),
    "argon": ("4 Argon", "gemini-argon", "e6fa609c3fa255c0", 4),
}

def load_access_db():
    if os.path.exists(ACCESS_FILE):
        try:
            with open(ACCESS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                data.setdefault("admins", [])
                data.setdefault("admin_usernames", [OWNER_USERNAME])
                data.setdefault("preapproved_usernames", {})
                data.setdefault("open_mode", False)
                data.setdefault("quota_short", {})
                data.setdefault("quota_weekly", {})
                return data
        except Exception:
            pass
    return {
        "admin_id": None,
        "admins": [],
        "admin_usernames": [OWNER_USERNAME],
        "users": {},
        "preapproved_usernames": {},
        "open_mode": False,
        "custom_psid": "",
        "custom_psidts": "",
        "user_cids": {},
        "quota_short": {},
        "quota_weekly": {},
    }

def save_access_db(db):
    try:
        with open(ACCESS_FILE, "w", encoding="utf-8") as f:
            json.dump(db, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

access_db = load_access_db()
abuse_status_store = "Не проверено"
last_quota_fetch_ok = False
stats_store = {"web_ok": 0, "fallback_ok": 0, "user_reqs": {}}
admin_input_state = {}

def parse_utc_reset_to_msk(line: str, is_weekly: bool = False) -> str:
    tz_msk = timezone(timedelta(hours=3))
    m_dt = re.search(r"(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2}:\d{2})\s*UTC", line)
    if m_dt:
        try:
            dt_utc = datetime.strptime(f"{m_dt.group(1)} {m_dt.group(2)}", "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            dt_msk = dt_utc.astimezone(tz_msk)
            months_ru = ["", "янв.", "фев.", "мар.", "апр.", "мая", "июн.", "июл.", "авг.", "сент.", "окт.", "нояб.", "дек."]
            if is_weekly or (dt_msk.date() != datetime.now(tz_msk).date()):
                return f"{dt_msk.day} {months_ru[dt_msk.month]} в {dt_msk.strftime('%H:%M')}"
            return dt_msk.strftime("%H:%M")
        except Exception:
            pass
    return ""

def _loguru_quota_sink(message):
    global abuse_status_store, last_quota_fetch_ok
    text = str(message).strip()
    if "Account quota updated:" in text:
        last_quota_fetch_ok = True
        info = text.split("Account quota updated:", 1)[1].strip()
        m_frac = re.search(r"(\d+)\s*/\s*(\d+)", info)
        if m_frac:
            rem = int(m_frac.group(1))
            tot = int(m_frac.group(2))
            if tot > 0:
                used_pct = max(0, min(100, round((1 - (rem / tot)) * 100)))
                if tot <= 10000:
                    access_db["quota_short"] = {
                        "rem": rem,
                        "tot": tot,
                        "pct": used_pct,
                        "reset": parse_utc_reset_to_msk(info, is_weekly=False),
                    }
                else:
                    access_db["quota_weekly"] = {
                        "rem": rem,
                        "tot": tot,
                        "pct": used_pct,
                        "reset": parse_utc_reset_to_msk(info, is_weekly=True),
                    }
                save_access_db(access_db)
    elif "Account abuse status:" in text:
        raw_ab = text.split("Account abuse status:", 1)[1].strip()
        abuse_status_store = "✅ Чистый (Clean)" if "Clean" in raw_ab else raw_ab

if not getattr(sys, "_quota_sink_v20", False):
    logger.add(_loguru_quota_sink)
    sys._quota_sink_v20 = True

def inject_missing_models_into_client(client):
    """Восстанавливает 3.8 Flash, 3.1 Pro и 4 Argon в client._models, которые gemini_webapi выкидывает из-за [None-11] и [None-4]"""
    try:
        models_dict = getattr(client, "_models", None)
        if isinstance(models_dict, dict):
            for key, (d_name, m_name, m_hash, t_id) in FULL_MODELS_CATALOG.items():
                if m_name not in models_dict:
                    m_obj = AvailableModel(
                        display_name=d_name,
                        model_name=m_name,
                        model_hash=m_hash,
                        tier_id=t_id,
                    )
                    m_obj._reasoning_slot = 1
                    models_dict[m_name] = m_obj
                else:
                    existing = models_dict[m_name]
                    if not getattr(existing, "model_hash", None):
                        existing.model_hash = m_hash
                    if getattr(existing, "tier_id", None) is None:
                        existing.tier_id = t_id
    except Exception as e:
        print(f"[!] Ошибка инжекта моделей: {e}")

def make_progress_bar(pct: int, length: int = 18) -> str:
    filled = round((pct / 100) * length)
    if pct > 0 and filled == 0:
        filled = 1
    filled = min(length, max(0, filled))
    return "█" * filled + "░" * (length - filled)

def format_limits_like_web() -> str:
    qs = access_db.get("quota_short", {})
    qw = access_db.get("quota_weekly", {})

    if not qs.get("tot") and not qw.get("tot"):
        return "⏳ <b>Данные о квоте обновляются (нажми Обновить).</b>"

    lines = ["📊 <b>Лимиты на использование</b> <code>PRO</code>\n"]

    if qs.get("tot"):
        cur_pct = qs.get("pct", 0)
        bar_cur = make_progress_bar(cur_pct)
        reset_s = f"\n<i>Лимит сбросится в {qs['reset']} (МСК)</i>" if qs.get("reset") else ""
        lines.append(
            f"<b>Текущее использование</b> — <b>Использовано {cur_pct} %</b> ({qs['rem']}/{qs['tot']})\n"
            f"<code>{bar_cur}</code>{reset_s}\n"
        )

    if qw.get("tot"):
        week_pct = qw.get("pct", 0)
        bar_week = make_progress_bar(week_pct)
        reset_w = f"\n<i>Лимит сбросится {qw['reset']} (МСК)</i>" if qw.get("reset") else ""
        lines.append(
            f"<b>Недельный лимит</b> — <b>Использовано {week_pct} %</b> ({qw['rem']}/{qw['tot']})\n"
            f"<code>{bar_week}</code>{reset_w}"
        )

    return "\n".join(lines)

def markdown_to_telegram_html(text: str) -> str:
    code_blocks = []

    def _save_code_block(match):
        code = html.escape(match.group(2).strip())
        code_blocks.append(f"<pre><code>{code}</code></pre>")
        return f"@@CODEBLOCK_{len(code_blocks) - 1}@@"

    text = re.sub(r"```([a-zA-Z0-9_-]*)\n?(.*?)```", _save_code_block, text, flags=re.DOTALL)

    inline_codes = []

    def _save_inline_code(match):
        code = html.escape(match.group(1))
        inline_codes.append(f"<code>{code}</code>")
        return f"@@INLINECODE_{len(inline_codes) - 1}@@"

    text = re.sub(r"`([^`\n]+)`", _save_inline_code, text)

    text = html.escape(text)
    text = re.sub(r"(?m)^\s*#{1,6}\s+(.+?)\s*$", r"<b>\1</b>", text)
    text = re.sub(r"(?m)^\s*[\*\-]\s+", "• ", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text, flags=re.DOTALL)
    text = re.sub(r"\*([^\*\n]+?)\*", r"<i>\1</i>", text)
    text = text.replace("**", "")

    for idx, c_html in enumerate(inline_codes):
        text = text.replace(f"@@INLINECODE_{idx}@@", c_html)
    for idx, b_html in enumerate(code_blocks):
        text = text.replace(f"@@CODEBLOCK_{idx}@@", b_html)

    return text

def sync_user_identity(user: types.User):
    if not user:
        return
    uid = user.id
    uname = (user.username or "").lower().lstrip("@")
    changed = False

    admin_unames = [u.lower().lstrip("@") for u in access_db.get("admin_usernames", [OWNER_USERNAME])]
    if uname and (uname == OWNER_USERNAME or uname in admin_unames):
        if access_db.get("admin_id") is None or uname == OWNER_USERNAME:
            access_db["admin_id"] = uid
            changed = True
        if uid not in access_db.setdefault("admins", []):
            access_db["admins"].append(uid)
            changed = True

    pre = access_db.get("preapproved_usernames", {})
    if uname and uname in pre:
        exp_val = pre.pop(uname)
        access_db.setdefault("users", {})[str(uid)] = {
            "name": f"{user.full_name} (@{user.username})",
            "expires_at": exp_val,
        }
        changed = True

    if changed:
        save_access_db(access_db)

def is_admin(uid: int, username: str = None) -> bool:
    if username and username.lower().lstrip("@") in [u.lower().lstrip("@") for u in access_db.get("admin_usernames", [OWNER_USERNAME])]:
        return True
    if not uid:
        return False
    if access_db.get("admin_id") and int(access_db["admin_id"]) == int(uid):
        return True
    return int(uid) in [int(x) for x in access_db.get("admins", [])]

def check_access(user: types.User) -> bool:
    sync_user_identity(user)
    if is_admin(user.id, user.username):
        return True
    if access_db.get("open_mode", False):
        return True
    u_info = access_db.get("users", {}).get(str(user.id))
    if not u_info:
        return False
    exp = u_info.get("expires_at", 0)
    if exp == 0 or time.time() < exp:
        return True
    access_db["users"].pop(str(user.id), None)
    save_access_db(access_db)
    return False

def register_chat_cid(uid: int, *objs):
    cids = access_db.setdefault("user_cids", {}).setdefault(str(uid), [])
    changed = False
    for obj in objs:
        if obj is None:
            continue
        cid = getattr(obj, "cid", None)
        if not cid and getattr(obj, "metadata", None):
            meta = getattr(obj, "metadata", None)
            if isinstance(meta, (list, tuple)) and len(meta) > 0:
                cid = meta[0]
        if cid and isinstance(cid, str) and cid not in cids:
            cids.append(cid)
            changed = True
    if changed:
        save_access_db(access_db)

MODELS_LIST = [
    ("3.5 Flash-Lite", "flash-lite"),
    ("3.8 Flash", "flash"),
    ("3.1 Pro", "pro"),
    ("4 Argon", "argon"),
]

REASONING_LIST = [
    ("Низкий", "low"),
    ("Средний", "medium"),
    ("Высокий", "high"),
]

TOOLS_LIST = [
    ("Без инструмента", "none"),
    ("🍌 Создание изображений", "image"),
    ("🎬 Создание видео", "video"),
    ("🎵 Создание музыки", "music"),
    ("📝 Canvas", "canvas"),
    ("🔬 Deep Research", "deep_research"),
    ("📖 Обучение", "learning"),
]

web_client = None
web_init_lock = asyncio.Lock()
web_chats = {}
user_history = {}
user_prefs = {}
working_api_key = None

def get_prefs(uid: int):
    if uid not in user_prefs:
        user_prefs[uid] = {
            "model": "flash",
            "model_label": "3.8 Flash",
            "reasoning": "low",
            "tool": "none",
        }
    return user_prefs[uid]

def get_main_reply_kb(uid: int = 0, username: str = None):
    kb = ReplyKeyboardBuilder()
    kb.button(text="Модель")
    kb.button(text="Уровень рассуждений")
    kb.button(text="Инструменты")
    kb.button(text="Новый чат")
    if is_admin(uid, username):
        kb.button(text="👑 Админка")
        kb.adjust(2, 2, 1)
    else:
        kb.adjust(2, 2)
    return kb.as_markup(resize_keyboard=True)

def get_locked_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="📩 Запросить доступ", callback_data="req_access")
    return kb.as_markup()

def get_active_cookies():
    psid = access_db.get("custom_psid") or SECURE_1PSID
    psidts = access_db.get("custom_psidts") or SECURE_1PSIDTS
    return psid.strip() if psid else "", psidts.strip() if psidts else None

def save_refreshed_cookies(client):
    try:
        cookies_dict = getattr(client, "cookies", None) or {}
        new_ts = cookies_dict.get("__Secure-1PSIDTS")
        new_psid = cookies_dict.get("__Secure-1PSID")
        changed = False
        if new_ts and new_ts != access_db.get("custom_psidts"):
            access_db["custom_psidts"] = new_ts
            changed = True
        if new_psid and new_psid != access_db.get("custom_psid"):
            access_db["custom_psid"] = new_psid
            changed = True
        if changed:
            save_access_db(access_db)
    except Exception:
        pass

async def auto_rotate_cookie_ts(psid: str, old_psidts: str = None):
    if not rotate_1psidts or not psid:
        return None
    for payload in ([{"__Secure-1PSID": psid, "__Secure-1PSIDTS": old_psidts}, {"__Secure-1PSID": psid}] if old_psidts else [{"__Secure-1PSID": psid}]):
        try:
            res = rotate_1psidts(payload)
            new_ts = (await res) if asyncio.iscoroutine(res) else res
            if new_ts and isinstance(new_ts, str):
                access_db["custom_psidts"] = new_ts
                save_access_db(access_db)
                return new_ts
        except Exception:
            continue
    return None

async def get_web_client(force_reinit: bool = False):
    global web_client, last_quota_fetch_ok
    psid, psidts = get_active_cookies()
    if not psid:
        raise Exception("В Secrets не указан SECURE_1PSID")
    async with web_init_lock:
        if force_reinit or web_client is None:
            last_quota_fetch_ok = False
            try:
                client = GeminiClient(psid, psidts)
                await client.init(timeout=30, auto_refresh=True, refresh_interval=300)
                inject_missing_models_into_client(client)
                save_refreshed_cookies(client)
                web_client = client
            except Exception:
                web_client = None

            if not last_quota_fetch_ok or web_client is None:
                rotated_ts = await auto_rotate_cookie_ts(psid, psidts)
                for candidate_ts in ([rotated_ts, None] if rotated_ts else [None]):
                    try:
                        last_quota_fetch_ok = False
                        client = GeminiClient(psid, candidate_ts)
                        await client.init(timeout=30, auto_refresh=True, refresh_interval=300)
                        inject_missing_models_into_client(client)
                        save_refreshed_cookies(client)
                        web_client = client
                        if last_quota_fetch_ok:
                            break
                    except Exception:
                        continue
    if web_client is not None:
        inject_missing_models_into_client(web_client)
    return web_client

async def delete_old_web_chat(uid: int):
    old_chat = web_chats.pop(uid, None)
    if old_chat is not None:
        register_chat_cid(uid, old_chat)

    saved_cids = list(access_db.get("user_cids", {}).get(str(uid), []))
    if not saved_cids:
        return

    try:
        client = await get_web_client()
        if hasattr(client, "delete_chat"):
            for cid in saved_cids:
                try:
                    await client.delete_chat(cid)
                except Exception:
                    pass
        access_db.setdefault("user_cids", {})[str(uid)] = []
        save_access_db(access_db)
    except Exception:
        pass

def resolve_model_clean(client, model_code: str, reasoning_code: str = "low"):
    """Создаёт реальный экземпляр AvailableModel с нужным model_hash, tier_id и _reasoning_slot (индекс 11)"""
    inject_missing_models_into_client(client)
    code = model_code.lower()
    d_name, m_name, m_hash, t_id = FULL_MODELS_CATALOG.get(code, FULL_MODELS_CATALOG["flash"])
    r_slot = 1 if reasoning_code == "low" else 2

    m_obj = AvailableModel(
        display_name=d_name,
        model_name=m_name,
        model_hash=m_hash,
        tier_id=t_id,
    )
    m_obj._reasoning_slot = r_slot
    return m_obj

@asynccontextmanager
async def continuous_typing(bot_inst: Bot, chat_id: int, action: str = "typing"):
    stop_event = asyncio.Event()

    async def _loop():
        while not stop_event.is_set():
            try:
                await bot_inst.send_chat_action(chat_id=chat_id, action=action)
            except Exception:
                pass
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=4.0)
            except asyncio.TimeoutError:
                pass

    task = asyncio.create_task(_loop())
    try:
        yield
    finally:
        stop_event.set()
        task.cancel()

def build_canvas_pptx(slides_data: list) -> bytes:
    if not HAS_PPTX:
        return b""
    prs = Presentation()
    prs.slide_width = Inches(13.333)
    prs.slide_height = Inches(7.5)
    blank_layout = prs.slide_layouts[6]

    for idx, s in enumerate(slides_data):
        slide = prs.slides.add_slide(blank_layout)
        bg = slide.background
        fill = bg.fill
        fill.solid()
        fill.fore_color.rgb = RGBColor(13, 25, 43)

        tag_text = str(s.get("tag", f"СЛАЙД {idx + 1}")).upper()
        title_text = str(s.get("title", f"Слайд {idx + 1}"))
        subtitle_text = str(s.get("subtitle", ""))
        points = s.get("points", [])

        tb_tag = slide.shapes.add_textbox(Inches(1.0), Inches(1.0), Inches(11.3), Inches(0.5))
        p_tag = tb_tag.text_frame.paragraphs[0]
        p_tag.text = tag_text
        p_tag.font.size = Pt(11)
        p_tag.font.bold = True
        p_tag.font.color.rgb = RGBColor(56, 189, 248)

        tb_title = slide.shapes.add_textbox(Inches(1.0), Inches(1.5), Inches(11.3), Inches(1.6))
        tf_title = tb_title.text_frame
        tf_title.word_wrap = True
        p_title = tf_title.paragraphs[0]
        p_title.text = title_text
        p_title.font.size = Pt(34 if idx == 0 else 28)
        p_title.font.bold = True
        p_title.font.color.rgb = RGBColor(255, 255, 255)

        tb_body = slide.shapes.add_textbox(Inches(1.0), Inches(3.2), Inches(11.3), Inches(3.6))
        tf_body = tb_body.text_frame
        tf_body.word_wrap = True

        first_p = True
        if subtitle_text:
            p = tf_body.paragraphs[0]
            first_p = False
            p.text = subtitle_text
            p.font.size = Pt(18)
            p.font.color.rgb = RGBColor(148, 163, 184)
            p.space_after = Pt(14)

        for pt in points:
            p = tf_body.paragraphs[0] if first_p else tf_body.add_paragraph()
            first_p = False
            p.text = f"• {pt}"
            p.font.size = Pt(18)
            p.font.color.rgb = RGBColor(226, 232, 240)
            p.space_after = Pt(10)

    out_io = io.BytesIO()
    prs.save(out_io)
    return out_io.getvalue()

def build_canvas_html(slides_data: list) -> bytes:
    slides_json = json.dumps(slides_data, ensure_ascii=False)
    html_code = f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Презентация Canvas</title>
<style>
  body {{ margin:0; background:#090d16; color:#fff; font-family:system-ui,-apple-system,sans-serif; display:flex; flex-direction:column; align-items:center; justify-content:center; min-height:100vh; }}
  .nav {{ display:flex; align-items:center; gap:16px; background:#1e293b; padding:8px 20px; border-radius:99px; margin-bottom:20px; }}
  .nav button {{ background:none; border:none; color:#fff; font-size:18px; cursor:pointer; padding:4px 10px; }}
  .card {{ width:90%; max-width:900px; min-height:460px; background:linear-gradient(135deg,#0d1b2a,#1b263b); border-radius:20px; padding:48px; box-sizing:border-box; box-shadow:0 20px 50px rgba(0,0,0,0.6); display:flex; flex-direction:column; justify-content:center; }}
  .tag {{ color:#38bdf8; font-size:13px; font-weight:700; letter-spacing:1.5px; text-transform:uppercase; margin-bottom:14px; }}
  .title {{ font-size:36px; font-weight:800; margin:0 0 18px 0; line-height:1.2; font-family:Georgia,serif; }}
  .sub {{ color:#94a3b8; font-size:18px; margin-bottom:22px; line-height:1.5; }}
  .line {{ width:50px; height:4px; background:#38bdf8; margin-bottom:24px; border-radius:2px; }}
  ul {{ margin:0; padding-left:22px; color:#e2e8f0; font-size:18px; line-height:1.7; }}
  li {{ margin-bottom:10px; }}
</style>
</head>
<body>
  <div class="nav">
    <button onclick="prev()">❮</button>
    <span id="counter">1 / 1</span>
    <button onclick="next()">❯</button>
  </div>
  <div class="card">
    <div class="tag" id="s_tag"></div>
    <h1 class="title" id="s_title"></h1>
    <div class="sub" id="s_sub"></div>
    <div class="line"></div>
    <ul id="s_list"></ul>
  </div>
<script>
const slides = {slides_json};
let cur = 0;
function render() {{
  const s = slides[cur] || {{}};
  document.getElementById('counter').innerText = (cur + 1) + ' of ' + slides.length;
  document.getElementById('s_tag').innerText = s.tag || ('СЛАЙД ' + (cur + 1));
  document.getElementById('s_title').innerText = s.title || '';
  document.getElementById('s_sub').innerText = s.subtitle || '';
  const ul = document.getElementById('s_list');
  ul.innerHTML = '';
  (s.points || []).forEach(p => {{
    const li = document.createElement('li');
    li.innerText = p;
    ul.appendChild(li);
  }});
}}
function prev() {{ if (cur > 0) {{ cur--; render(); }} }}
function next() {{ if (cur < slides.length - 1) {{ cur++; render(); }} }}
document.addEventListener('keydown', e => {{ if (e.key === 'ArrowRight') next(); if (e.key === 'ArrowLeft') prev(); }});
render();
</script>
</body>
</html>"""
    return html_code.encode("utf-8")

async def safe_send(message: types.Message, text: str):
    if not text:
        return
    uid = message.from_user.id
    uname = message.from_user.username

    text = re.sub(r"https?://googleusercontent\.com/\S+", "", text)
    text = re.sub(r"(?:^|\s)_[0-9]+\b", "", text).strip()
    if not text:
        return

    if "SEND_FILE:" in text:
        after_tag = text.split("SEND_FILE:", 1)[1].strip()
        lines = after_tag.split("\n", 1)
        filename = lines[0].strip() or "file.py"
        file_content = lines[1] if len(lines) > 1 else ""
        file_content = re.sub(r"^```[a-zA-Z0-9_-]*\n", "", file_content.strip())
        file_content = re.sub(r"\n```$", "", file_content.strip())
        doc = types.BufferedInputFile(file_content.encode("utf-8"), filename=filename)
        await message.answer_document(doc, reply_markup=get_main_reply_kb(uid, uname))
        return

    max_len = 3800
    for i in range(0, len(text), max_len):
        raw_part = text[i:i + max_len]
        html_part = markdown_to_telegram_html(raw_part)
        try:
            await message.answer(html_part, parse_mode="HTML", reply_markup=get_main_reply_kb(uid, uname))
        except Exception:
            clean_plain = re.sub(r"<[^>]+>", "", html.unescape(html_part))
            await message.answer(clean_plain, parse_mode=None, reply_markup=get_main_reply_kb(uid, uname))

def is_presentation_request(prompt: str, resp_text: str) -> bool:
    p_low = prompt.lower()
    r_low = (resp_text or "").lower()
    keywords = ["презентац", "повер поинт", "павер поинт", "powerpoint", "pptx", "слайд"]
    if any(k in p_low for k in keywords):
        return True
    if ("презентац" in r_low or "presentation" in r_low) and ("готов" in r_low or "посмотреть" in r_low or "i will now generate" in r_low):
        return True
    return False

def is_canvas_stub(resp_text: str) -> bool:
    if not resp_text:
        return False
    r_low = resp_text.lower()
    stubs = ["i will now generate", "вы можете посмотреть её", "вы можете посмотреть его", "готова! вы можете", "готов! вы можете"]
    return len(resp_text) < 600 and any(s in r_low for s in stubs)

def is_image_request(prompt: str, tool: str) -> bool:
    if tool == "image":
        return True
    p_low = prompt.lower()
    return any(w in p_low for w in ["сгенерируй изображен", "сгенерируй картинк", "нарисуй", "создай картинк", "создай изображен", "кинь картинк"])

async def send_fallback_image(message: types.Message, prompt_ru: str):
    uid = message.from_user.id
    uname = message.from_user.username
    prompt_en = prompt_ru
    for key in GEMINI_KEYS:
        if not key:
            continue
        try:
            g_client = genai.Client(api_key=key.strip())
            tr = await asyncio.to_thread(
                g_client.models.generate_content,
                model="gemini-3.5-flash-lite",
                contents=f"Translate this image generation prompt into detailed English (output ONLY the English prompt, nothing else): {prompt_ru}",
            )
            if tr and tr.text:
                prompt_en = tr.text.strip()
            break
        except Exception:
            continue

    encoded = urllib.parse.quote(prompt_en[:400])
    img_url = f"https://image.pollinations.ai/prompt/{encoded}?width=1024&height=1024&nologo=true&model=flux"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/131.0.0.0 Safari/537.36"}
    try:
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(img_url, timeout=40) as resp:
                if resp.status == 200:
                    data = await resp.read()
                    photo_file = types.BufferedInputFile(data, filename="generated.jpg")
                    await message.answer_photo(photo_file, reply_markup=get_main_reply_kb(uid, uname))
                    return True
    except Exception:
        pass
    try:
        await message.answer_photo(img_url, reply_markup=get_main_reply_kb(uid, uname))
        return True
    except Exception:
        return False

async def ask_gemini_web(bot_inst: Bot, message: types.Message, prompt: str, file_path: str = None):
    uid = message.from_user.id
    uname = message.from_user.username
    prefs = get_prefs(uid)
    want_img = is_image_request(prompt, prefs["tool"])

    # Если просят картинку, а выбрана 3.5 Flash-Lite (в которой нет генератора картинок) — переключаем запрос на 3.8 Flash!
    effective_model_code = "flash" if (want_img and prefs["model"] == "flash-lite") else prefs["model"]

    files = [file_path] if file_path else None
    client = await get_web_client()
    target_model = resolve_model_clean(client, effective_model_code, prefs["reasoning"])

    if uid not in web_chats:
        web_chats[uid] = client.start_chat(model=target_model)
    else:
        web_chats[uid].model = target_model

    chat = web_chats[uid]
    response = await asyncio.wait_for(chat.send_message(prompt, files=files), timeout=90)

    save_refreshed_cookies(client)
    register_chat_cid(uid, chat, response)

    resp_text = getattr(response, "text", "") or ""

    stats_store["web_ok"] += 1
    stats_store["user_reqs"][str(uid)] = stats_store["user_reqs"].get(str(uid), 0) + 1

    sent_any_image = False
    if getattr(response, "images", None):
        for idx, img in enumerate(response.images):
            try:
                out_name = f"img_{uid}_{idx}.png"
                out_path = os.path.join("/tmp", out_name)
                await img.save(path="/tmp", filename=out_name)
                if os.path.exists(out_path):
                    with open(out_path, "rb") as f:
                        photo_file = types.BufferedInputFile(f.read(), filename=out_name)
                        await message.answer_photo(photo_file, reply_markup=get_main_reply_kb(uid, uname))
                    os.remove(out_path)
                    sent_any_image = True
                elif getattr(img, "url", None):
                    await message.answer_photo(img.url, reply_markup=get_main_reply_kb(uid, uname))
                    sent_any_image = True
            except Exception:
                if getattr(img, "url", None):
                    try:
                        await message.answer_photo(img.url, reply_markup=get_main_reply_kb(uid, uname))
                        sent_any_image = True
                    except Exception:
                        pass

    if want_img and not sent_any_image and not getattr(response, "videos", None):
        if await send_fallback_image(message, prompt):
            return

    if getattr(response, "videos", None):
        vid_dir = f"/tmp/vid_{uid}_{int(time.time())}"
        os.makedirs(vid_dir, exist_ok=True)
        try:
            for vid in response.videos:
                await vid.save(path=vid_dir, verbose=True)
            for fname in sorted(os.listdir(vid_dir)):
                fpath = os.path.join(vid_dir, fname)
                if os.path.isfile(fpath):
                    with open(fpath, "rb") as f:
                        vid_doc = types.BufferedInputFile(f.read(), filename=fname if fname.endswith(".mp4") else f"{fname}.mp4")
                        await message.answer_video(vid_doc, caption="🎬 <b>Видео готово!</b>", parse_mode="HTML", reply_markup=get_main_reply_kb(uid, uname))
        except Exception as e:
            print(f"[!] Ошибка сохранения видео: {e}")
        finally:
            shutil.rmtree(vid_dir, ignore_errors=True)

    if getattr(response, "media", None):
        med_dir = f"/tmp/med_{uid}_{int(time.time())}"
        os.makedirs(med_dir, exist_ok=True)
        try:
            for med in response.media:
                await med.save(path=med_dir, verbose=True)
            for fname in sorted(os.listdir(med_dir)):
                fpath = os.path.join(med_dir, fname)
                if os.path.isfile(fpath):
                    with open(fpath, "rb") as f:
                        raw_b = f.read()
                    if fname.lower().endswith((".mp3", ".wav", ".ogg", ".m4a")):
                        aud_doc = types.BufferedInputFile(raw_b, filename=fname)
                        await message.answer_audio(aud_doc, caption="🎵 <b>Музыкальный трек готов!</b>", parse_mode="HTML", reply_markup=get_main_reply_kb(uid, uname))
                    elif fname.lower().endswith(".mp4"):
                        vid_doc = types.BufferedInputFile(raw_b, filename=fname)
                        await message.answer_video(vid_doc, caption="🎬 <b>Видео-обложка трека</b>", parse_mode="HTML", reply_markup=get_main_reply_kb(uid, uname))
                    else:
                        doc_f = types.BufferedInputFile(raw_b, filename=fname)
                        await message.answer_document(doc_f, reply_markup=get_main_reply_kb(uid, uname))
        except Exception as e:
            print(f"[!] Ошибка сохранения аудио: {e}")
        finally:
            shutil.rmtree(med_dir, ignore_errors=True)

    if is_presentation_request(prompt, resp_text):
        try:
            extract_prompt = (
                "Не используй Canvas. Выведи все слайды этой презентации СТРОГО в формате чистого JSON-массива "
                "(без кавычек ```json и без лишнего текста до и после): "
                '[{"tag": "ТЕМА РАЗДЕЛА", "title": "Заголовок слайда", "subtitle": "Подзаголовок или краткая суть", "points": ["Пункт 1", "Пункт 2", "Пункт 3"]}]'
            )
            slide_resp = await asyncio.wait_for(chat.send_message(extract_prompt), timeout=90)
            raw_json = (slide_resp.text or "").strip()
            m_json = re.search(r"\[.*\]", raw_json, flags=re.DOTALL)
            if m_json:
                slides_data = json.loads(m_json.group(0))
                if isinstance(slides_data, list) and len(slides_data) > 0:
                    pptx_bytes = build_canvas_pptx(slides_data)
                    html_bytes = build_canvas_html(slides_data)

                    if pptx_bytes:
                        pptx_doc = types.BufferedInputFile(pptx_bytes, filename="Presentation.pptx")
                        await message.answer_document(
                            pptx_doc,
                            caption="📊 <b>Презентация PowerPoint (.pptx) готова!</b>",
                            parse_mode="HTML",
                            reply_markup=get_main_reply_kb(uid, uname)
                        )
                    html_doc = types.BufferedInputFile(html_bytes, filename="Presentation_Canvas.html")
                    await message.answer_document(
                        html_doc,
                        caption="🌐 <b>Интерактивная версия (листается в браузере как в Canvas)</b>",
                        parse_mode="HTML",
                        reply_markup=get_main_reply_kb(uid, uname)
                    )
                    return
        except Exception as e:
            print(f"[!] Ошибка сборки презентации: {e}")

    elif is_canvas_stub(resp_text) and not getattr(response, "videos", None) and not getattr(response, "media", None):
        try:
            extract_doc = await asyncio.wait_for(
                chat.send_message("Не открывай Canvas. Выведи полное содержимое только что созданного документа/кода обычным текстом прямо сюда в сообщение."),
                timeout=90
            )
            if getattr(extract_doc, "text", None):
                resp_text = extract_doc.text
        except Exception:
            pass

    if resp_text:
        await safe_send(message, resp_text)

async def ask_gemini_fallback(user_id: int, new_content, history_text: str):
    global working_api_key
    tz_msk = timezone(timedelta(hours=3))
    current_time = datetime.now(tz_msk).strftime("%d.%m.%Y, %H:%M")
    prefs = get_prefs(user_id)

    api_models = ["gemini-3.8-flash", "gemini-3.5-flash-lite"]
    keys_to_try = ([working_api_key] + [k for k in GEMINI_KEYS if k != working_api_key]) if working_api_key else GEMINI_KEYS

    last_err = None
    for model_name in api_models:
        for key in keys_to_try:
            if not key:
                continue
            try:
                client = genai.Client(api_key=key.strip())
                response = await asyncio.to_thread(
                    client.models.generate_content,
                    model=model_name,
                    contents=[new_content],
                    config=gtypes.GenerateContentConfig(
                        system_instruction=(
                            f"Сейчас (МСК): {current_time}. Твоя модель: Gemini {prefs['model_label']}. "
                            "Отвечай чётко по сути запроса пользователя. Пиши математику без $ и без LaTeX (x², √, /). "
                            "Если просят скинуть файлом — начни первую строку с SEND_FILE:имя_файла.расширение"
                        )
                    ),
                )
                working_api_key = key
                stats_store["fallback_ok"] += 1
                stats_store["user_reqs"][str(user_id)] = stats_store["user_reqs"].get(str(user_id), 0) + 1
                ans = response.text or "Пустой ответ."
                return ans, model_name
            except Exception as e:
                last_err = e
                continue
    raise Exception(f"Запасные ключи не сработали: {last_err}")

def build_admin_panel_view():
    users = access_db.get("users", {})
    open_mode = access_db.get("open_mode", False)
    mode_str = "🔓 ОТКРЫТ ДЛЯ ВСЕХ" if open_mode else "🔒 ЗАКРЫТЫЙ (По заявкам)"

    kb = InlineKeyboardBuilder()
    kb.button(text="📊 Лимиты и Куки", callback_data="check_limits")
    kb.button(text="🔍 Модели на аккаунте", callback_data="adm_inspect_models")
    kb.button(text="➕ Дать доступ по @юзу", callback_data="adm_add_user")
    kb.button(text="👑 Выдать / Снять Админку", callback_data="adm_manage_admins")
    kb.button(text=f"Режим: {mode_str}", callback_data="adm_toggle_mode")
    kb.button(text="📢 Рассылка всем", callback_data="adm_broadcast")
    kb.button(text="🍪 Обновить Куки", callback_data="adm_set_cookie")
    kb.button(text="🔄 Переподключить Веб", callback_data="reconnect_web")
    kb.button(text="🗑 Очистить чаты на сайте", callback_data="wipe_all_chats")

    lines = [
        "👑 <b>Панель управления (@targetturtle)</b>",
        f"⚙️ Режим входа: <b>{mode_str}</b>\n",
    ]

    now = time.time()
    active_count = 0
    for uid_str, info in list(users.items()):
        exp = info.get("expires_at", 0)
        if exp != 0 and now >= exp:
            users.pop(uid_str, None)
            continue
        active_count += 1
        reqs = stats_store["user_reqs"].get(uid_str, 0)
        rem = "♾ Навсегда" if exp == 0 else f"⏳ {int((exp - now) // 60)} мин."
        u_name = html.escape(info.get("name", uid_str))
        is_u_adm = "👑 " if int(uid_str) in [int(x) for x in access_db.get("admins", [])] else ""
        lines.append(f"• {is_u_adm}<b>{u_name}</b> ({rem} | Запросов: {reqs})")
        kb.button(text=f"❌ Забрать у {info.get('name', uid_str)[:18]}", callback_data=f"revoke:{uid_str}")

    pre = access_db.get("preapproved_usernames", {})
    if pre:
        lines.append("\n⏳ <b>Ожидают первого входа по @юзу:</b> " + ", ".join([f"@{k}" for k in pre.keys()]))

    if active_count == 0 and not pre:
        lines.append("Сейчас активных клиентов нет.")

    save_access_db(access_db)
    kb.adjust(2, 2, 2, 2, 1)
    return "\n".join(lines), kb.as_markup()

def create_dispatcher(bot_inst: Bot):
    d = Dispatcher()

    @d.message(CommandStart())
    async def start_cmd(message: types.Message):
        uid = message.from_user.id
        uname = message.from_user.username
        if not check_access(message.from_user):
            await message.answer(
                "🔒 <b>Доступ к этому боту закрыт.</b>\n\n"
                "Нажми кнопку ниже, чтобы отправить заявку владельцу:",
                parse_mode="HTML",
                reply_markup=get_locked_kb()
            )
            return

        prefs = get_prefs(uid)
        r_label = dict((v, k) for k, v in REASONING_LIST).get(prefs["reasoning"], prefs["reasoning"])
        await message.answer(
            f"Модель: <b>{prefs['model_label']}</b>\n"
            f"Уровень рассуждений: <b>{r_label}</b>",
            parse_mode="HTML",
            reply_markup=get_main_reply_kb(uid, uname)
        )

    @d.callback_query(F.data == "req_access")
    async def cb_req_access(call: types.CallbackQuery):
        uid = call.from_user.id
        if check_access(call.from_user):
            await call.answer("У тебя уже есть доступ!")
            return

        admin_targets = list(set(([access_db["admin_id"]] if access_db.get("admin_id") else []) + access_db.get("admins", [])))
        if not admin_targets:
            await call.answer("Ожидай входа владельца @targetturtle.", show_alert=True)
            return

        u_name = html.escape(call.from_user.full_name or "Без имени")
        u_nick = f"@{call.from_user.username}" if call.from_user.username else "без юзернейма"

        kb = InlineKeyboardBuilder()
        kb.button(text="⏱ На 1 час (Урок)", callback_data=f"grant:{uid}:3600")
        kb.button(text="📅 На 1 день (24ч)", callback_data=f"grant:{uid}:86400")
        kb.button(text="♾ Навсегда", callback_data=f"grant:{uid}:0")
        kb.button(text="👑 Сделать Админом", callback_data=f"makeadm:{uid}")
        kb.button(text="❌ Отказать", callback_data=f"deny:{uid}")
        kb.adjust(2, 1, 1, 1)

        for adm_id in admin_targets:
            try:
                await bot_inst.send_message(
                    int(adm_id),
                    f"🔔 <b>Заявка на доступ!</b>\n"
                    f"👤 Имя: {u_name} ({u_nick})\n"
                    f"🆔 ID: <code>{uid}</code>",
                    parse_mode="HTML",
                    reply_markup=kb.as_markup()
                )
            except Exception:
                pass

        await call.answer("Заявка отправлена!")
        await call.message.edit_text("⏳ <b>Заявка отправлена владельцу!</b> Ожидай подтверждения.", parse_mode="HTML")

    @d.callback_query(F.data.startswith("grant:"))
    async def cb_grant(call: types.CallbackQuery):
        if not is_admin(call.from_user.id, call.from_user.username):
            return
        _, target_uid_str, seconds_str = call.data.split(":")
        target_uid = int(target_uid_str)
        seconds = int(seconds_str)
        expires_at = (time.time() + seconds) if seconds > 0 else 0

        try:
            chat_info = await bot_inst.get_chat(target_uid)
            name_label = f"{chat_info.full_name} (@{chat_info.username})" if chat_info.username else chat_info.full_name
            t_uname = chat_info.username
        except Exception:
            name_label = f"User {target_uid_str}"
            t_uname = None

        access_db.setdefault("users", {})[target_uid_str] = {
            "name": name_label,
            "expires_at": expires_at,
        }
        save_access_db(access_db)

        period_text = "на 1 час" if seconds == 3600 else ("на 24 часа" if seconds == 86400 else "навсегда")
        await call.answer("Доступ выдан!")
        await call.message.edit_text(
            f"✅ Доступ для <b>{html.escape(name_label)}</b> (<code>{target_uid_str}</code>) открыт <b>{period_text}</b>!",
            parse_mode="HTML"
        )

        try:
            await bot_inst.send_message(
                target_uid,
                f"🎉 <b>Тебе открыт доступ к боту ({period_text})!</b>\nМожешь писать вопросы или скидывать фото задач 👇",
                parse_mode="HTML",
                reply_markup=get_main_reply_kb(target_uid, t_uname)
            )
        except Exception:
            pass

    @d.callback_query(F.data.startswith("makeadm:"))
    async def cb_makeadm(call: types.CallbackQuery):
        if not is_admin(call.from_user.id, call.from_user.username):
            return
        target_uid = int(call.data.split(":")[1])
        if target_uid not in access_db.setdefault("admins", []):
            access_db["admins"].append(target_uid)
        access_db.setdefault("users", {})[str(target_uid)] = {"name": f"Admin {target_uid}", "expires_at": 0}
        save_access_db(access_db)
        await call.answer("Назначен админом!")
        await call.message.edit_text(f"👑 Пользователь <code>{target_uid}</code> назначен <b>Администратором</b>!", parse_mode="HTML")
        try:
            await bot_inst.send_message(
                target_uid,
                "👑 <b>Тебе выданы права Администратора бота!</b>",
                parse_mode="HTML",
                reply_markup=get_main_reply_kb(target_uid)
            )
        except Exception:
            pass

    @d.callback_query(F.data.startswith("deny:"))
    async def cb_deny(call: types.CallbackQuery):
        if not is_admin(call.from_user.id, call.from_user.username):
            return
        target_uid = int(call.data.split(":")[1])
        await call.answer("Отклонено")
        await call.message.edit_text(f"❌ Заявка <code>{target_uid}</code> отклонена.", parse_mode="HTML")
        try:
            await bot_inst.send_message(target_uid, "❌ Владелец отклонил заявку на доступ.")
        except Exception:
            pass

    @d.message(F.text == "👑 Админка")
    async def admin_panel(message: types.Message):
        sync_user_identity(message.from_user)
        uid = message.from_user.id
        uname = message.from_user.username
        if not is_admin(uid, uname):
            await message.answer("⛔ Доступ только для администратора.", reply_markup=get_main_reply_kb(uid, uname))
            return

        admin_input_state.pop(uid, None)
        text, markup = build_admin_panel_view()
        await message.answer(text, parse_mode="HTML", reply_markup=markup)

    @d.callback_query(F.data == "adm_back")
    async def cb_adm_back(call: types.CallbackQuery):
        if not is_admin(call.from_user.id, call.from_user.username):
            return
        admin_input_state.pop(call.from_user.id, None)
        text, markup = build_admin_panel_view()
        await call.message.edit_text(text, parse_mode="HTML", reply_markup=markup)

    @d.callback_query(F.data == "adm_inspect_models")
    async def cb_adm_inspect_models(call: types.CallbackQuery):
        if not is_admin(call.from_user.id, call.from_user.username):
            return
        await call.answer("Считываю модели клиента...")
        kb = InlineKeyboardBuilder()
        kb.button(text="🔙 Назад", callback_data="adm_back")
        try:
            client = await get_web_client()
            inject_missing_models_into_client(client)
            models = client.list_models() if hasattr(client, "list_models") else []
            out = ["🔍 <b>Модели, подключенные в клиенте:</b>\n"]
            for m in models:
                d_n = html.escape(str(getattr(m, "display_name", "")))
                m_n = html.escape(str(getattr(m, "model_name", "")))
                hdr = html.escape(str(getattr(m, "model_header", "")))
                out.append(f"• <b>{d_n}</b> (<code>{m_n}</code>)\n<code>{hdr}</code>\n")
            await call.message.edit_text("\n".join(out)[:3900], parse_mode="HTML", reply_markup=kb.as_markup())
        except Exception as e:
            await call.message.edit_text(f"Ошибка: <code>{html.escape(str(e))}</code>", parse_mode="HTML", reply_markup=kb.as_markup())

    @d.callback_query(F.data == "adm_toggle_mode")
    async def cb_adm_toggle_mode(call: types.CallbackQuery):
        if not is_admin(call.from_user.id, call.from_user.username):
            return
        access_db["open_mode"] = not access_db.get("open_mode", False)
        save_access_db(access_db)
        await call.answer("Режим переключен!")
        text, markup = build_admin_panel_view()
        await call.message.edit_text(text, parse_mode="HTML", reply_markup=markup)

    @d.callback_query(F.data == "adm_add_user")
    async def cb_adm_add_user(call: types.CallbackQuery):
        if not is_admin(call.from_user.id, call.from_user.username):
            return
        admin_input_state[call.from_user.id] = "wait_add_user"
        kb = InlineKeyboardBuilder()
        kb.button(text="🔙 Назад", callback_data="adm_back")
        await call.answer()
        await call.message.edit_text(
            "➕ <b>Выдача доступа по @юзу или ID</b>\n\n"
            "Напиши следующим сообщением <code>@username</code> (или ID) человека, которому нужно открыть доступ:",
            parse_mode="HTML",
            reply_markup=kb.as_markup()
        )

    @d.callback_query(F.data == "adm_manage_admins")
    async def cb_adm_manage_admins(call: types.CallbackQuery):
        if not is_admin(call.from_user.id, call.from_user.username):
            return
        admin_input_state[call.from_user.id] = "wait_add_admin"
        cur_adms = access_db.get("admin_usernames", [OWNER_USERNAME])
        kb = InlineKeyboardBuilder()
        for u in cur_adms:
            if u.lower() != OWNER_USERNAME:
                kb.button(text=f"❌ Снять админа @{u}", callback_data=f"rmadm:{u}")
        kb.button(text="🔙 Назад", callback_data="adm_back")
        kb.adjust(1)
        await call.answer()
        await call.message.edit_text(
            "👑 <b>Управление Администраторами</b>\n\n"
            f"Текущие админы по юзу: {', '.join(['@' + x for x in cur_adms])}\n\n"
            "Чтобы назначить нового админа — просто отправь его <code>@username</code> или ID следующим сообщением:",
            parse_mode="HTML",
            reply_markup=kb.as_markup()
        )

    @d.callback_query(F.data.startswith("rmadm:"))
    async def cb_rmadm(call: types.CallbackQuery):
        if not is_admin(call.from_user.id, call.from_user.username):
            return
        u_to_rm = call.data.split(":", 1)[1].lower()
        if u_to_rm != OWNER_USERNAME and u_to_rm in access_db.get("admin_usernames", []):
            access_db["admin_usernames"].remove(u_to_rm)
            save_access_db(access_db)
        await call.answer("Админ снят!")
        await cb_adm_manage_admins(call)

    @d.callback_query(F.data == "adm_broadcast")
    async def cb_adm_broadcast(call: types.CallbackQuery):
        if not is_admin(call.from_user.id, call.from_user.username):
            return
        admin_input_state[call.from_user.id] = "wait_broadcast"
        kb = InlineKeyboardBuilder()
        kb.button(text="🔙 Назад", callback_data="adm_back")
        await call.answer()
        await call.message.edit_text(
            "📢 <b>Рассылка всем пользователям</b>\n\n"
            "Напиши следующим сообщением текст объявления — бот отправит его всем твоим клиентам:",
            parse_mode="HTML",
            reply_markup=kb.as_markup()
        )

    @d.callback_query(F.data == "adm_set_cookie")
    async def cb_adm_set_cookie(call: types.CallbackQuery):
        if not is_admin(call.from_user.id, call.from_user.username):
            return
        admin_input_state[call.from_user.id] = "wait_cookie"
        kb = InlineKeyboardBuilder()
        kb.button(text="🔙 Назад", callback_data="adm_back")
        await call.answer()
        await call.message.edit_text(
            "🍪 <b>Обновление Куки</b>\n\n"
            "Отправь следующим сообщением новые куки через пробел:\n"
            "<code>ТВОЙ_1PSID ТВОЙ_1PSIDTS</code>",
            parse_mode="HTML",
            reply_markup=kb.as_markup()
        )

    @d.callback_query(F.data == "wipe_all_chats")
    async def cb_wipe_all_chats(call: types.CallbackQuery):
        if not is_admin(call.from_user.id, call.from_user.username):
            return
        await call.answer("Удаляю все чаты с сайта Gemini...")
        deleted_count = 0
        try:
            client = await get_web_client()
            for uid_str in list(access_db.get("user_cids", {}).keys()):
                await delete_old_web_chat(int(uid_str))
                deleted_count += 1
            if hasattr(client, "list_chats") and hasattr(client, "delete_chat"):
                chats = await client.list_chats()
                if chats:
                    for ch in chats:
                        cid = getattr(ch, "cid", None)
                        if cid:
                            try:
                                await client.delete_chat(cid)
                                deleted_count += 1
                            except Exception:
                                pass
            web_chats.clear()
            kb = InlineKeyboardBuilder()
            kb.button(text="🔙 Назад в Админку", callback_data="adm_back")
            await call.message.edit_text(
                f"🗑 <b>Готово! Удалено чатов с сайта Gemini: {deleted_count}</b>",
                parse_mode="HTML",
                reply_markup=kb.as_markup()
            )
        except Exception as e:
            await call.message.edit_text(f"⚠️ Ошибка при очистке чатов: <code>{html.escape(str(e))}</code>", parse_mode="HTML")

    @d.callback_query(F.data == "check_limits")
    async def cb_check_limits(call: types.CallbackQuery):
        global last_quota_fetch_ok
        if not is_admin(call.from_user.id, call.from_user.username):
            return
        await call.answer("Обновляю данные с Google...")
        err_detail = ""
        try:
            client = await get_web_client()
            if hasattr(client, "_fetch_quota"):
                await client._fetch_quota()
            if hasattr(client, "_fetch_abuse_status"):
                await client._fetch_abuse_status()
            inject_missing_models_into_client(client)
            save_refreshed_cookies(client)
        except Exception as e:
            err_detail = str(e)

        has_saved_quota = bool(access_db.get("quota_short", {}).get("tot") or access_db.get("quota_weekly", {}).get("tot"))
        if last_quota_fetch_ok or has_saved_quota:
            cookie_ok = "✅ Активны (Авторизован в Pro)"
        elif err_detail:
            cookie_ok = f"❌ Ошибка ({html.escape(err_detail)})"
        else:
            cookie_ok = "⚠️ Нет ответа от квот"

        web_style_limits = format_limits_like_web()
        report = (
            f"{web_style_limits}\n\n"
            "────────────────────\n"
            f"🍪 <b>Статус Куки:</b> {cookie_ok}\n"
            f"🛡 <b>Аккаунт:</b> {abuse_status_store}\n"
            f"📨 <b>Запросов:</b> Веб <code>{stats_store['web_ok']}</code> | Запаска <code>{stats_store['fallback_ok']}</code>"
        )
        kb = InlineKeyboardBuilder()
        kb.button(text="🔄 Обновить", callback_data="check_limits")
        kb.button(text="🍪 Сменить куки", callback_data="adm_set_cookie")
        kb.button(text="🔙 Назад", callback_data="adm_back")
        kb.adjust(2, 1)
        try:
            await call.message.edit_text(report, parse_mode="HTML", reply_markup=kb.as_markup())
        except Exception:
            await call.message.answer(report, parse_mode="HTML", reply_markup=kb.as_markup())

    @d.callback_query(F.data == "reconnect_web")
    async def cb_reconnect_web(call: types.CallbackQuery):
        if not is_admin(call.from_user.id, call.from_user.username):
            return
        await call.answer("Переподключаю веб-сессию...")
        kb = InlineKeyboardBuilder()
        kb.button(text="🔙 Назад в Админку", callback_data="adm_back")
        try:
            await get_web_client(force_reinit=True)
            await call.message.edit_text("✅ <b>Веб-сессия Gemini Pro переподключена, все модели восстановлены!</b>", parse_mode="HTML", reply_markup=kb.as_markup())
        except Exception as e:
            await call.message.edit_text(f"⚠️ Не удалось переподключить Веб: <code>{html.escape(str(e))}</code>", parse_mode="HTML", reply_markup=kb.as_markup())

    @d.callback_query(F.data.startswith("revoke:"))
    async def cb_revoke(call: types.CallbackQuery):
        if not is_admin(call.from_user.id, call.from_user.username):
            return
        uid_str = call.data.split(":")[1]
        access_db.get("users", {}).pop(uid_str, None)
        save_access_db(access_db)
        await delete_old_web_chat(int(uid_str))
        await call.answer("Доступ закрыт!")
        text, markup = build_admin_panel_view()
        await call.message.edit_text(text, parse_mode="HTML", reply_markup=markup)
        try:
            await bot_inst.send_message(int(uid_str), "🔒 Твой доступ к боту завершён.", reply_markup=types.ReplyKeyboardRemove())
        except Exception:
            pass

    @d.message(F.text == "Новый чат")
    @d.message(Command("clear"))
    async def clear_cmd(message: types.Message):
        uid = message.from_user.id
        if not check_access(message.from_user):
            return
        await delete_old_web_chat(uid)
        user_history[uid] = []
        await message.answer("Старый чат удалён с сайта, начат новый.", reply_markup=get_main_reply_kb(uid, message.from_user.username))

    @d.message(F.text == "Модель")
    async def menu_models(message: types.Message):
        uid = message.from_user.id
        if not check_access(message.from_user):
            return
        prefs = get_prefs(uid)
        cur = prefs["model"]

        kb = InlineKeyboardBuilder()
        for label, code in MODELS_LIST:
            mark = "✓ " if cur == code else ""
            kb.button(text=f"{mark}{label}", callback_data=f"model:{code}:{label}")
        kb.adjust(1)
        await message.answer("Модель:", reply_markup=kb.as_markup())

    @d.callback_query(F.data.startswith("model:"))
    async def cb_model(call: types.CallbackQuery):
        uid = call.from_user.id
        if not check_access(call.from_user):
            return
        _, code, label = call.data.split(":", 2)
        prefs = get_prefs(uid)
        prefs["model"] = code
        prefs["model_label"] = label
        try:
            client = await get_web_client()
            target_model = resolve_model_clean(client, code, prefs["reasoning"])
            if uid in web_chats and target_model:
                web_chats[uid].model = target_model
        except Exception:
            pass
        await call.answer()
        await call.message.edit_text(f"Модель: <b>{label}</b>", parse_mode="HTML")

    @d.message(F.text == "Уровень рассуждений")
    async def menu_reasoning(message: types.Message):
        if not check_access(message.from_user):
            return
        cur = get_prefs(message.from_user.id)["reasoning"]
        kb = InlineKeyboardBuilder()
        for label, code in REASONING_LIST:
            mark = "✓ " if cur == code else ""
            kb.button(text=f"{mark}{label}", callback_data=f"reason:{code}")
        kb.adjust(1)
        await message.answer("Уровень рассуждений:", reply_markup=kb.as_markup())

    @d.callback_query(F.data.startswith("reason:"))
    async def cb_reason(call: types.CallbackQuery):
        uid = call.from_user.id
        if not check_access(call.from_user):
            return
        code = call.data.split(":", 1)[1]
        prefs = get_prefs(uid)
        prefs["reasoning"] = code
        try:
            client = await get_web_client()
            target_model = resolve_model_clean(client, prefs["model"], code)
            if uid in web_chats and target_model:
                web_chats[uid].model = target_model
        except Exception:
            pass
        label = dict((v, k) for k, v in REASONING_LIST).get(code, code)
        await call.answer()
        await call.message.edit_text(f"Уровень рассуждений: <b>{label}</b>", parse_mode="HTML")

    @d.message(F.text == "Инструменты")
    async def menu_tools(message: types.Message):
        uid = message.from_user.id
        if not check_access(message.from_user):
            return
        prefs = get_prefs(uid)
        cur = prefs["tool"]

        kb = InlineKeyboardBuilder()
        for label, code in TOOLS_LIST:
            mark = "✓ " if cur == code else ""
            kb.button(text=f"{mark}{label}", callback_data=f"tool:{code}")
        kb.adjust(1)
        await message.answer("Инструменты:", reply_markup=kb.as_markup())

    @d.callback_query(F.data.startswith("tool:"))
    async def cb_tool(call: types.CallbackQuery):
        uid = call.from_user.id
        if not check_access(call.from_user):
            return
        code = call.data.split(":", 1)[1]
        get_prefs(uid)["tool"] = code
        label = dict((v, k) for k, v in TOOLS_LIST).get(code, code)
        await call.answer()
        await call.message.edit_text(f"Инструмент: <b>{label}</b>", parse_mode="HTML")

    @d.message(F.text)
    async def handle_text(message: types.Message):
        uid = message.from_user.id
        uname = message.from_user.username
        if not check_access(message.from_user):
            await message.answer("🔒 Доступ закрыт. Отправь заявку владельцу:", reply_markup=get_locked_kb())
            return

        if is_admin(uid, uname) and uid in admin_input_state:
            mode = admin_input_state.pop(uid)
            val = message.text.strip()
            if mode == "wait_add_user":
                clean_u = val.lstrip("@").lower()
                if clean_u.isdigit():
                    access_db.setdefault("users", {})[clean_u] = {"name": f"ID {clean_u}", "expires_at": 0}
                else:
                    access_db.setdefault("preapproved_usernames", {})[clean_u] = 0
                save_access_db(access_db)
                await message.answer(f"✅ Доступ для <b>@{clean_u}</b> открыт!", parse_mode="HTML", reply_markup=get_main_reply_kb(uid, uname))
                return
            elif mode == "wait_add_admin":
                clean_u = val.lstrip("@").lower()
                if clean_u.isdigit():
                    if int(clean_u) not in access_db.setdefault("admins", []):
                        access_db["admins"].append(int(clean_u))
                else:
                    if clean_u not in access_db.setdefault("admin_usernames", []):
                        access_db["admin_usernames"].append(clean_u)
                save_access_db(access_db)
                await message.answer(f"👑 Пользователь <b>@{clean_u}</b> назначен Администратором!", parse_mode="HTML", reply_markup=get_main_reply_kb(uid, uname))
                return
            elif mode == "wait_broadcast":
                sent = 0
                for target_u in list(access_db.get("users", {}).keys()):
                    try:
                        await bot_inst.send_message(int(target_u), f"📢 <b>Сообщение от администратора:</b>\n\n{html.escape(val)}", parse_mode="HTML")
                        sent += 1
                    except Exception:
                        pass
                await message.answer(f"✅ Рассылка отправлена ({sent} чел.)!", reply_markup=get_main_reply_kb(uid, uname))
                return
            elif mode == "wait_cookie":
                parts = val.split()
                access_db["custom_psid"] = parts[0].strip()
                access_db["custom_psidts"] = parts[1].strip() if len(parts) > 1 else ""
                save_access_db(access_db)
                try:
                    await get_web_client(force_reinit=True)
                    await message.answer("✅ <b>Новые куки сохранены и подключены!</b>", parse_mode="HTML", reply_markup=get_main_reply_kb(uid, uname))
                except Exception as e:
                    await message.answer(f"⚠️ Ошибка при проверке куков: <code>{html.escape(str(e))}</code>", parse_mode="HTML")
                return

        async with continuous_typing(bot_inst, message.chat.id, "typing"):
            try:
                await ask_gemini_web(bot_inst, message, message.text)
            except Exception as web_err:
                prefs = get_prefs(uid)
                if is_image_request(message.text, prefs["tool"]):
                    if await send_fallback_image(message, message.text):
                        return
                try:
                    err_name = f"{type(web_err).__name__}: {web_err}".strip(": ")
                    ans, used_model = await ask_gemini_fallback(uid, message.text, message.text)
                    warn = f"⚠️ [Запасной API ({used_model}) | Веб: {err_name}]\n\n"
                    await safe_send(message, warn + ans)
                except Exception as e:
                    await message.answer(f"Ошибка: {e}", reply_markup=get_main_reply_kb(uid, uname))

    @d.message(F.photo)
    async def handle_photo(message: types.Message):
        uid = message.from_user.id
        uname = message.from_user.username
        if not check_access(message.from_user):
            await message.answer("🔒 Доступ закрыт. Отправь заявку владельцу:", reply_markup=get_locked_kb())
            return

        temp_path = f"/tmp/photo_{uid}.jpg"
        async with continuous_typing(bot_inst, message.chat.id, "typing"):
            try:
                photo = message.photo[-1]
                file_io = io.BytesIO()
                await bot_inst.download(photo, destination=file_io)
                file_io.seek(0)
                with open(temp_path, "wb") as f:
                    f.write(file_io.getvalue())

                caption = message.caption or "Реши или опиши фото."
                try:
                    await ask_gemini_web(bot_inst, message, caption, file_path=temp_path)
                except Exception as web_err:
                    err_name = f"{type(web_err).__name__}: {web_err}".strip(": ")
                    image = Image.open(file_io)
                    image.load()
                    ans, used_model = await ask_gemini_fallback(uid, [caption, image], f"[Фото] {caption}")
                    warn = f"⚠️ [Запасной API ({used_model}) | Веб: {err_name}]\n\n"
                    await safe_send(message, warn + ans)
            except Exception as e:
                await message.answer(f"Ошибка: {e}", reply_markup=get_main_reply_kb(uid, uname))
            finally:
                if os.path.exists(temp_path):
                    os.remove(temp_path)

    @d.message(F.document)
    async def handle_document(message: types.Message):
        uid = message.from_user.id
        uname = message.from_user.username
        if not check_access(message.from_user):
            await message.answer("🔒 Доступ закрыт. Отправь заявку владельцу:", reply_markup=get_locked_kb())
            return

        doc = message.document
        temp_path = os.path.join("/tmp", doc.file_name or f"file_{uid}")
        async with continuous_typing(bot_inst, message.chat.id, "typing"):
            try:
                file_io = io.BytesIO()
                await bot_inst.download(doc, destination=file_io)
                with open(temp_path, "wb") as f:
                    f.write(file_io.getvalue())

                caption = message.caption or "Проанализируй файл."
                await ask_gemini_web(bot_inst, message, caption, file_path=temp_path)
            except Exception as e:
                await message.answer(f"Ошибка файла: {e}", reply_markup=get_main_reply_kb(uid, uname))
            finally:
                if os.path.exists(temp_path):
                    os.remove(temp_path)

    return d

@st.cache_resource
def launch_bot_v34():
    gen_id = time.time()
    sys._active_tg_gen = gen_id

    old_state = getattr(sys, "_active_tg_bot", None)
    if old_state:
        try:
            old_loop = old_state[0]
            old_dp = old_state[1]
            asyncio.run_coroutine_threadsafe(old_dp.stop_polling(), old_loop)
            if len(old_state) > 2 and old_state[2] is not None:
                asyncio.run_coroutine_threadsafe(old_state[2].session.close(), old_loop)
        except Exception:
            pass
        time.sleep(1.5)

    def worker():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        bot_inst = Bot(token=TELEGRAM_TOKEN)
        dp_inst = create_dispatcher(bot_inst)
        sys._active_tg_bot = (loop, dp_inst, bot_inst)

        async def runner():
            while getattr(sys, "_active_tg_gen", None) == gen_id:
                try:
                    await bot_inst.delete_webhook(drop_pending_updates=True)
                    await dp_inst.start_polling(bot_inst, handle_signals=False)
                except Exception:
                    await asyncio.sleep(3)

        loop.run_until_complete(runner())

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    return t

launch_bot_v34()

st.title("Gemini Bot")
st.success("Активен (@targetturtle)")
