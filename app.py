import os
import sys
import io
import re
import json
import time
import subprocess
import asyncio
import threading
from datetime import datetime, timezone, timedelta
from PIL import Image
import streamlit as st

try:
    from gemini_webapi import GeminiClient
except ImportError:
    subprocess.check_call([sys.executable, "-m", "pip", "install", "-U", "gemini-webapi"])
    from gemini_webapi import GeminiClient

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
MASTER_PIN = str(get_secret("MASTER_PIN", "322"))

ACCESS_FILE = "access_db.json"

def load_access_db():
    if os.path.exists(ACCESS_FILE):
        try:
            with open(ACCESS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"admin_id": None, "users": {}}

def save_access_db(db):
    try:
        with open(ACCESS_FILE, "w", encoding="utf-8") as f:
            json.dump(db, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

access_db = load_access_db()

def is_admin(uid: int) -> bool:
    return access_db.get("admin_id") == uid

def check_access(uid: int) -> bool:
    if is_admin(uid):
        return True
    u_info = access_db.get("users", {}).get(str(uid))
    if not u_info:
        return False
    exp = u_info.get("expires_at", 0)
    if exp == 0:  # 0 = Навсегда
        return True
    if time.time() < exp:
        return True
    # Срок вышел — удаляем
    access_db["users"].pop(str(uid), None)
    save_access_db(access_db)
    return False

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

def get_main_reply_kb(uid: int = 0):
    kb = ReplyKeyboardBuilder()
    kb.button(text="Модель")
    kb.button(text="Уровень рассуждений")
    kb.button(text="Инструменты")
    kb.button(text="Новый чат")
    if is_admin(uid):
        kb.button(text="👑 Админка")
        kb.adjust(2, 2, 1)
    else:
        kb.adjust(2, 2)
    return kb.as_markup(resize_keyboard=True)

def get_locked_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="📩 Запросить доступ", callback_data="req_access")
    return kb.as_markup()

async def get_web_client():
    global web_client
    if not SECURE_1PSID:
        raise Exception("В Secrets не указан SECURE_1PSID")
    async with web_init_lock:
        if web_client is None:
            client = GeminiClient(SECURE_1PSID.strip(), SECURE_1PSIDTS.strip() if SECURE_1PSIDTS else None)
            await client.init(timeout=30, auto_refresh=True)
            web_client = client
    return web_client

def resolve_clean_model(client, model_code: str):
    code = model_code.lower()
    if hasattr(client, "list_models"):
        try:
            models = client.list_models() or []
            for m in models:
                d_name = str(getattr(m, "display_name", "")).lower()
                m_name = str(getattr(m, "model_name", "")).lower()
                if code == "flash-lite" and "lite" in (d_name + m_name):
                    return m
                if code == "argon" and ("argon" in (d_name + m_name) or "4" in d_name):
                    return m
                if code == "pro" and "pro" in (d_name + m_name):
                    return m
                if code == "flash" and "flash" in (d_name + m_name) and "lite" not in (d_name + m_name):
                    return m
        except Exception:
            pass

    if hasattr(client, "resolve_model"):
        for candidate in ([code, "pro"] if code == "argon" else [code]):
            try:
                return client.resolve_model(candidate)
            except Exception:
                continue
    return None

async def safe_send(message: types.Message, text: str):
    if not text:
        return
    uid = message.from_user.id

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
        await message.answer_document(doc, reply_markup=get_main_reply_kb(uid))
        return

    max_len = 4000
    for i in range(0, len(text), max_len):
        part = text[i:i + max_len]
        try:
            await message.answer(part, parse_mode="Markdown", reply_markup=get_main_reply_kb(uid))
        except Exception:
            await message.answer(part, parse_mode=None, reply_markup=get_main_reply_kb(uid))

