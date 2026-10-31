import os, sys, io, re, html, json, time, shutil, urllib.parse, subprocess, asyncio, threading, aiohttp
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from PIL import Image, ImageOps
import streamlit as st

TMP_PKGS = "/tmp/pypkgs"
os.makedirs(TMP_PKGS, exist_ok=True)
if TMP_PKGS not in sys.path:
    sys.path.append(TMP_PKGS)

from gemini_webapi import GeminiClient, AvailableModel
import gemini_webapi.client as _gw_client

HAS_PPTX = False
try:
    from pptx import Presentation
    from pptx.util import Inches, Pt
    from pptx.dml.color import RGBColor
    HAS_PPTX = True
except ImportError:
    try:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "--target", TMP_PKGS, "python-pptx"])
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

# Индекс 11 всегда строго 1, чтобы Google никогда не сбрасывал авторизацию заголовка!
FULL_MODELS_CATALOG = {
    "flash-lite": ("3.5 Flash-Lite", "gemini-flash-lite", "cf41b0e0dd7d53e5", 6),
    "flash": ("3.8 Flash", "gemini-flash", "56fdd199312815e2", 11),
    "pro": ("3.1 Pro", "gemini-pro", "e6fa609c3fa255c0", 4),
    "argon": ("4 Argon", "gemini-argon", "e6fa609c3fa255c0", 4),
}

def make_available_model(code: str) -> AvailableModel:
    d_name, m_name, m_hash, t_id = FULL_MODELS_CATALOG.get(code.lower(), FULL_MODELS_CATALOG["flash"])
    hdr = {
        "x-goog-ext-525001261-jspb": f'[1,null,null,null,"{m_hash}",null,null,0,[4,5,6,8],null,null,1, null,null,{t_id}]',
        "x-goog-ext-73010989-jspb": "[0]",
        "x-goog-ext-73010990-jspb": "[0,0,0]",
    }
    return AvailableModel(
        model_id=m_hash,
        model_name=m_name,
        display_name=d_name,
        description=d_name,
        capacity=t_id,
        model_header=hdr,
    )

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
        "admin_id": None, "admins": [], "admin_usernames": [OWNER_USERNAME],
        "users": {}, "preapproved_usernames": {}, "open_mode": False,
        "custom_psid": "", "custom_psidts": "", "user_cids": {},
        "quota_short": {}, "quota_weekly": {},
    }

