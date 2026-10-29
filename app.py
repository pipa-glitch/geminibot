import os
import sys
import io
import re
import json
import uuid
import time
import copy
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

MODELS_LIST = [
    ("3.5 Flash-Lite", "3.5 Flash-Lite"),
    ("3.8 Flash", "3.8 Flash"),
    ("3.1 Pro", "3.1 Pro"),
    ("4 Argon", "4 Argon"),
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

# Внутренние аппаратные хэши моделей веб-интерфейса Gemini (из протокола Google)
WEB_MODEL_HASHES = {
    "3.5 Flash-Lite": ("8c46e95b1a07cecc", 6),
    "3.8 Flash": ("56fdd199312815e2", 1),
    "3.1 Pro": ("e6fa609c3fa255c0", 3),
}

web_client = None
web_init_lock = asyncio.Lock()
web_chats = {}
user_history = {}
user_prefs = {}
working_api_key = None

def get_prefs(uid: int):
    if uid not in user_prefs:
        user_prefs[uid] = {
            "model": "3.8 Flash",  # Быстрая модель по умолчанию (отвечает за 2-3 сек)
            "reasoning": "low",    # Низкий уровень рассуждений по умолчанию (без долгих зависаний)
            "tool": "none",
        }
    return user_prefs[uid]

def get_main_reply_kb():
    kb = ReplyKeyboardBuilder()
    kb.button(text="Модель")
    kb.button(text="Уровень рассуждений")
    kb.button(text="Инструменты")
    kb.button(text="Новый чат")
    kb.adjust(2, 2)
    return kb.as_markup(resize_keyboard=True)

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

def build_web_model(client, wanted_name: str, reasoning_level: str):
    """Подбирает реальную модель аккаунта и модифицирует аппаратный заголовок x-goog-ext-525001261-jspb"""
    w = wanted_name.lower()
    r_slot = 1 if reasoning_level == "low" else 2  # Слот 15 в заголовке Google отвечает за глубину мышления

    matched_model = None
    if hasattr(client, "list_models"):
        try:
            models = client.list_models() or []
            # Точный порядок проверки: сначала lite, потом argon, потом pro, потом flash
            for m in models:
                d_name = str(getattr(m, "display_name", "")).lower()
                m_name = str(getattr(m, "model_name", "")).lower()
                hdr = str(getattr(m, "model_header", "")).lower()
                if "lite" in w and ("lite" in d_name or "lite" in m_name or "8c46e95b1a07cecc" in hdr):
                    matched_model = m
                    break
                elif "argon" in w and ("argon" in d_name or "argon" in m_name or "4" in d_name):
                    matched_model = m
                    break
                elif "pro" in w and ("pro" in d_name or "pro" in m_name or "e6fa609c3fa255c0" in hdr):
                    matched_model = m
                    break
                elif "flash" in w and "lite" not in w and ("flash" in d_name or "flash" in m_name or "56fdd199312815e2" in hdr) and "lite" not in m_name:
                    matched_model = m
                    break
        except Exception:
            pass

    if matched_model is None and hasattr(client, "resolve_model"):
        try:
            if "lite" in w:
                matched_model = client.resolve_model("flash-lite")
            elif "pro" in w or "argon" in w:
                matched_model = client.resolve_model("pro")
            else:
                matched_model = client.resolve_model("flash")
        except Exception:
            pass

    # Модифицируем аппаратный заголовок x-goog-ext-525001261-jspb под выбранный уровень рассуждений
    if matched_model is not None:
        try:
            m_copy = copy.deepcopy(matched_model)
            hdr_dict = getattr(m_copy, "model_header", None)
            if isinstance(hdr_dict, dict) and "x-goog-ext-525001261-jspb" in hdr_dict:
                arr = json.loads(hdr_dict["x-goog-ext-525001261-jspb"])
                if isinstance(arr, list) and len(arr) > 15:
                    arr[15] = r_slot
                    hdr_dict["x-goog-ext-525001261-jspb"] = json.dumps(arr, separators=(",", ":"))
            return m_copy
        except Exception:
            return matched_model

    # Если модель не нашлась в list_models — собираем прямой аппаратный заголовок по хэшу
    if wanted_name in WEB_MODEL_HASHES:
        m_hash, cat = WEB_MODEL_HASHES[wanted_name]
        req_uuid = str(uuid.uuid4()).upper()
        jspb = f'[1,null,null,null,"{m_hash}",null,null,0,[4,5,6,8],null,null,2,null,null,{cat},{r_slot},"{req_uuid}"]'
        return {
            "model_name": wanted_name,
            "model_header": {"x-goog-ext-525001261-jspb": jspb}
        }

    return None

async def safe_send(message: types.Message, text: str):
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
        await message.answer_document(doc, reply_markup=get_main_reply_kb())
        return

    max_len = 4000
    for i in range(0, len(text), max_len):
        part = text[i:i + max_len]
        try:
            await message.answer(part, parse_mode="Markdown", reply_markup=get_main_reply_kb())
        except Exception:
            await message.answer(part, parse_mode=None, reply_markup=get_main_reply_kb())

async def ask_gemini_web(bot_inst: Bot, message: types.Message, prompt: str, file_path: str = None):
    client = await get_web_client()
    uid = message.from_user.id
    prefs = get_prefs(uid)

    target_model = build_web_model(client, prefs["model"], prefs["reasoning"])

    if uid not in web_chats:
        try:
            web_chats[uid] = client.start_chat(model=target_model) if target_model else client.start_chat()
        except Exception:
            web_chats[uid] = client.start_chat()
    else:
        old_chat = web_chats[uid]
        if target_model:
            try:
                web_chats[uid] = client.start_chat(model=target_model, metadata=old_chat.metadata)
            except Exception:
                old_chat.model = target_model

    chat = web_chats[uid]
    tool = prefs["tool"]

    # Отправляем чистый запрос (без мусорных приписок в чате)
    send_kwargs = {}
    if file_path:
        send_kwargs["files"] = [file_path]
    if tool == "deep_research":
        send_kwargs["deep_research"] = True

    clean_prompt = prompt
    if tool == "image":
        clean_prompt = f"Создай или отредактируй изображение: {prompt}"
    elif tool == "video":
        clean_prompt = f"Создай видео: {prompt}"
    elif tool == "music":
        clean_prompt = f"Создай музыку: {prompt}"

    try:
        response = await chat.send_message(clean_prompt, **send_kwargs)
    except TypeError:
        # На случай если версия gemini-webapi не принимает флаг deep_research
        response = await chat.send_message(clean_prompt, files=[file_path] if file_path else None)

    if getattr(response, "images", None):
        await bot_inst.send_chat_action(chat_id=message.chat.id, action="upload_photo")
        for idx, img in enumerate(response.images):
            out_name = f"img_{uid}_{idx}.png"
            out_path = os.path.join("/tmp", out_name)
            await img.save(path="/tmp", filename=out_name)
            if os.path.exists(out_path):
                with open(out_path, "rb") as f:
                    photo_file = types.BufferedInputFile(f.read(), filename=out_name)
                    await message.answer_photo(photo_file, reply_markup=get_main_reply_kb())
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

    # Ставим быструю модель первой, если выбрана 3.5, иначе 3.8
    if "3.5" in prefs["model"]:
        api_models = ["gemini-3.5-flash-lite", "gemini-3.8-flash"]
    else:
        api_models = ["gemini-3.8-flash", "gemini-3.5-flash-lite"]

    # Сначала пробуем уже проверенный рабочий ключ, чтобы не тратить время на мертвые
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
    raise Exception(f"Запасные ключи тоже не сработали: {last_err}")

# ================= ДИСПЕТЧЕР =================

def create_dispatcher(bot_inst: Bot):
    d = Dispatcher()

    @d.message(CommandStart())
    async def start_cmd(message: types.Message):
        uid = message.from_user.id
        web_chats.pop(uid, None)
        prefs = get_prefs(uid)
        r_label = dict((v, k) for k, v in REASONING_LIST).get(prefs["reasoning"], prefs["reasoning"])
        await message.answer(
            f"Модель: *{prefs['model']}*\n"
            f"Уровень рассуждений: *{r_label}*",
            parse_mode="Markdown",
            reply_markup=get_main_reply_kb()
        )

    @d.message(F.text == "Новый чат")
    @d.message(Command("clear"))
    async def clear_cmd(message: types.Message):
        web_chats.pop(message.from_user.id, None)
        user_history[message.from_user.id] = []
        await message.answer("Новый чат начат.", reply_markup=get_main_reply_kb())

    @d.message(F.text == "Модель")
    async def menu_models(message: types.Message):
        cur = get_prefs(message.from_user.id)["model"]
        kb = InlineKeyboardBuilder()
        for label, code in MODELS_LIST:
            mark = "✓ " if cur == code else ""
            kb.button(text=f"{mark}{label}", callback_data=f"model:{code}")
        kb.adjust(1)
        await message.answer("Модель:", reply_markup=kb.as_markup())

    @d.callback_query(F.data.startswith("model:"))
    async def cb_model(call: types.CallbackQuery):
        code = call.data.split(":", 1)[1]
        get_prefs(call.from_user.id)["model"] = code
        await call.answer()
        await call.message.edit_text(f"Модель: *{code}*", parse_mode="Markdown")

    @d.message(F.text == "Уровень рассуждений")
    async def menu_reasoning(message: types.Message):
        cur = get_prefs(message.from_user.id)["reasoning"]
        kb = InlineKeyboardBuilder()
        for label, code in REASONING_LIST:
            mark = "✓ " if cur == code else ""
            kb.button(text=f"{mark}{label}", callback_data=f"reason:{code}")
        kb.adjust(1)
        await message.answer("Уровень рассуждений:", reply_markup=kb.as_markup())

    @d.callback_query(F.data.startswith("reason:"))
    async def cb_reason(call: types.CallbackQuery):
        code = call.data.split(":", 1)[1]
        get_prefs(call.from_user.id)["reasoning"] = code
        label = dict((v, k) for k, v in REASONING_LIST).get(code, code)
        await call.answer()
        await call.message.edit_text(f"Уровень рассуждений: *{label}*", parse_mode="Markdown")

    @d.message(F.text == "Инструменты")
    async def menu_tools(message: types.Message):
        cur = get_prefs(message.from_user.id)["tool"]
        kb = InlineKeyboardBuilder()
        for label, code in TOOLS_LIST:
            mark = "✓ " if cur == code else ""
            kb.button(text=f"{mark}{label}", callback_data=f"tool:{code}")
        kb.adjust(1)
        await message.answer("Инструменты:", reply_markup=kb.as_markup())

    @d.callback_query(F.data.startswith("tool:"))
    async def cb_tool(call: types.CallbackQuery):
        code = call.data.split(":", 1)[1]
        get_prefs(call.from_user.id)["tool"] = code
        label = dict((v, k) for k, v in TOOLS_LIST).get(code, code)
        await call.answer()
        await call.message.edit_text(f"Инструмент: *{label}*", parse_mode="Markdown")

    @d.message(F.text)
    async def handle_text(message: types.Message):
        await bot_inst.send_chat_action(chat_id=message.chat.id, action="typing")
        try:
            await ask_gemini_web(bot_inst, message, message.text)
        except Exception as web_err:
            try:
                ans, used_model = await ask_gemini_fallback(message.from_user.id, message.text, message.text)
                warn = f"⚠️ [Сработал запасной API ({used_model}) | Причина веб-сбоя: {web_err}]\n\n"
                await safe_send(message, warn + ans)
            except Exception as e:
                await message.answer(f"Ошибка (и веб, и запаска): {e}", reply_markup=get_main_reply_kb())

    @d.message(F.photo)
    async def handle_photo(message: types.Message):
        await bot_inst.send_chat_action(chat_id=message.chat.id, action="typing")
        temp_path = f"/tmp/photo_{message.from_user.id}.jpg"
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
                ans, used_model = await ask_gemini_fallback(message.from_user.id, [caption, image], f"[Фото] {caption}")
                warn = f"⚠️ [Сработал запасной API ({used_model}) | Причина веб-сбоя: {web_err}]\n\n"
                await safe_send(message, warn + ans)
        except Exception as e:
            await message.answer(f"Ошибка: {e}", reply_markup=get_main_reply_kb())
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)

    @d.message(F.document)
    async def handle_document(message: types.Message):
        await bot_inst.send_chat_action(chat_id=message.chat.id, action="typing")
        doc = message.document
        temp_path = os.path.join("/tmp", doc.file_name or f"file_{message.from_user.id}")
        try:
            file_io = io.BytesIO()
            await bot_inst.download(doc, destination=file_io)
            with open(temp_path, "wb") as f:
                f.write(file_io.getvalue())

            caption = message.caption or "Проанализируй файл."
            await ask_gemini_web(bot_inst, message, caption, file_path=temp_path)
        except Exception as e:
            await message.answer(f"Ошибка файла: {e}", reply_markup=get_main_reply_kb())
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)

    return d

@st.cache_resource
def launch_bot_v9():
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

launch_bot_v9()

st.title("Gemini Bot")
st.success("Активен")