async def ask_gemini_web(bot_inst: Bot, message: types.Message, prompt: str, file_path: str = None):
    client = await get_web_client()
    uid = message.from_user.id
    prefs = get_prefs(uid)

    target_model = resolve_clean_model(client, prefs["model"])

    if uid not in web_chats:
        web_chats[uid] = client.start_chat(model=target_model) if target_model else client.start_chat()
    else:
        old_chat = web_chats[uid]
        if target_model:
            try:
                web_chats[uid] = client.start_chat(model=target_model, metadata=old_chat.metadata)
            except Exception:
                old_chat.model = target_model

    chat = web_chats[uid]
    tool = prefs["tool"]

    clean_prompt = prompt
    if tool == "image":
        clean_prompt = f"Создай или отредактируй изображение: {prompt}"
    elif tool == "video":
        clean_prompt = f"Создай видео: {prompt}"
    elif tool == "music":
        clean_prompt = f"Создай музыку: {prompt}"

    files = [file_path] if file_path else None
    response = await asyncio.wait_for(chat.send_message(clean_prompt, files=files), timeout=45)

    if getattr(response, "images", None):
        await bot_inst.send_chat_action(chat_id=message.chat.id, action="upload_photo")
        for idx, img in enumerate(response.images):
            out_name = f"img_{uid}_{idx}.png"
            out_path = os.path.join("/tmp", out_name)
            await img.save(path="/tmp", filename=out_name)
            if os.path.exists(out_path):
                with open(out_path, "rb") as f:
                    photo_file = types.BufferedInputFile(f.read(), filename=out_name)
                    await message.answer_photo(photo_file, reply_markup=get_main_reply_kb(uid))
                os.remove(out_path)

    if getattr(response, "text", None):
        await safe_send(message, response.text)

async def ask_gemini_fallback(user_id: int, new_content, history_text: str):
    global working_api_key
    tz_msk = timezone(timedelta(hours=3))
    current_time = datetime.now(tz_msk).strftime("%d.%m.%Y, %H:%M")
    history = user_history.get(user_id, [])
    full_contents = list(history) + [new_content]
    prefs = get_prefs(user_id)

    api_models = ["gemini-3.5-flash-lite", "gemini-3.8-flash"] if prefs["model"] == "flash-lite" else ["gemini-3.8-flash", "gemini-3.5-flash-lite"]
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
                    contents=full_contents,
                    config=gtypes.GenerateContentConfig(
                        system_instruction=(
                            f"Сейчас (МСК): {current_time}. "
                            "Пиши математику без $ и без LaTeX (x², √, /). "
                            "Если просят скинуть файлом — начни первую строку с SEND_FILE:имя_файла.расширение"
                        )
                    ),
                )
                working_api_key = key
                ans = response.text or "Пустой ответ."
                history.append(f"Пользователь: {history_text}")
                history.append(f"Ассистент: {ans}")
                user_history[user_id] = history[-14:]
                return ans, model_name
            except Exception as e:
                last_err = e
                continue
    raise Exception(f"Запасные ключи не сработали: {last_err}")