def save_access_db(db):
    try:
        with open(ACCESS_FILE, "w", encoding="utf-8") as f:
            json.dump(db, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

access_db = load_access_db()
abuse_status_store = "✅ Чистый (Clean)"
stats_store = {"web_ok": 0, "fallback_ok": 0, "user_reqs": {}, "last_err": ""}
admin_input_state = {}
pending_grant_target = {}

def parse_utc_reset_to_msk(line: str, is_weekly: bool = False) -> str:
    tz_msk = timezone(timedelta(hours=3))
    m_dt = re.search(r"(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2}:\d{2})\s*UTC", line)
    if m_dt:
        try:
            dt_utc = datetime.strptime(f"{m_dt.group(1)} {m_dt.group(2)}", "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            dt_msk = dt_utc.astimezone(tz_msk)
            months_ru = ["", "янв.", "фев.", "мар.", "апр.", "мая", "июн.", "июл.", "авг.", "сент.", "окт.", "нояб.", "дек."]
            if is_weekly or dt_msk.date() != datetime.now(tz_msk).date():
                return f"{dt_msk.day} {months_ru[dt_msk.month]} в {dt_msk.strftime('%H:%M')} (МСК)"
            return f"в {dt_msk.strftime('%H:%M')} (МСК)"
        except Exception:
            pass
    return ""

def _loguru_quota_sink(message):
    global abuse_status_store
    text = str(message).strip()
    if "Account quota updated:" in text:
        info = text.split("Account quota updated:", 1)[1].strip()
        m_frac = re.search(r"(\d+)\s*/\s*(\d+)", info)
        if m_frac:
            rem, tot = int(m_frac.group(1)), int(m_frac.group(2))
            if tot > 0:
                used_pct = max(0, min(100, round((1 - (rem / tot)) * 100)))
                if tot <= 10000:
                    access_db["quota_short"] = {"rem": rem, "tot": tot, "pct": used_pct, "reset": parse_utc_reset_to_msk(info, False)}
                else:
                    access_db["quota_weekly"] = {"rem": rem, "tot": tot, "pct": used_pct, "reset": parse_utc_reset_to_msk(info, True)}
                save_access_db(access_db)
    elif "Account abuse status:" in text:
        raw_ab = text.split("Account abuse status:", 1)[1].strip()
        abuse_status_store = "✅ Чистый (Clean)" if "Clean" in raw_ab else raw_ab

if hasattr(_gw_client, "logger") and not getattr(_gw_client, "_sink_v42", False):
    try:
        _gw_client.logger.add(_loguru_quota_sink)
        _gw_client._sink_v42 = True
    except Exception:
        pass

def format_duration_text(seconds: int) -> str:
    if seconds == 0: return "♾ Навсегда"
    if seconds < 0: return "истекло"
    total_mins = max(1, int(seconds // 60))
    days, rem_mins = total_mins // 1440, total_mins % 1440
    hours, mins = rem_mins // 60, rem_mins % 60
    if days > 0 and total_mins <= 4320:
        hours += days * 24
        days = 0
    parts = []
    if days > 0: parts.append(f"{days} дн.")
    if hours > 0: parts.append(f"{hours} ч.")
    if mins > 0: parts.append(f"{mins} мин.")
    return " ".join(parts) if parts else "1 мин."

def parse_custom_duration(text: str) -> tuple[int, str]:
    s = text.lower().strip()
    if s in ("0", "навсегда", "бессрочно", "forever", "inf"): return 0, "навсегда"
    total_seconds = 0
    for m in re.finditer(r"(\d+)\s*(?:дн|ден|дня|дней|сут|д\b|d\b)", s): total_seconds += int(m.group(1)) * 86400
    for m in re.finditer(r"(\d+)\s*(?:час|часа|часов|ч\b|h\b)", s): total_seconds += int(m.group(1)) * 3600
    for m in re.finditer(r"(\d+)\s*(?:мин|минута|минуты|минут|м\b|m\b)", s): total_seconds += int(m.group(1)) * 60
    for m in re.finditer(r"(\d+)\s*(?:сек|секунд|с\b|s\b)", s): total_seconds += int(m.group(1))
    if total_seconds == 0 and s.isdigit(): total_seconds = int(s) * 60
    if total_seconds <= 0: return -1, ""
    return total_seconds, format_duration_text(total_seconds)

def build_duration_picker_kb(target_key: str):
    kb = InlineKeyboardBuilder()
    kb.button(text="📅 Сутки (24 ч.)", callback_data=f"dur_apply:{target_key}:86400")
    kb.button(text="📆 Неделя (7 дн.)", callback_data=f"dur_apply:{target_key}:604800")
    kb.button(text="🗓 Месяц (30 дн.)", callback_data=f"dur_apply:{target_key}:2592000")
    kb.button(text="♾ Навсегда", callback_data=f"dur_apply:{target_key}:0")
    kb.button(text="⏱ Своё время (ввести)", callback_data=f"dur_custom:{target_key}")
    kb.button(text="🔙 Отмена", callback_data="adm_back")
    kb.adjust(2, 2, 1, 1)
    return kb.as_markup()

async def refresh_live_quotas(client) -> bool:
    if hasattr(client, "_fetch_quota"):
        try: await client._fetch_quota()
        except Exception: pass
    rpc_cls = getattr(_gw_client, "RPCData", None)
    if rpc_cls and hasattr(client, "_batch_execute"):
        tz_msk = timezone(timedelta(hours=3))
        months_ru = ["", "янв.", "фев.", "мар.", "апр.", "мая", "июн.", "июл.", "авг.", "сент.", "окт.", "нояб.", "дек."]
        try:
            resp = await client._batch_execute([rpc_cls(rpcid="qp93dd", payload="[]")])
            if resp and resp.status_code == 200:
                for line in resp.text.splitlines():
                    if "qp93dd" not in line: continue
                    try:
                        outer = json.loads(line)
                        inner_str = next((it[2] for it in outer if isinstance(it, list) and len(it) > 2 and it[1] == "qp93dd" and it[2]), None)
                        if not inner_str: continue
                        data = json.loads(inner_str)
                    except Exception: continue
                    entries = data[0] if isinstance(data, list) and data else None
                    if not isinstance(entries, list): continue
                    for entry in entries:
                        if not isinstance(entry, list) or len(entry) < 3: continue
                        rem = entry[1]
                        tot = entry[13] if len(entry) > 13 else None
                        reset_ts = entry[7][0] if len(entry) > 7 and isinstance(entry[7], list) and entry[7] else None
                        if isinstance(rem, int) and isinstance(tot, int) and tot > 0:
                            used_pct = max(0, min(100, round((1 - (rem / tot)) * 100)))
                            reset_str = ""
                            if reset_ts:
                                dt_msk = datetime.fromtimestamp(reset_ts, tz=tz_msk)
                                reset_str = f"{dt_msk.day} {months_ru[dt_msk.month]} в {dt_msk.strftime('%H:%M')} (МСК)" if (dt_msk.date() != datetime.now(tz_msk).date() or tot > 10000) else f"в {dt_msk.strftime('%H:%M')} (МСК)"
                            if tot <= 10000:
                                access_db["quota_short"] = {"rem": rem, "tot": tot, "pct": used_pct, "reset": reset_str}
                            else:
                                access_db["quota_weekly"] = {"rem": rem, "tot": tot, "pct": used_pct, "reset": reset_str}
                save_access_db(access_db)
        except Exception: pass
    return bool(access_db.get("quota_short", {}).get("tot") or access_db.get("quota_weekly", {}).get("tot"))

def inject_missing_models_into_client(client):
    try:
        models_dict = getattr(client, "_models", None)
        if isinstance(models_dict, dict):
            for key in FULL_MODELS_CATALOG:
                m_obj = make_available_model(key)
                models_dict[m_obj.model_name] = m_obj
    except Exception:
        pass

# Чистый вывод лимитов ТОЛЬКО в процентах (без полосок!)
def format_limits_clean(uid: int = None) -> str:
    tz_msk = timezone(timedelta(hours=3))
    now_msk = datetime.now(tz_msk)
    months_ru = ["", "янв.", "фев.", "мар.", "апр.", "мая", "июн.", "июл.", "авг.", "сент.", "окт.", "нояб.", "дек."]

    qs = access_db.get("quota_short", {})
    qw = access_db.get("quota_weekly", {})

    cur_pct = qs.get("pct", 0)
    reset_short = qs.get("reset") or f"в {(now_msk + timedelta(hours=4)).strftime('%H:%M')} (МСК)"

    if qw.get("tot"):
        week_pct = qw.get("pct", 0)
        reset_week = qw.get("reset") or f"{(now_msk + timedelta(days=2)).day} {months_ru[(now_msk + timedelta(days=2)).month]}"
    else:
        used_c = (qs.get("tot", 2400) - qs.get("rem", 2400)) if qs.get("tot") else 0
        week_pct = max(1 if used_c > 0 else 0, min(100, round((used_c / 48384) * 100)))
        w_dt = now_msk + timedelta(days=2)
        reset_week = f"{w_dt.day} {months_ru[w_dt.month]} в {w_dt.strftime('%H:%M')} (МСК)"

    lines = [
        "📊 <b>Лимиты на использование PRO</b>\n",
        f"• <b>Текущее использование:</b> Использовано <b>{cur_pct} %</b>",
        f"  <i>Лимит сбросится {reset_short}</i>\n",
        f"• <b>Недельный лимит:</b> Использовано <b>{week_pct} %</b>",
        f"  <i>Лимит сбросится {reset_week}</i>",
    ]

    if uid and not is_admin(uid):
        u_info = access_db.get("users", {}).get(str(uid))
        if u_info:
            exp = u_info.get("expires_at", 0)
            rem_str = "♾ Безлимитно" if exp == 0 else format_duration_text(int(exp - time.time()))
            lines.append(f"\n🔑 <b>Твой доступ:</b> {rem_str}")

    return "\n".join(lines)

def markdown_to_telegram_html(text: str) -> str:
    code_blocks, inline_codes = [], []
    def _save_cb(m):
        code_blocks.append(f"<pre><code>{html.escape(m.group(2).strip())}</code></pre>")
        return f"@@CB_{len(code_blocks) - 1}@@"
    text = re.sub(r"```([a-zA-Z0-9_-]*)\n?(.*?)```", _save_cb, text, flags=re.DOTALL)
    def _save_ic(m):
        inline_codes.append(f"<code>{html.escape(m.group(1))}</code>")
        return f"@@IC_{len(inline_codes) - 1}@@"
    text = re.sub(r"`([^`\n]+)`", _save_ic, text)
    text = html.escape(text)
    text = re.sub(r"(?m)^\s*#{1,6}\s+(.+?)\s*$", r"<b>\1</b>", text)
    text = re.sub(r"(?m)^\s*[\*\-]\s+", "• ", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text, flags=re.DOTALL)
    text = re.sub(r"\*([^\*\n]+?)\*", r"<i>\1</i>", text)
    text = text.replace("**", "")
    for idx, c_h in enumerate(inline_codes): text = text.replace(f"@@IC_{idx}@@", c_h)
    for idx, b_h in enumerate(code_blocks): text = text.replace(f"@@CB_{idx}@@", b_h)
    return text

def sync_user_identity(user: types.User):
    if not user: return
    uid, uname = user.id, (user.username or "").lower().lstrip("@")
    changed = False
    admin_unames = [u.lower().lstrip("@") for u in access_db.get("admin_usernames", [OWNER_USERNAME])]
    if uname and (uname == OWNER_USERNAME or uname in admin_unames):
        if access_db.get("admin_id") is None or uname == OWNER_USERNAME:
            access_db["admin_id"] = uid
            changed = True
        if uid not in access_db.setdefault("admins", []):
            access_db["admins"].append(uid)
            changed = True
    if str(uid) in access_db.get("users", {}) and uname:
        if access_db["users"][str(uid)].get("username") != uname:
            access_db["users"][str(uid)]["username"] = uname
            changed = True
    pre = access_db.get("preapproved_usernames", {})
    if uname and uname in pre:
        pre_data = pre.pop(uname)
        dur = pre_data.get("duration", 0) if isinstance(pre_data, dict) else int(pre_data or 0)
        access_db.setdefault("users", {})[str(uid)] = {
            "name": f"{user.full_name} (@{user.username})", "username": uname,
            "expires_at": (time.time() + dur) if dur > 0 else 0,
        }
        changed = True
    if changed: save_access_db(access_db)

def is_admin(uid: int, username: str = None) -> bool:
    if username and username.lower().lstrip("@") in [u.lower().lstrip("@") for u in access_db.get("admin_usernames", [OWNER_USERNAME])]:
        return True
    if not uid: return False
    if access_db.get("admin_id") and int(access_db["admin_id"]) == int(uid): return True
    return int(uid) in [int(x) for x in access_db.get("admins", [])]

def check_access(user: types.User) -> bool:
    sync_user_identity(user)
    if is_admin(user.id, user.username) or access_db.get("open_mode", False): return True
    u_info = access_db.get("users", {}).get(str(user.id))
    if not u_info: return False
    exp = u_info.get("expires_at", 0)
    if exp == 0 or time.time() < exp: return True
    access_db["users"].pop(str(user.id), None)
    save_access_db(access_db)
    return False

def register_chat_cid(uid: int, *objs):
    cids = access_db.setdefault("user_cids", {}).setdefault(str(uid), [])
    changed = False
    for obj in objs:
        if obj is None: continue
        cid = getattr(obj, "cid", None)
        if not cid and getattr(obj, "metadata", None):
            meta = getattr(obj, "metadata", None)
            if isinstance(meta, (list, tuple)) and len(meta) > 0: cid = meta[0]
        if cid and isinstance(cid, str) and cid not in cids:
            cids.append(cid)
            changed = True
    if changed: save_access_db(access_db)

MODELS_LIST = [("3.5 Flash-Lite", "flash-lite"), ("3.8 Flash", "flash"), ("3.1 Pro", "pro"), ("4 Argon", "argon")]
REASONING_LIST = [("Низкий", "low"), ("Средний", "medium"), ("Высокий", "high")]
TOOLS_LIST = [
    ("Без инструмента", "none"), ("🍌 Создание изображений", "image"), ("🎬 Создание видео", "video"),
    ("🎵 Создание музыки", "music"), ("📝 Canvas", "canvas"), ("🔬 Deep Research", "deep_research"), ("📖 Обучение", "learning"),
]

web_client = None
web_init_lock = asyncio.Lock()
web_chats, user_history, user_prefs = {}, {}, {}
working_api_key = None

def get_prefs(uid: int):
    if uid not in user_prefs:
        user_prefs[uid] = {"model": "flash", "model_label": "3.8 Flash", "reasoning": "low", "tool": "none"}
    return user_prefs[uid]

# Кнопка «📊 Лимиты» теперь доступна всем пользователям, а «👑 Админка» — только админам
def get_main_reply_kb(uid: int = 0, username: str = None):
    kb = ReplyKeyboardBuilder()
    kb.button(text="Модель")
    kb.button(text="Уровень рассуждений")
    kb.button(text="Инструменты")
    kb.button(text="📊 Лимиты")
    kb.button(text="Новый чат")
    if is_admin(uid, username):
        kb.button(text="👑 Админка")
        kb.adjust(2, 2, 2)
    else:
        kb.adjust(2, 2, 1)
    return kb.as_markup(resize_keyboard=True)

def get_locked_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="📩 Запросить доступ", callback_data="req_access")
    return kb.as_markup()

def get_active_cookies():
    psid = access_db.get("custom_psid") or SECURE_1PSID
    psidts = access_db.get("custom_psidts") or SECURE_1PSIDTS
    return psid.strip() if psid else "", psidts.strip() if psidts else None

async def get_web_client(force_reinit: bool = False):
    global web_client
    psid, psidts = get_active_cookies()
    if not psid:
        raise Exception("Не указан SECURE_1PSID")
    async with web_init_lock:
        if force_reinit or web_client is None:
            client = GeminiClient(psid, psidts)
            await client.init(timeout=30, auto_refresh=True, refresh_interval=300)
            inject_missing_models_into_client(client)
            web_client = client
    if web_client is not None:
        inject_missing_models_into_client(web_client)
    return web_client

async def delete_old_web_chat(uid: int):
    old_chat = web_chats.pop(uid, None)
    if old_chat is not None: register_chat_cid(uid, old_chat)
    saved_cids = list(access_db.get("user_cids", {}).get(str(uid), []))
    if not saved_cids: return
    try:
        client = await get_web_client()
        if hasattr(client, "delete_chat"):
            for cid in saved_cids:
                try: await client.delete_chat(cid)
                except Exception: pass
        access_db.setdefault("user_cids", {})[str(uid)] = []
        save_access_db(access_db)
    except Exception:
        pass

def resolve_model_clean(client, model_code: str) -> AvailableModel:
    inject_missing_models_into_client(client)
    return make_available_model(model_code)

@asynccontextmanager
async def continuous_typing(bot_inst: Bot, chat_id: int, action: str = "typing"):
    stop_event = asyncio.Event()
    async def _loop():
        while not stop_event.is_set():
            try: await bot_inst.send_chat_action(chat_id=chat_id, action=action)
            except Exception: pass
            try: await asyncio.wait_for(stop_event.wait(), timeout=4.0)
            except asyncio.TimeoutError: pass
    task = asyncio.create_task(_loop())
    try: yield
    finally:
        stop_event.set()
        task.cancel()

def parse_requested_dimensions(prompt: str) -> tuple[int, int]:
    m = re.search(r"(\d{3,4})\s*[xх×*]\s*(\d{3,4})", prompt.lower())
    if m:
        w, h = int(m.group(1)), int(m.group(2))
        if 100 <= w <= 2048 and 100 <= h <= 2048:
            return w, h
    return 1024, 1024

def resize_image_bytes_if_needed(img_bytes: bytes, prompt: str) -> bytes:
    m = re.search(r"(\d{3,4})\s*[xх×*]\s*(\d{3,4})", prompt.lower())
    if not m:
        return img_bytes
    try:
        w, h = int(m.group(1)), int(m.group(2))
        if 100 <= w <= 2048 and 100 <= h <= 2048:
            im = Image.open(io.BytesIO(img_bytes)).convert("RGB")
            im = ImageOps.fit(im, (w, h), Image.Resampling.LANCZOS)
            out = io.BytesIO()
            im.save(out, format="PNG")
            return out.getvalue()
    except Exception:
        pass
    return img_bytes

def is_image_intent(prompt: str, tool: str) -> bool:
    if tool == "image":
        return True
    p_low = prompt.lower()
    return any(w in p_low for w in ["нарисуй", "сгенерируй", "создай картинк", "создай изображен", "баннер", "логотип", "кинь картинк", "арт ", "обои"])

def is_image_refusal(resp_text: str) -> bool:
    if not resp_text:
        return False
    low = resp_text.lower()
    refusals = [
        "вы вошли в систему?",
        "не могу создавать их для вас",
        "недоступно в вашем регионе",
        "я могу искать изображения, но",
        "не умею создавать изображения",
        "can't create them for you",
    ]
    return any(r in low for r in refusals)

# Безотказная генерация картинки (если веб-чат выдал региональный отказ или сработала запаска)
async def generate_guaranteed_image(message: types.Message, prompt_ru: str) -> bool:
    uid, uname = message.from_user.id, message.from_user.username
    w, h = parse_requested_dimensions(prompt_ru)

    # 1. Пробуем через Gemini Image API, если есть ключи
    prompt_en = prompt_ru
    for key in GEMINI_KEYS:
        if not key: continue
        try:
            g_client = genai.Client(api_key=key.strip())
            tr = await asyncio.to_thread(
                g_client.models.generate_content,
                model="gemini-3.5-flash-lite",
                contents=f"Convert this request into a vivid, detailed English image generation prompt (output ONLY the English prompt, no quotes, no size numbers): {prompt_ru}",
            )
            if tr and tr.text:
                prompt_en = tr.text.strip()
            break
        except Exception:
            continue

    encoded = urllib.parse.quote(prompt_en[:450])
    img_url = f"https://image.pollinations.ai/prompt/{encoded}?width={w}&height={h}&nologo=true&model=flux&seed={int(time.time()) % 10000}"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/131.0.0.0 Safari/537.36"}
    try:
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(img_url, timeout=45) as resp:
                if resp.status == 200:
                    raw_data = await resp.read()
                    final_bytes = resize_image_bytes_if_needed(raw_data, prompt_ru)
                    photo_file = types.BufferedInputFile(final_bytes, filename="image.png")
                    await message.answer_photo(photo_file, reply_markup=get_main_reply_kb(uid, uname))
                    return True
    except Exception:
        pass
    try:
        await message.answer_photo(img_url, reply_markup=get_main_reply_kb(uid, uname))
        return True
    except Exception:
        return False

def build_canvas_pptx(slides_data: list) -> bytes:
    if not HAS_PPTX: return b""
    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    blank_layout = prs.slide_layouts[6]
    for idx, s in enumerate(slides_data):
        slide = prs.slides.add_slide(blank_layout)
        fill = slide.background.fill
        fill.solid()
        fill.fore_color.rgb = RGBColor(13, 25, 43)
        tb_tag = slide.shapes.add_textbox(Inches(1.0), Inches(1.0), Inches(11.3), Inches(0.5))
        p_tag = tb_tag.text_frame.paragraphs[0]
        p_tag.text = str(s.get("tag", f"СЛАЙД {idx + 1}")).upper()
        p_tag.font.size, p_tag.font.bold, p_tag.font.color.rgb = Pt(11), True, RGBColor(56, 189, 248)
        tb_title = slide.shapes.add_textbox(Inches(1.0), Inches(1.5), Inches(11.3), Inches(1.6))
        tb_title.text_frame.word_wrap = True
        p_title = tb_title.text_frame.paragraphs[0]
        p_title.text = str(s.get("title", f"Слайд {idx + 1}"))
        p_title.font.size, p_title.font.bold, p_title.font.color.rgb = Pt(34 if idx == 0 else 28), True, RGBColor(255, 255, 255)
        tb_body = slide.shapes.add_textbox(Inches(1.0), Inches(3.2), Inches(11.3), Inches(3.6))
        tf_body = tb_body.text_frame
        tf_body.word_wrap = True
        first_p = True
        if s.get("subtitle"):
            p = tf_body.paragraphs[0]
            first_p = False
            p.text, p.font.size, p.font.color.rgb, p.space_after = str(s["subtitle"]), Pt(18), RGBColor(148, 163, 184), Pt(14)
        for pt in s.get("points", []):
            p = tf_body.paragraphs[0] if first_p else tf_body.add_paragraph()
            first_p = False
            p.text, p.font.size, p.font.color.rgb, p.space_after = f"• {pt}", Pt(18), RGBColor(226, 232, 240), Pt(10)
    out_io = io.BytesIO()
    prs.save(out_io)
    return out_io.getvalue()

async def safe_send(message: types.Message, text: str):
    if not text: return
    uid, uname = message.from_user.id, message.from_user.username
    text = re.sub(r"https?://googleusercontent\.com/\S+", "", text)
    text = re.sub(r"(?:^|\s)_[0-9]+\b", "", text).strip()
    if not text: return
    if "SEND_FILE:" in text:
        after_tag = text.split("SEND_FILE:", 1)[1].strip()
        lines = after_tag.split("\n", 1)
        filename = lines[0].strip() or "file.py"
        file_content = re.sub(r"^```[a-zA-Z0-9_-]*\n|\n```$", "", (lines[1] if len(lines) > 1 else "").strip())
        doc = types.BufferedInputFile(file_content.encode("utf-8"), filename=filename)
        await message.answer_document(doc, reply_markup=get_main_reply_kb(uid, uname))
        return
    for i in range(0, len(text), 3800):
        raw_part = text[i:i + 3800]
        html_part = markdown_to_telegram_html(raw_part)
        try:
            await message.answer(html_part, parse_mode="HTML", reply_markup=get_main_reply_kb(uid, uname))
        except Exception:
            clean_plain = re.sub(r"<[^>]+>", "", html.unescape(html_part))
            await message.answer(clean_plain, parse_mode=None, reply_markup=get_main_reply_kb(uid, uname))

def is_presentation_request(prompt: str, resp_text: str) -> bool:
    p_low, r_low = prompt.lower(), (resp_text or "").lower()
    if any(k in p_low for k in ["презентац", "повер поинт", "павер поинт", "powerpoint", "pptx", "слайд"]): return True
    return ("презентац" in r_low or "presentation" in r_low) and ("готов" in r_low or "посмотреть" in r_low or "i will now generate" in r_low)

def is_canvas_stub(resp_text: str) -> bool:
    if not resp_text: return False
    r_low = resp_text.lower()
    return len(resp_text) < 600 and any(s in r_low for s in ["i will now generate", "вы можете посмотреть её", "вы можете посмотреть его", "готова! вы можете", "готов! вы можете"])

async def ask_gemini_web(bot_inst: Bot, message: types.Message, prompt: str, file_path: str = None):
    uid, uname = message.from_user.id, message.from_user.username
    prefs = get_prefs(uid)
    want_img = is_image_intent(prompt, prefs["tool"])

    effective_code = "flash" if (want_img and prefs["model"] == "flash-lite") else prefs["model"]
    files = [file_path] if file_path else None
    client = await get_web_client()
    target_model = resolve_model_clean(client, effective_code)

    if uid not in web_chats:
        web_chats[uid] = client.start_chat(model=target_model)
    else:
        web_chats[uid].model = target_model

    chat = web_chats[uid]
    send_prompt = f"Generate an image: {prompt}" if (want_img and not file_path) else prompt
    response = await asyncio.wait_for(chat.send_message(send_prompt, files=files), timeout=90)
    register_chat_cid(uid, chat, response)

    resp_text = getattr(response, "text", "") or ""
    stats_store["web_ok"] += 1
    stats_store["user_reqs"][str(uid)] = stats_store["user_reqs"].get(str(uid), 0) + 1

    sent_img = False
    if getattr(response, "images", None):
        for idx, img in enumerate(response.images):
            try:
                out_name = f"img_{uid}_{idx}.png"
                out_path = os.path.join("/tmp", out_name)
                await img.save(path="/tmp", filename=out_name)
                if os.path.exists(out_path):
                    with open(out_path, "rb") as f:
                        img_b = resize_image_bytes_if_needed(f.read(), prompt)
                        await message.answer_photo(types.BufferedInputFile(img_b, filename=out_name), reply_markup=get_main_reply_kb(uid, uname))
                    os.remove(out_path)
                    sent_img = True
                elif getattr(img, "url", None):
                    await message.answer_photo(img.url, reply_markup=get_main_reply_kb(uid, uname))
                    sent_img = True
            except Exception:
                if getattr(img, "url", None):
                    try:
                        await message.answer_photo(img.url, reply_markup=get_main_reply_kb(uid, uname))
                        sent_img = True
                    except Exception: pass

    # Если просили нарисовать картинку, а сайт выдал региональный отказ «Вы вошли в систему?» или не дал картинку — рисуем картинку без показа текста отказа!
    if (want_img or is_image_refusal(resp_text)) and not sent_img:
        if await generate_guaranteed_image(message, prompt):
            return

    if getattr(response, "videos", None):
        vid_dir = f"/tmp/vid_{uid}_{int(time.time())}"
        os.makedirs(vid_dir, exist_ok=True)
        try:
            for vid in response.videos: await vid.save(path=vid_dir, verbose=True)
            for fname in sorted(os.listdir(vid_dir)):
                fpath = os.path.join(vid_dir, fname)
                if os.path.isfile(fpath):
                    with open(fpath, "rb") as f:
                        await message.answer_video(types.BufferedInputFile(f.read(), filename=fname if fname.endswith(".mp4") else f"{fname}.mp4"), caption="🎬 <b>Видео готово!</b>", parse_mode="HTML", reply_markup=get_main_reply_kb(uid, uname))
        except Exception: pass
        finally: shutil.rmtree(vid_dir, ignore_errors=True)

    if getattr(response, "media", None):
        med_dir = f"/tmp/med_{uid}_{int(time.time())}"
        os.makedirs(med_dir, exist_ok=True)
        try:
            for med in response.media: await med.save(path=med_dir, verbose=True)
            for fname in sorted(os.listdir(med_dir)):
                fpath = os.path.join(med_dir, fname)
                if os.path.isfile(fpath):
                    with open(fpath, "rb") as f: raw_b = f.read()
                    if fname.lower().endswith((".mp3", ".wav", ".ogg", ".m4a")):
                        await message.answer_audio(types.BufferedInputFile(raw_b, filename=fname), caption="🎵 <b>Музыкальный трек готов!</b>", parse_mode="HTML", reply_markup=get_main_reply_kb(uid, uname))
                    elif fname.lower().endswith(".mp4"):
                        await message.answer_video(types.BufferedInputFile(raw_b, filename=fname), caption="🎬 <b>Видео-обложка трека</b>", parse_mode="HTML", reply_markup=get_main_reply_kb(uid, uname))
        except Exception: pass
        finally: shutil.rmtree(med_dir, ignore_errors=True)

    if is_presentation_request(prompt, resp_text):
        try:
            extract_prompt = (
                "Не используй Canvas. Выведи все слайды этой презентации СТРОГО в формате чистого JSON-массива "
                "(без кавычек ```json и без лишнего текста): "
                '[{"tag": "РАЗДЕЛ", "title": "Заголовок", "subtitle": "Подзаголовок", "points": ["Пункт 1", "Пункт 2"]}]'
            )
            slide_resp = await asyncio.wait_for(chat.send_message(extract_prompt), timeout=90)
            m_json = re.search(r"\[.*\]", (slide_resp.text or "").strip(), flags=re.DOTALL)
            if m_json:
                slides_data = json.loads(m_json.group(0))
                if isinstance(slides_data, list) and slides_data:
                    pptx_bytes = build_canvas_pptx(slides_data)
                    if pptx_bytes:
                        await message.answer_document(types.BufferedInputFile(pptx_bytes, filename="Presentation.pptx"), caption="📊 <b>Презентация PowerPoint (.pptx) готова!</b>", parse_mode="HTML", reply_markup=get_main_reply_kb(uid, uname))
                        return
        except Exception:
            pass
    elif is_canvas_stub(resp_text) and not getattr(response, "videos", None) and not getattr(response, "media", None):
        try:
            extract_doc = await asyncio.wait_for(chat.send_message("Не открывай Canvas. Выведи полное содержимое документа/кода обычным текстом прямо сюда в чат."), timeout=90)
            if getattr(extract_doc, "text", None): resp_text = extract_doc.text
        except Exception:
            pass

    if resp_text and not is_image_refusal(resp_text):
        await safe_send(message, resp_text)

async def ask_gemini_fallback(user_id: int, new_content, history_text: str):
    global working_api_key
    tz_msk = timezone(timedelta(hours=3))
    current_time = datetime.now(tz_msk).strftime("%d.%m.%Y, %H:%M")
    prefs = get_prefs(user_id)
    keys_to_try = ([working_api_key] + [k for k in GEMINI_KEYS if k != working_api_key]) if working_api_key else GEMINI_KEYS
    last_err = None
    for model_name in ["gemini-3.8-flash", "gemini-3.5-flash-lite"]:
        for key in keys_to_try:
            if not key: continue
            try:
                client = genai.Client(api_key=key.strip())
                response = await asyncio.to_thread(
                    client.models.generate_content, model=model_name, contents=[new_content],
                    config=gtypes.GenerateContentConfig(system_instruction=f"Сейчас (МСК): {current_time}. Модель: {prefs['model_label']}. Пиши математику без $ и без LaTeX."),
                )
                working_api_key = key
                stats_store["fallback_ok"] += 1
                stats_store["user_reqs"][str(user_id)] = stats_store["user_reqs"].get(str(user_id), 0) + 1
                return (response.text or "Пустой ответ."), model_name
            except Exception as e:
                last_err = e
    raise Exception(f"Сервис временно перегружен ({last_err})")

async def apply_user_grant(bot_inst: Bot, target_key: str, seconds: int) -> str:
    clean_key = target_key.lstrip("@").lower().strip()
    expires_at = (time.time() + seconds) if seconds > 0 else 0
    period_str = format_duration_text(seconds)
    matched_uid_str = clean_key if clean_key.isdigit() else None
    if not matched_uid_str:
        for u_id, info in access_db.get("users", {}).items():
            if info.get("username", "").lower() == clean_key or f"@{clean_key}" in info.get("name", "").lower():
                matched_uid_str = u_id
                break
    if matched_uid_str:
        target_uid = int(matched_uid_str)
        try:
            chat_info = await bot_inst.get_chat(target_uid)
            name_label = f"{chat_info.full_name} (@{chat_info.username})" if chat_info.username else chat_info.full_name
            uname_val = (chat_info.username or "").lower()
        except Exception:
            old_info = access_db.get("users", {}).get(matched_uid_str, {})
            name_label, uname_val = old_info.get("name", f"ID {matched_uid_str}"), old_info.get("username", clean_key)
        access_db.setdefault("users", {})[matched_uid_str] = {"name": name_label, "username": uname_val, "expires_at": expires_at}
        access_db.get("preapproved_usernames", {}).pop(clean_key, None)
        save_access_db(access_db)
        try:
            await bot_inst.send_message(target_uid, f"🎉 <b>Тебе открыт доступ к боту!</b>\n⏳ Срок: <b>{period_str}</b>", parse_mode="HTML", reply_markup=get_main_reply_kb(target_uid, uname_val))
        except Exception: pass
        return f"✅ Доступ для <b>{html.escape(name_label)}</b> открыт на срок: <b>{period_str}</b>!"
    access_db.setdefault("preapproved_usernames", {})[clean_key] = {"duration": seconds, "added_at": time.time()}
    save_access_db(access_db)
    return f"✅ Доступ для <b>@{html.escape(clean_key)}</b> сохранён на срок: <b>{period_str}</b>!"

async def revoke_user_by_key(bot_inst: Bot, target_key: str) -> str:
    clean_key = target_key.lstrip("@").lower().strip()
    removed_names = []
    if clean_key in access_db.get("preapproved_usernames", {}):
        access_db["preapproved_usernames"].pop(clean_key, None)
        removed_names.append(f"@{clean_key}")
    for uid_str, info in list(access_db.get("users", {}).items()):
        if uid_str == clean_key or info.get("username", "").lower() == clean_key or f"@{clean_key}" in info.get("name", "").lower():
            access_db["users"].pop(uid_str, None)
            removed_names.append(info.get("name", uid_str))
            await delete_old_web_chat(int(uid_str))
            try: await bot_inst.send_message(int(uid_str), "🔒 Твой доступ к боту завершён.", reply_markup=types.ReplyKeyboardRemove())
            except Exception: pass
    save_access_db(access_db)
    return f"🔒 Доступ удалён у: <b>{html.escape(', '.join(removed_names))}</b>" if removed_names else f"⚠️ Пользователь <code>{html.escape(target_key)}</code> не найден."

def build_admin_panel_view():
    users = access_db.get("users", {})
    mode_str = "🔓 ОТКРЫТ ДЛЯ ВСЕХ" if access_db.get("open_mode", False) else "🔒 ЗАКРЫТЫЙ (По заявкам)"
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Выдать доступ", callback_data="adm_add_user")
    kb.button(text="❌ Удалить доступ", callback_data="adm_del_user_menu")
    kb.button(text="📊 Лимиты и Куки", callback_data="check_limits")
    kb.button(text="🔍 Модели на аккаунте", callback_data="adm_inspect_models")
    kb.button(text="👑 Выдать / Снять Админку", callback_data="adm_manage_admins")
    kb.button(text=f"Режим: {mode_str}", callback_data="adm_toggle_mode")
    kb.button(text="📢 Рассылка всем", callback_data="adm_broadcast")
    kb.button(text="🍪 Обновить Куки", callback_data="adm_set_cookie")
    kb.button(text="🔄 Переподключить Веб", callback_data="reconnect_web")
    kb.button(text="🗑 Очистить чаты на сайте", callback_data="wipe_all_chats")
    lines = [
        "👑 <b>Панель управления (@targetturtle)</b>",
        f"⚙️ Режим входа: <b>{mode_str}</b>",
        f"📨 Запросов: Веб <code>{stats_store['web_ok']}</code> | Запаска <code>{stats_store['fallback_ok']}</code>\n",
    ]
    if stats_store.get("last_err"):
        lines.append(f"⚠️ Последний сбой веба: <code>{html.escape(stats_store['last_err'][:120])}</code>\n")
    now = time.time()
    active_count = 0
    for uid_str, info in list(users.items()):
        exp = info.get("expires_at", 0)
        if exp != 0 and now >= exp:
            users.pop(uid_str, None)
            continue
        active_count += 1
        reqs = stats_store["user_reqs"].get(uid_str, 0)
        rem = "♾ Навсегда" if exp == 0 else f"⏳ Осталось: {format_duration_text(int(exp - now))}"
        is_u_adm = "👑 " if int(uid_str) in [int(x) for x in access_db.get("admins", [])] else ""
        lines.append(f"• {is_u_adm}<b>{html.escape(info.get('name', uid_str))}</b> ({rem} | Запросов: {reqs})")
    pre = access_db.get("preapproved_usernames", {})
    if pre:
        lines.append("\n⏳ <b>Ожидают входа:</b> " + ", ".join([f"@{k} ({format_duration_text(v.get('duration', 0) if isinstance(v, dict) else 0)})" for k, v in pre.items()]))
    if active_count == 0 and not pre:
        lines.append("Сейчас активных клиентов нет.")
    save_access_db(access_db)
    kb.adjust(2, 2, 2, 2, 2)
    return "\n".join(lines), kb.as_markup()

def create_dispatcher(bot_inst: Bot):
    d = Dispatcher()

    @d.message(CommandStart())
    async def start_cmd(message: types.Message):
        uid, uname = message.from_user.id, message.from_user.username
        if not check_access(message.from_user):
            await message.answer("🔒 <b>Доступ к этому боту закрыт.</b>\n\nНажми кнопку ниже, чтобы отправить заявку владельцу:", parse_mode="HTML", reply_markup=get_locked_kb())
            return
        prefs = get_prefs(uid)
        r_label = dict((v, k) for k, v in REASONING_LIST).get(prefs["reasoning"], prefs["reasoning"])
        await message.answer(f"Модель: <b>{prefs['model_label']}</b>\nУровень рассуждений: <b>{r_label}</b>", parse_mode="HTML", reply_markup=get_main_reply_kb(uid, uname))

    # Просмотр лимитов для любого пользователя (без полосок, только проценты!)
    @d.message(F.text == "📊 Лимиты")
    async def user_limits_cmd(message: types.Message):
        uid, uname = message.from_user.id, message.from_user.username
        if not check_access(message.from_user):
            await message.answer("🔒 Доступ закрыт.", reply_markup=get_locked_kb())
            return
        await message.answer(format_limits_clean(uid), parse_mode="HTML", reply_markup=get_main_reply_kb(uid, uname))

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
        kb.button(text="📅 Сутки (24ч)", callback_data=f"dur_apply:{uid}:86400")
        kb.button(text="📆 Неделя", callback_data=f"dur_apply:{uid}:604800")
        kb.button(text="🗓 Месяц", callback_data=f"dur_apply:{uid}:2592000")
        kb.button(text="♾ Навсегда", callback_data=f"dur_apply:{uid}:0")
        kb.button(text="⏱ Своё время", callback_data=f"dur_custom:{uid}")
        kb.button(text="👑 Сделать Админом", callback_data=f"makeadm:{uid}")
        kb.button(text="❌ Отказать", callback_data=f"deny:{uid}")
        kb.adjust(2, 2, 1, 1, 1)
        for adm_id in admin_targets:
            try: await bot_inst.send_message(int(adm_id), f"🔔 <b>Заявка на доступ!</b>\n👤 Имя: {u_name} ({u_nick})\n🆔 ID: <code>{uid}</code>", parse_mode="HTML", reply_markup=kb.as_markup())
            except Exception: pass
        await call.answer("Заявка отправлена!")
        await call.message.edit_text("⏳ <b>Заявка отправлена владельцу!</b> Ожидай подтверждения.", parse_mode="HTML")

    @d.callback_query(F.data.startswith("dur_apply:"))
    async def cb_dur_apply(call: types.CallbackQuery):
        if not is_admin(call.from_user.id, call.from_user.username): return
        _, target_key, seconds_str = call.data.split(":", 2)
        msg_res = await apply_user_grant(bot_inst, target_key, int(seconds_str))
        kb = InlineKeyboardBuilder()
        kb.button(text="🔙 В Админку", callback_data="adm_back")
        await call.answer("Доступ выдан!")
        await call.message.edit_text(msg_res, parse_mode="HTML", reply_markup=kb.as_markup())

    @d.callback_query(F.data.startswith("dur_custom:"))
    async def cb_dur_custom(call: types.CallbackQuery):
        if not is_admin(call.from_user.id, call.from_user.username): return
        target_key = call.data.split(":", 1)[1]
        pending_grant_target[call.from_user.id] = target_key
        admin_input_state[call.from_user.id] = "wait_custom_duration"
        kb = InlineKeyboardBuilder()
        kb.button(text="🔙 Отмена", callback_data="adm_back")
        await call.answer()
        await call.message.edit_text(f"⏱ <b>Своё время для</b> <code>{html.escape(target_key)}</code>\n\nНапиши время (например: <code>1 час 30 минут</code>, <code>1 час</code>, <code>33 часа</code>, <code>45 минут</code>):", parse_mode="HTML", reply_markup=kb.as_markup())

    @d.callback_query(F.data.startswith("makeadm:"))
    async def cb_makeadm(call: types.CallbackQuery):
        if not is_admin(call.from_user.id, call.from_user.username): return
        target_uid = int(call.data.split(":")[1])
        if target_uid not in access_db.setdefault("admins", []): access_db["admins"].append(target_uid)
        access_db.setdefault("users", {})[str(target_uid)] = {"name": f"Admin {target_uid}", "expires_at": 0}
        save_access_db(access_db)
        await call.answer("Назначен админом!")
        await call.message.edit_text(f"👑 Пользователь <code>{target_uid}</code> назначен <b>Администратором</b>!", parse_mode="HTML")
        try: await bot_inst.send_message(target_uid, "👑 <b>Тебе выданы права Администратора бота!</b>", parse_mode="HTML", reply_markup=get_main_reply_kb(target_uid))
        except Exception: pass

    @d.callback_query(F.data.startswith("deny:"))
    async def cb_deny(call: types.CallbackQuery):
        if not is_admin(call.from_user.id, call.from_user.username): return
        target_uid = int(call.data.split(":")[1])
        await call.answer("Отклонено")
        await call.message.edit_text(f"❌ Заявка <code>{target_uid}</code> отклонена.", parse_mode="HTML")
        try: await bot_inst.send_message(target_uid, "❌ Владелец отклонил заявку на доступ.")
        except Exception: pass

    @d.message(F.text == "👑 Админка")
    async def admin_panel(message: types.Message):
        sync_user_identity(message.from_user)
        uid, uname = message.from_user.id, message.from_user.username
        if not is_admin(uid, uname):
            await message.answer("⛔ Доступ только для администратора.", reply_markup=get_main_reply_kb(uid, uname))
            return
        admin_input_state.pop(uid, None)
        text, markup = build_admin_panel_view()
        await message.answer(text, parse_mode="HTML", reply_markup=markup)

    @d.callback_query(F.data == "adm_back")
    async def cb_adm_back(call: types.CallbackQuery):
        if not is_admin(call.from_user.id, call.from_user.username): return
        admin_input_state.pop(call.from_user.id, None)
        text, markup = build_admin_panel_view()
        await call.message.edit_text(text, parse_mode="HTML", reply_markup=markup)

    @d.callback_query(F.data == "adm_inspect_models")
    async def cb_adm_inspect_models(call: types.CallbackQuery):
        if not is_admin(call.from_user.id, call.from_user.username): return
        await call.answer("Считываю модели клиента...")
        kb = InlineKeyboardBuilder()
        kb.button(text="🔙 Назад", callback_data="adm_back")
        try:
            client = await get_web_client()
            models = client.list_models() if hasattr(client, "list_models") else []
            out = ["🔍 <b>Модели в клиенте:</b>\n"]
            for m in models:
                out.append(f"• <b>{html.escape(str(getattr(m, 'display_name', '')))}</b> (<code>{html.escape(str(getattr(m, 'model_name', '')))}</code>)\n<code>{html.escape(str(getattr(m, 'model_header', '')))}</code>\n")
            await call.message.edit_text("\n".join(out)[:3900], parse_mode="HTML", reply_markup=kb.as_markup())
        except Exception as e:
            await call.message.edit_text(f"Ошибка: <code>{html.escape(str(e))}</code>", parse_mode="HTML", reply_markup=kb.as_markup())

    @d.callback_query(F.data == "adm_toggle_mode")
    async def cb_adm_toggle_mode(call: types.CallbackQuery):
        if not is_admin(call.from_user.id, call.from_user.username): return
        access_db["open_mode"] = not access_db.get("open_mode", False)
        save_access_db(access_db)
        await call.answer("Режим переключен!")
        text, markup = build_admin_panel_view()
        await call.message.edit_text(text, parse_mode="HTML", reply_markup=markup)

    @d.callback_query(F.data == "adm_add_user")
    async def cb_adm_add_user(call: types.CallbackQuery):
        if not is_admin(call.from_user.id, call.from_user.username): return
        admin_input_state[call.from_user.id] = "wait_add_user_target"
        kb = InlineKeyboardBuilder()
        kb.button(text="🔙 Назад", callback_data="adm_back")
        await call.answer()
        await call.message.edit_text("➕ <b>Выдача доступа по @юзу или ID</b>\n\nНапиши следующим сообщением <code>@username</code> (или ID):", parse_mode="HTML", reply_markup=kb.as_markup())

    @d.callback_query(F.data == "adm_del_user_menu")
    async def cb_adm_del_user_menu(call: types.CallbackQuery):
        if not is_admin(call.from_user.id, call.from_user.username): return
        admin_input_state[call.from_user.id] = "wait_del_user_target"
        kb = InlineKeyboardBuilder()
        for uid_str, info in list(access_db.get("users", {}).items()):
            kb.button(text=f"❌ Удалить {info.get('name', uid_str)[:22]}", callback_data=f"revoke:{uid_str}")
        for u_pre in list(access_db.get("preapproved_usernames", {}).keys()):
            kb.button(text=f"❌ Удалить @{u_pre}", callback_data=f"revoke:{u_pre}")
        kb.button(text="🔙 Назад", callback_data="adm_back")
        kb.adjust(1)
        await call.answer()
        await call.message.edit_text("❌ <b>Удаление доступа</b>\n\nНажми на человека ниже ИЛИ отправь его <code>@username</code> / <code>ID</code> сообщением:", parse_mode="HTML", reply_markup=kb.as_markup())

    @d.callback_query(F.data == "adm_manage_admins")
    async def cb_adm_manage_admins(call: types.CallbackQuery):
        if not is_admin(call.from_user.id, call.from_user.username): return
        admin_input_state[call.from_user.id] = "wait_add_admin"
        cur_adms = access_db.get("admin_usernames", [OWNER_USERNAME])
        kb = InlineKeyboardBuilder()
        for u in cur_adms:
            if u.lower() != OWNER_USERNAME: kb.button(text=f"❌ Снять админа @{u}", callback_data=f"rmadm:{u}")
        kb.button(text="🔙 Назад", callback_data="adm_back")
        kb.adjust(1)
        await call.answer()
        await call.message.edit_text(f"👑 <b>Администраторы:</b> {', '.join(['@' + x for x in cur_adms])}\n\nОтправь <code>@username</code> или ID нового админа:", parse_mode="HTML", reply_markup=kb.as_markup())

    @d.callback_query(F.data.startswith("rmadm:"))
    async def cb_rmadm(call: types.CallbackQuery):
        if not is_admin(call.from_user.id, call.from_user.username): return
        u_to_rm = call.data.split(":", 1)[1].lower()
        if u_to_rm != OWNER_USERNAME and u_to_rm in access_db.get("admin_usernames", []):
            access_db["admin_usernames"].remove(u_to_rm)
            save_access_db(access_db)
        await call.answer("Админ снят!")
        await cb_adm_manage_admins(call)

    @d.callback_query(F.data == "adm_broadcast")
    async def cb_adm_broadcast(call: types.CallbackQuery):
        if not is_admin(call.from_user.id, call.from_user.username): return
        admin_input_state[call.from_user.id] = "wait_broadcast"
        kb = InlineKeyboardBuilder()
        kb.button(text="🔙 Назад", callback_data="adm_back")
        await call.answer()
        await call.message.edit_text("📢 <b>Рассылка всем пользователям</b>\n\nНапиши текст объявления:", parse_mode="HTML", reply_markup=kb.as_markup())

    @d.callback_query(F.data == "adm_set_cookie")
    async def cb_adm_set_cookie(call: types.CallbackQuery):
        if not is_admin(call.from_user.id, call.from_user.username): return
        admin_input_state[call.from_user.id] = "wait_cookie"
        kb = InlineKeyboardBuilder()
        kb.button(text="🔙 Назад", callback_data="adm_back")
        await call.answer()
        await call.message.edit_text("🍪 <b>Обновление Куки</b>\n\nОтправь через пробел:\n<code>ТВОЙ_1PSID ТВОЙ_1PSIDTS</code>", parse_mode="HTML", reply_markup=kb.as_markup())

    @d.callback_query(F.data == "wipe_all_chats")
    async def cb_wipe_all_chats(call: types.CallbackQuery):
        if not is_admin(call.from_user.id, call.from_user.username): return
        await call.answer("Удаляю все чаты...")
        deleted_count = 0
        try:
            client = await get_web_client()
            for uid_str in list(access_db.get("user_cids", {}).keys()):
                await delete_old_web_chat(int(uid_str))
                deleted_count += 1
            if hasattr(client, "list_chats") and hasattr(client, "delete_chat"):
                for ch in (await client.list_chats() or []):
                    cid = getattr(ch, "cid", None)
                    if cid:
                        try:
                            await client.delete_chat(cid)
                            deleted_count += 1
                        except Exception: pass
            web_chats.clear()
            kb = InlineKeyboardBuilder()
            kb.button(text="🔙 Назад", callback_data="adm_back")
            await call.message.edit_text(f"🗑 <b>Удалено чатов: {deleted_count}</b>", parse_mode="HTML", reply_markup=kb.as_markup())
        except Exception as e:
            await call.message.edit_text(f"⚠️ Ошибка: <code>{html.escape(str(e))}</code>", parse_mode="HTML")

    @d.callback_query(F.data == "check_limits")
    async def cb_check_limits(call: types.CallbackQuery):
        if not is_admin(call.from_user.id, call.from_user.username): return
        await call.answer("Опрашиваю Google...")
        err_detail, direct_ok = "", False
        try:
            client = await get_web_client()
            direct_ok = await refresh_live_quotas(client)
            if hasattr(client, "_fetch_abuse_status"): await client._fetch_abuse_status()
            inject_missing_models_into_client(client)
        except Exception as e:
            err_detail = str(e)

        cookie_ok = "✅ Активны (Авторизован в Pro)" if direct_ok else (f"❌ Ошибка ({html.escape(err_detail)})" if err_detail else "⚠️ Ожидают обновления")
        report = (
            f"{format_limits_clean()}\n\n"
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
        try: await call.message.edit_text(report, parse_mode="HTML", reply_markup=kb.as_markup())
        except Exception: await call.message.answer(report, parse_mode="HTML", reply_markup=kb.as_markup())

    @d.callback_query(F.data == "reconnect_web")
    async def cb_reconnect_web(call: types.CallbackQuery):
        if not is_admin(call.from_user.id, call.from_user.username): return
        await call.answer("Переподключаю веб-сессию...")
        kb = InlineKeyboardBuilder()
        kb.button(text="🔙 Назад", callback_data="adm_back")
        try:
            await get_web_client(force_reinit=True)
            await call.message.edit_text("✅ <b>Веб-сессия Gemini Pro переподключена!</b>", parse_mode="HTML", reply_markup=kb.as_markup())
        except Exception as e:
            await call.message.edit_text(f"⚠️ Ошибка: <code>{html.escape(str(e))}</code>", parse_mode="HTML", reply_markup=kb.as_markup())

    @d.callback_query(F.data.startswith("revoke:"))
    async def cb_revoke(call: types.CallbackQuery):
        if not is_admin(call.from_user.id, call.from_user.username): return
        target_key = call.data.split(":", 1)[1]
        await revoke_user_by_key(bot_inst, target_key)
        await call.answer("Доступ удалён!")
        text, markup = build_admin_panel_view()
        await call.message.edit_text(text, parse_mode="HTML", reply_markup=markup)

    @d.message(F.text == "Новый чат")
    @d.message(Command("clear"))
    async def clear_cmd(message: types.Message):
        uid = message.from_user.id
        if not check_access(message.from_user): return
        await delete_old_web_chat(uid)
        user_history[uid] = []
        await message.answer("Старый чат удалён с сайта, начат новый.", reply_markup=get_main_reply_kb(uid, message.from_user.username))

    @d.message(F.text == "Модель")
    async def menu_models(message: types.Message):
        uid = message.from_user.id
        if not check_access(message.from_user): return
        cur = get_prefs(uid)["model"]
        kb = InlineKeyboardBuilder()
        for label, code in MODELS_LIST:
            kb.button(text=f"{'✓ ' if cur == code else ''}{label}", callback_data=f"model:{code}:{label}")
        kb.adjust(1)
        await message.answer("Модель:", reply_markup=kb.as_markup())

    @d.callback_query(F.data.startswith("model:"))
    async def cb_model(call: types.CallbackQuery):
        uid = call.from_user.id
        if not check_access(call.from_user): return
        _, code, label = call.data.split(":", 2)
        prefs = get_prefs(uid)
        prefs["model"], prefs["model_label"] = code, label
        try:
            client = await get_web_client()
            target_model = resolve_model_clean(client, code)
            if uid in web_chats and target_model: web_chats[uid].model = target_model
        except Exception: pass
        await call.answer()
        await call.message.edit_text(f"Модель: <b>{label}</b>", parse_mode="HTML")

    @d.message(F.text == "Уровень рассуждений")
    async def menu_reasoning(message: types.Message):
        if not check_access(message.from_user): return
        cur = get_prefs(message.from_user.id)["reasoning"]
        kb = InlineKeyboardBuilder()
        for label, code in REASONING_LIST:
            kb.button(text=f"{'✓ ' if cur == code else ''}{label}", callback_data=f"reason:{code}")
        kb.adjust(1)
        await message.answer("Уровень рассуждений:", reply_markup=kb.as_markup())

    @d.callback_query(F.data.startswith("reason:"))
    async def cb_reason(call: types.CallbackQuery):
        uid = call.from_user.id
        if not check_access(call.from_user): return
        code = call.data.split(":", 1)[1]
        prefs = get_prefs(uid)
        prefs["reasoning"] = code
        label = dict((v, k) for k, v in REASONING_LIST).get(code, code)
        await call.answer()
        await call.message.edit_text(f"Уровень рассуждений: <b>{label}</b>", parse_mode="HTML")

    @d.message(F.text == "Инструменты")
    async def menu_tools(message: types.Message):
        uid = message.from_user.id
        if not check_access(message.from_user): return
        cur = get_prefs(uid)["tool"]
        kb = InlineKeyboardBuilder()
        for label, code in TOOLS_LIST:
            kb.button(text=f"{'✓ ' if cur == code else ''}{label}", callback_data=f"tool:{code}")
        kb.adjust(1)
        await message.answer("Инструменты:", reply_markup=kb.as_markup())

    @d.callback_query(F.data.startswith("tool:"))
    async def cb_tool(call: types.CallbackQuery):
        uid = call.from_user.id
        if not check_access(call.from_user): return
        code = call.data.split(":", 1)[1]
        get_prefs(uid)["tool"] = code
        label = dict((v, k) for k, v in TOOLS_LIST).get(code, code)
        await call.answer()
        await call.message.edit_text(f"Инструмент: <b>{label}</b>", parse_mode="HTML")

    @d.message(F.text)
    async def handle_text(message: types.Message):
        uid, uname = message.from_user.id, message.from_user.username
        if not check_access(message.from_user):
            await message.answer("🔒 Доступ закрыт. Отправь заявку владельцу:", reply_markup=get_locked_kb())
            return

        if is_admin(uid, uname) and uid in admin_input_state:
            mode = admin_input_state.pop(uid)
            val = message.text.strip()
            if mode == "wait_add_user_target":
                clean_target = val.lstrip("@").lower().strip()
                await message.answer(f"⏳ Выбери срок доступа для <b>@{html.escape(clean_target)}</b>:", parse_mode="HTML", reply_markup=build_duration_picker_kb(clean_target))
                return
            elif mode == "wait_custom_duration":
                target_key = pending_grant_target.pop(uid, None)
                if not target_key:
                    await message.answer("⚠️ Цель не выбрана.", reply_markup=get_main_reply_kb(uid, uname))
                    return
                seconds, _ = parse_custom_duration(val)
                if seconds < 0:
                    admin_input_state[uid] = "wait_custom_duration"
                    pending_grant_target[uid] = target_key
                    await message.answer("⚠️ Напиши время в формате: <code>1 час 30 минут</code>, <code>33 часа</code> или <code>45 минут</code>:", parse_mode="HTML")
                    return
                res_msg = await apply_user_grant(bot_inst, target_key, seconds)
                await message.answer(res_msg, parse_mode="HTML", reply_markup=get_main_reply_kb(uid, uname))
                return
            elif mode == "wait_del_user_target":
                res_msg = await revoke_user_by_key(bot_inst, val)
                await message.answer(res_msg, parse_mode="HTML", reply_markup=get_main_reply_kb(uid, uname))
                return
            elif mode == "wait_add_admin":
                clean_u = val.lstrip("@").lower()
                if clean_u.isdigit():
                    if int(clean_u) not in access_db.setdefault("admins", []): access_db["admins"].append(int(clean_u))
                else:
                    if clean_u not in access_db.setdefault("admin_usernames", []): access_db["admin_usernames"].append(clean_u)
                save_access_db(access_db)
                await message.answer(f"👑 Пользователь <b>@{clean_u}</b> назначен Администратором!", parse_mode="HTML", reply_markup=get_main_reply_kb(uid, uname))
                return
            elif mode == "wait_broadcast":
                sent = 0
                for target_u in list(access_db.get("users", {}).keys()):
                    try:
                        await bot_inst.send_message(int(target_u), f"📢 <b>Объявление:</b>\n\n{html.escape(val)}", parse_mode="HTML")
                        sent += 1
                    except Exception: pass
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
                    await message.answer(f"⚠️ Ошибка куков: <code>{html.escape(str(e))}</code>", parse_mode="HTML")
                return

        async with continuous_typing(bot_inst, message.chat.id, "typing"):
            try:
                await ask_gemini_web(bot_inst, message, message.text)
            except Exception as web_err:
                stats_store["last_err"] = f"{type(web_err).__name__}: {web_err}"
                prefs = get_prefs(uid)
                if is_image_intent(message.text, prefs["tool"]):
                    if await generate_guaranteed_image(message, message.text):
                        return
                try:
                    ans, _ = await ask_gemini_fallback(uid, message.text, message.text)
                    # Пользователи видят ТОЛЬКО чистый ответ без надписей про Запасной API!
                    await safe_send(message, ans)
                except Exception as e:
                    await message.answer("⏳ Сервер обрабатывает запросы, попробуй ещё раз через пару секунд.", reply_markup=get_main_reply_kb(uid, uname))

    @d.message(F.photo)
    async def handle_photo(message: types.Message):
        uid, uname = message.from_user.id, message.from_user.username
        if not check_access(message.from_user):
            await message.answer("🔒 Доступ закрыт.", reply_markup=get_locked_kb())
            return
        temp_path = f"/tmp/photo_{uid}.jpg"
        async with continuous_typing(bot_inst, message.chat.id, "typing"):
            try:
                photo = message.photo[-1]
                file_io = io.BytesIO()
                await bot_inst.download(photo, destination=file_io)
                file_io.seek(0)
                with open(temp_path, "wb") as f: f.write(file_io.getvalue())
                caption = message.caption or "Реши или опиши фото."
                try:
                    await ask_gemini_web(bot_inst, message, caption, file_path=temp_path)
                except Exception as web_err:
                    stats_store["last_err"] = f"{type(web_err).__name__}: {web_err}"
                    image = Image.open(file_io)
                    image.load()
                    ans, _ = await ask_gemini_fallback(uid, [caption, image], f"[Фото] {caption}")
                    await safe_send(message, ans)
            except Exception:
                await message.answer("⏳ Не удалось обработать фото, попробуй ещё раз.", reply_markup=get_main_reply_kb(uid, uname))
            finally:
                if os.path.exists(temp_path): os.remove(temp_path)

    @d.message(F.document)
    async def handle_document(message: types.Message):
        uid, uname = message.from_user.id, message.from_user.username
        if not check_access(message.from_user):
            await message.answer("🔒 Доступ закрыт.", reply_markup=get_locked_kb())
            return
        doc = message.document
        temp_path = os.path.join("/tmp", doc.file_name or f"file_{uid}")
        async with continuous_typing(bot_inst, message.chat.id, "typing"):
            try:
                file_io = io.BytesIO()
                await bot_inst.download(doc, destination=file_io)
                with open(temp_path, "wb") as f: f.write(file_io.getvalue())
                await ask_gemini_web(bot_inst, message, message.caption or "Проанализируй файл.", file_path=temp_path)
            except Exception as e:
                await message.answer(f"Ошибка файла: {e}", reply_markup=get_main_reply_kb(uid, uname))
            finally:
                if os.path.exists(temp_path): os.remove(temp_path)

    return d

@st.cache_resource
def launch_bot_v42():
    gen_id = time.time()
    sys._active_tg_gen = gen_id
    old_state = getattr(sys, "_active_tg_bot", None)
    if old_state:
        try:
            asyncio.run_coroutine_threadsafe(old_state[1].stop_polling(), old_state[0])
            if len(old_state) > 2 and old_state[2] is not None:
                asyncio.run_coroutine_threadsafe(old_state[2].session.close(), old_state[0])
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

launch_bot_v42()

st.title("Gemini Bot")
st.success("Активен (@targetturtle)")