def create_dispatcher(bot_inst: Bot):
    d = Dispatcher()

    # Секретная команда активации Владельца: /owner 322
    @d.message(Command("owner"))
    async def claim_owner(message: types.Message):
        parts = message.text.split()
        if len(parts) > 1 and parts[1].strip() == MASTER_PIN:
            access_db["admin_id"] = message.from_user.id
            save_access_db(access_db)
            await message.answer(
                "👑 **Ты назначен Главным Владельцем бота!**\n"
                "Теперь у тебя есть кнопка «👑 Админка», а все посторонние заблокированы.",
                parse_mode="Markdown",
                reply_markup=get_main_reply_kb(message.from_user.id)
            )

    @d.message(CommandStart())
    async def start_cmd(message: types.Message):
        uid = message.from_user.id
        if not check_access(uid):
            await message.answer(
                "🔒 **Доступ к этому боту закрыт.**\n\n"
                "Нажми кнопку ниже, чтобы отправить заявку владельцу:",
                parse_mode="Markdown",
                reply_markup=get_locked_kb()
            )
            return

        web_chats.pop(uid, None)
        prefs = get_prefs(uid)
        r_label = dict((v, k) for k, v in REASONING_LIST).get(prefs["reasoning"], prefs["reasoning"])
        await message.answer(
            f"Модель: *{prefs['model_label']}*\n"
            f"Уровень рассуждений: *{r_label}*",
            parse_mode="Markdown",
            reply_markup=get_main_reply_kb(uid)
        )

    # Заявка на доступ от нового человека
    @d.callback_query(F.data == "req_access")
    async def cb_req_access(call: types.CallbackQuery):
        uid = call.from_user.id
        if check_access(uid):
            await call.answer("У тебя уже есть доступ!")
            return

        admin_id = access_db.get("admin_id")
        if not admin_id:
            await call.answer("Владелец ещё не активировал админку (/owner 322).", show_alert=True)
            return

        u_name = call.from_user.full_name or "Без имени"
        u_nick = f"@{call.from_user.username}" if call.from_user.username else "без юзернейма"

        kb = InlineKeyboardBuilder()
        kb.button(text="⏱ На 1 час (Урок)", callback_data=f"grant:{uid}:3600")
        kb.button(text="📅 На 1 день (24ч)", callback_data=f"grant:{uid}:86400")
        kb.button(text="♾ Навсегда", callback_data=f"grant:{uid}:0")
        kb.button(text="❌ Отказать", callback_data=f"deny:{uid}")
        kb.adjust(2, 1, 1)

        await bot_inst.send_message(
            admin_id,
            f"🔔 **Заявка на доступ!**\n"
            f"👤 Имя: {u_name} ({u_nick})\n"
            f"🆔 ID: `{uid}`",
            parse_mode="Markdown",
            reply_markup=kb.as_markup()
        )
        await call.answer("Заявка отправлена владельцу!")
        await call.message.edit_text("⏳ **Заявка отправлена!** Ожидай подтверждения от владельца.", parse_mode="Markdown")

    # Выдача доступа админом
    @d.callback_query(F.data.startswith("grant:"))
    async def cb_grant(call: types.CallbackQuery):
        if not is_admin(call.from_user.id):
            return
        _, target_uid_str, seconds_str = call.data.split(":")
        seconds = int(seconds_str)
        expires_at = (time.time() + seconds) if seconds > 0 else 0

        try:
            chat_info = await bot_inst.get_chat(int(target_uid_str))
            name_label = f"{chat_info.full_name} (@{chat_info.username})" if chat_info.username else chat_info.full_name
        except Exception:
            name_label = f"User {target_uid_str}"

        access_db.setdefault("users", {})[target_uid_str] = {
            "name": name_label,
            "expires_at": expires_at,
        }
        save_access_db(access_db)

        period_text = "на 1 час" if seconds == 3600 else ("на 24 часа" if seconds == 86400 else "навсегда")
        await call.answer("Доступ выдан!")
        await call.message.edit_text(f"✅ Доступ для **{name_label}** (`{target_uid_str}`) открыт **{period_text}**!", parse_mode="Markdown")

        try:
            await bot_inst.send_message(
                int(target_uid_str),
                f"🎉 **Тебе открыт доступ к боту ({period_text})!**\nМожешь писать вопросы или скидывать фото задач 👇",
                parse_mode="Markdown",
                reply_markup=get_main_reply_kb(int(target_uid_str))
            )
        except Exception:
            pass

    @d.callback_query(F.data.startswith("deny:"))
    async def cb_deny(call: types.CallbackQuery):
        if not is_admin(call.from_user.id):
            return
        target_uid = int(call.data.split(":")[1])
        await call.answer("Отклонено")
        await call.message.edit_text(f"❌ Заявка `{target_uid}` отклонена.", parse_mode="Markdown")
        try:
            await bot_inst.send_message(target_uid, "❌ Владелец отклонил заявку на доступ.")
        except Exception:
            pass

    # Панель управления клиентами
    @d.message(F.text == "👑 Админка")
    async def admin_panel(message: types.Message):
        if not is_admin(message.from_user.id):
            return
        users = access_db.get("users", {})
        if not users:
            await message.answer("👑 **Админ-панель**\n\nСейчас активных клиентов нет.", parse_mode="Markdown")
            return

        kb = InlineKeyboardBuilder()
        lines = ["👑 **Активные пользователи:**\n"]
        now = time.time()
        for uid_str, info in list(users.items()):
            exp = info.get("expires_at", 0)
            if exp != 0 and now >= exp:
                users.pop(uid_str, None)
                continue
            rem = "♾ Навсегда" if exp == 0 else f"⏳ Осталось: {int((exp - now) // 60)} мин."
            lines.append(f"• **{info.get('name', uid_str)}** — {rem}")
            kb.button(text=f"❌ Забрать у {info.get('name', uid_str)[:18]}", callback_data=f"revoke:{uid_str}")

        save_access_db(access_db)
        kb.adjust(1)
        await message.answer("\n".join(lines), parse_mode="Markdown", reply_markup=kb.as_markup())

    @d.callback_query(F.data.startswith("revoke:"))
    async def cb_revoke(call: types.CallbackQuery):
        if not is_admin(call.from_user.id):
            return
        uid_str = call.data.split(":")[1]
        removed = access_db.get("users", {}).pop(uid_str, None)
        save_access_db(access_db)
        await call.answer("Доступ закрыт!")
        await call.message.edit_text(f"🔒 Доступ у пользователя `{uid_str}` отозван.", parse_mode="Markdown")
        try:
            await bot_inst.send_message(int(uid_str), "🔒 Твой доступ к боту завершён.")
        except Exception:
            pass

    # Стандартные кнопки меню
    @d.message(F.text == "Новый чат")
    @d.message(Command("clear"))
    async def clear_cmd(message: types.Message):
        if not check_access(message.from_user.id):
            return
        web_chats.pop(message.from_user.id, None)
        user_history[message.from_user.id] = []
        await message.answer("Новый чат начат.", reply_markup=get_main_reply_kb(message.from_user.id))

    @d.message(F.text == "Модель")
    async def menu_models(message: types.Message):
        if not check_access(message.from_user.id):
            return
        cur = get_prefs(message.from_user.id)["model"]
        kb = InlineKeyboardBuilder()
        for label, code in MODELS_LIST:
            mark = "✓ " if cur == code else ""
            kb.button(text=f"{mark}{label}", callback_data=f"model:{code}:{label}")
        kb.adjust(1)
        await message.answer("Модель:", reply_markup=kb.as_markup())

    @d.callback_query(F.data.startswith("model:"))
    async def cb_model(call: types.CallbackQuery):
        if not check_access(call.from_user.id):
            return
        _, code, label = call.data.split(":", 2)
        prefs = get_prefs(call.from_user.id)
        prefs["model"] = code
        prefs["model_label"] = label
        await call.answer()
        await call.message.edit_text(f"Модель: *{label}*", parse_mode="Markdown")

    @d.message(F.text == "Уровень рассуждений")
    async def menu_reasoning(message: types.Message):
        if not check_access(message.from_user.id):
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
        if not check_access(call.from_user.id):
            return
        code = call.data.split(":", 1)[1]
        get_prefs(call.from_user.id)["reasoning"] = code
        label = dict((v, k) for k, v in REASONING_LIST).get(code, code)
        await call.answer()
        await call.message.edit_text(f"Уровень рассуждений: *{label}*", parse_mode="Markdown")

    @d.message(F.text == "Инструменты")
    async def menu_tools(message: types.Message):
        if not check_access(message.from_user.id):
            return
        cur = get_prefs(message.from_user.id)["tool"]
        kb = InlineKeyboardBuilder()
        for label, code in TOOLS_LIST:
            mark = "✓ " if cur == code else ""
            kb.button(text=f"{mark}{label}", callback_data=f"tool:{code}")
        kb.adjust(1)
        await message.answer("Инструменты:", reply_markup=kb.as_markup())

    @d.callback_query(F.data.startswith("tool:"))
    async def cb_tool(call: types.CallbackQuery):
        if not check_access(call.from_user.id):
            return
        code = call.data.split(":", 1)[1]
        get_prefs(call.from_user.id)["tool"] = code
        label = dict((v, k) for k, v in TOOLS_LIST).get(code, code)
        await call.answer()
        await call.message.edit_text(f"Инструмент: *{label}*", parse_mode="Markdown")

    @d.message(F.text)
    async def handle_text(message: types.Message):
        uid = message.from_user.id
        if not check_access(uid):
            await message.answer("🔒 Доступ закрыт. Отправь заявку владельцу:", reply_markup=get_locked_kb())
            return

        await bot_inst.send_chat_action(chat_id=message.chat.id, action="typing")
        try:
            await ask_gemini_web(bot_inst, message, message.text)
        except Exception as web_err:
            try:
                ans, used_model = await ask_gemini_fallback(uid, message.text, message.text)
                warn = f"⚠️ [Запасной API ({used_model}) | Веб: {web_err}]\n\n"
                await safe_send(message, warn + ans)
            except Exception as e:
                await message.answer(f"Ошибка: {e}", reply_markup=get_main_reply_kb(uid))

    @d.message(F.photo)
    async def handle_photo(message: types.Message):
        uid = message.from_user.id
        if not check_access(uid):
            await message.answer("🔒 Доступ закрыт. Отправь заявку владельцу:", reply_markup=get_locked_kb())
            return

        await bot_inst.send_chat_action(chat_id=message.chat.id, action="typing")
        temp_path = f"/tmp/photo_{uid}.jpg"
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
                image = Image.open(file_io)
                image.load()
                ans, used_model = await ask_gemini_fallback(uid, [caption, image], f"[Фото] {caption}")
                warn = f"⚠️ [Запасной API ({used_model}) | Веб: {web_err}]\n\n"
                await safe_send(message, warn + ans)
        except Exception as e:
            await message.answer(f"Ошибка: {e}", reply_markup=get_main_reply_kb(uid))
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)

    @d.message(F.document)
    async def handle_document(message: types.Message):
        uid = message.from_user.id
        if not check_access(uid):
            await message.answer("🔒 Доступ закрыт. Отправь заявку владельцу:", reply_markup=get_locked_kb())
            return

        await bot_inst.send_chat_action(chat_id=message.chat.id, action="typing")
        doc = message.document
        temp_path = os.path.join("/tmp", doc.file_name or f"file_{uid}")
        try:
            file_io = io.BytesIO()
            await bot_inst.download(doc, destination=file_io)
            with open(temp_path, "wb") as f:
                f.write(file_io.getvalue())

            caption = message.caption or "Проанализируй файл."
            await ask_gemini_web(bot_inst, message, caption, file_path=temp_path)
        except Exception as e:
            await message.answer(f"Ошибка файла: {e}", reply_markup=get_main_reply_kb(uid))
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)

    return d

@st.cache_resource
def launch_bot_v12():
    old_state = getattr(sys, "_active_tg_bot", None)
    if old_state:
        old_loop, old_dp = old_state
        try:
            old_loop.call_soon_threadsafe(old_dp.stop_polling)
        except Exception:
            pass
        time.sleep(1)

    gen_id = time.time()
    sys._active_tg_gen = gen_id

    def worker():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        bot_inst = Bot(token=TELEGRAM_TOKEN)
        dp_inst = create_dispatcher(bot_inst)
        sys._active_tg_bot = (loop, dp_inst)

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

launch_bot_v12()

st.title("Gemini Bot")
st.success("Активен")
