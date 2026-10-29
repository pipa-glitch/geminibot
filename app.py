import os
import io
import re
import asyncio
import threading
from datetime import datetime, timezone, timedelta
from PIL import Image
import streamlit as st
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart, Command
from aiogram.utils.keyboard import ReplyKeyboardBuilder, InlineKeyboardBuilder
from gemini_webapi import GeminiClient
from google import genai
from google.genai import types as gtypes

TELEGRAM_TOKEN = st.secrets["TELEGRAM_TOKEN"]
SECURE_1PSID = st.secrets.get("SECURE_1PSID", "")
SECURE_1PSIDTS = st.secrets.get("SECURE_1PSIDTS", "")
GEMINI_KEYS = st.secrets.get("GEMINI_KEYS", [])

bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()

web_client = None
web_chats = {}
user_history = {}
user_prefs = {}

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

def get_prefs(uid: int):
    if uid not in user_prefs:
        user_prefs[uid] = {
            "model": "3.8 Flash",
            "reasoning": "high",
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
        return None
    if web_client is None:
        client = GeminiClient(SECURE_1PSID, SECURE_1PSIDTS)
        await client.init(timeout=60, auto_refresh=True)
        web_client = client
    return web_client

def resolve_account_model(client, wanted_name: str):
    wanted_low = wanted_name.lower()
    try:
        models = client.list_models() or []
        for m in models:
            d_name = str(getattr(m, "display_name", "")).lower()
            m_name = str(getattr(m, "model_name", "")).lower()
            if wanted_low in d_name or wanted_low in m_name:
                return m
        for token in ["argon", "3.1", "3.8", "3.5", "pro", "flash"]:
            if token in wanted_low:
                for m in models:
                    d_name = str(getattr(m, "display_name", "")).lower()
                    m_name = str(getattr(m, "model_name", "")).lower()
                    if token in d_name or token in m_name:
                        return m
    except Exception:
        pass
    try:
        if "pro" in wanted_low or "argon" in wanted_low:
            return client.resolve_model("pro")
        if "lite" in wanted_low:
            return client.resolve_model("flash-lite")
        return client.resolve_model("flash")
    except Exception:
        return wanted_name

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

async def ask_gemini_web(message: types.Message, prompt: str, file_path: str = None):
    client = await get_web_client()
    if not client:
        raise Exception("Веб-клиент не подключен")

    uid = message.from_user.id
    prefs = get_prefs(uid)
    target_model = resolve_account_model(client, prefs["model"])

    if uid not in web_chats:
        web_chats[uid] = client.start_chat(model=target_model)
    else:
        old_chat = web_chats[uid]
        try:
            web_chats[uid] = client.start_chat(model=target_model, metadata=old_chat.metadata)
        except Exception:
            old_chat.model = target_model

    chat = web_chats[uid]

    tool_map = {
        "none": "",
        "image": "[Инструмент: Создание изображений (Nano Banana)] ",
        "video": "[Инструмент: Создание видео] ",
        "music": "[Инструмент: Создание музыки] ",
        "canvas": "[Инструмент: Canvas — интерактивный документ/код] ",
        "deep_research": "[Инструмент: Deep Research — глубокое исследование] ",
        "learning": "[Инструмент: Обучение — пошаговое объяснение] ",
    }
    reasoning_map = {
        "low": "Уровень рассуждений: Низкий (быстрый и эффективный ответ).",
        "medium": "Уровень рассуждений: Средний (умеренный разбор).",
        "high": "Уровень рассуждений: Высокий (глубокий анализ).",
    }

    prefix = tool_map.get(prefs["tool"], "")
    r_note = reasoning_map.get(prefs["reasoning"], "")

    full_prompt = (
        f"{prefix}{prompt}\n\n"
        f"[{r_note} Формулы пиши без $ и без LaTeX (x², √, /). "
        "Если просят скинуть файлом — начни первую строку с SEND_FILE:имя_файла.расширение]"
    )

    files = [file_path] if file_path else None
    response = await chat.send_message(full_prompt, files=files)

    if response.images:
        await bot.send_chat_action(chat_id=message.chat.id, action="upload_photo")
        for idx, img in enumerate(response.images):
            out_name = f"img_{uid}_{idx}.png"
            out_path = os.path.join("/tmp", out_name)
            await img.save(path="/tmp", filename=out_name)
            if os.path.exists(out_path):
                with open(out_path, "rb") as f:
                    photo_file = types.BufferedInputFile(f.read(), filename=out_name)
                    await message.answer_photo(photo_file, reply_markup=get_main_reply_kb())
                os.remove(out_path)

    if response.text:
        await safe_send(message, response.text)

async def ask_gemini_fallback(user_id: int, new_content, history_text: str):
    tz_msk = timezone(timedelta(hours=3))
    current_time = datetime.now(tz_msk).strftime("%d.%m.%Y, %H:%M")
    history = user_history.get(user_id, [])
    full_contents = list(history) + [new_content]

    for model_name in ["gemini-3.8-flash", "gemini-3.5-flash-lite"]:
        for key in GEMINI_KEYS:
            try:
                client = genai.Client(api_key=key.strip())
                response = await asyncio.to_thread(
                    client.models.generate_content,
                    model=model_name,
                    contents=full_contents,
                    config=gtypes.GenerateContentConfig(
                        system_instruction=f"Сейчас (МСК): {current_time}. Не используй LaTeX и знаки $."
                    ),
                )
                ans = response.text or "Пустой ответ."
                history.append(f"Пользователь: {history_text}")
                history.append(f"Ассистент: {ans}")
                user_history[user_id] = history[-14:]
                return ans
            except Exception:
                continue
    raise Exception("Сервис временно недоступен.")

# ================= МЕНЮ =================

@dp.message(CommandStart())
async def start_cmd(message: types.Message):
    uid = message.from_user.id
    web_chats.pop(uid, None)
    prefs = get_prefs(uid)
    await message.answer(
        f"Модель: *{prefs['model']}*\n"
        f"Уровень рассуждений: *{prefs['reasoning']}*",
        parse_mode="Markdown",
        reply_markup=get_main_reply_kb()
    )

@dp.message(F.text == "Новый чат")
@dp.message(Command("clear"))
async def clear_cmd(message: types.Message):
    web_chats.pop(message.from_user.id, None)
    user_history[message.from_user.id] = []
    await message.answer("Новый чат начат.", reply_markup=get_main_reply_kb())

@dp.message(F.text == "Модель")
async def menu_models(message: types.Message):
    cur = get_prefs(message.from_user.id)["model"]
    kb = InlineKeyboardBuilder()
    for label, code in MODELS_LIST:
        mark = "✓ " if cur == code else ""
        kb.button(text=f"{mark}{label}", callback_data=f"model:{code}")
    kb.adjust(1)
    await message.answer("Выбери модель:", reply_markup=kb.as_markup())

@dp.callback_query(F.data.startswith("model:"))
async def cb_model(call: types.CallbackQuery):
    code = call.data.split(":", 1)[1]
    get_prefs(call.from_user.id)["model"] = code
    await call.answer()
    await call.message.edit_text(f"Модель: *{code}*", parse_mode="Markdown")

@dp.message(F.text == "Уровень рассуждений")
async def menu_reasoning(message: types.Message):
    cur = get_prefs(message.from_user.id)["reasoning"]
    kb = InlineKeyboardBuilder()
    for label, code in REASONING_LIST:
        mark = "✓ " if cur == code else ""
        kb.button(text=f"{mark}{label}", callback_data=f"reason:{code}")
    kb.adjust(1)
    await message.answer("Уровень рассуждений:", reply_markup=kb.as_markup())

@dp.callback_query(F.data.startswith("reason:"))
async def cb_reason(call: types.CallbackQuery):
    code = call.data.split(":", 1)[1]
    get_prefs(call.from_user.id)["reasoning"] = code
    label = dict((v, k) for k, v in REASONING_LIST).get(code, code)
    await call.answer()
    await call.message.edit_text(f"Уровень рассуждений: *{label}*", parse_mode="Markdown")

@dp.message(F.text == "Инструменты")
async def menu_tools(message: types.Message):
    cur = get_prefs(message.from_user.id)["tool"]
    kb = InlineKeyboardBuilder()
    for label, code in TOOLS_LIST:
        mark = "✓ " if cur == code else ""
        kb.button(text=f"{mark}{label}", callback_data=f"tool:{code}")
    kb.adjust(1)
    await message.answer("Инструменты:", reply_markup=kb.as_markup())

@dp.callback_query(F.data.startswith("tool:"))
async def cb_tool(call: types.CallbackQuery):
    code = call.data.split(":", 1)[1]
    get_prefs(call.from_user.id)["tool"] = code
    label = dict((v, k) for k, v in TOOLS_LIST).get(code, code)
    await call.answer()
    await call.message.edit_text(f"Инструмент: *{label}*", parse_mode="Markdown")

# ================= ОБРАБОТКА ЗАПРОСОВ =================

@dp.message(F.text)
async def handle_text(message: types.Message):
    await bot.send_chat_action(chat_id=message.chat.id, action="typing")
    try:
        await ask_gemini_web(message, message.text)
    except Exception:
        try:
            global web_client
            web_client = None
            ans = await ask_gemini_fallback(message.from_user.id, message.text, message.text)
            await safe_send(message, ans)
        except Exception as e:
            await message.answer(f"Ошибка: {e}", reply_markup=get_main_reply_kb())

@dp.message(F.photo)
async def handle_photo(message: types.Message):
    await bot.send_chat_action(chat_id=message.chat.id, action="typing")
    temp_path = f"/tmp/photo_{message.from_user.id}.jpg"
    try:
        photo = message.photo[-1]
        file_io = io.BytesIO()
        await bot.download(photo, destination=file_io)
        file_io.seek(0)
        with open(temp_path, "wb") as f:
            f.write(file_io.getvalue())

        caption = message.caption or "Реши или опиши фото."
        try:
            await ask_gemini_web(message, caption, file_path=temp_path)
        except Exception:
            image = Image.open(file_io)
            image.load()
            ans = await ask_gemini_fallback(message.from_user.id, [caption, image], f"[Фото] {caption}")
            await safe_send(message, ans)
    except Exception as e:
        await message.answer(f"Ошибка: {e}", reply_markup=get_main_reply_kb())
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)

@dp.message(F.document)
async def handle_document(message: types.Message):
    await bot.send_chat_action(chat_id=message.chat.id, action="typing")
    doc = message.document
    temp_path = os.path.join("/tmp", doc.file_name or f"file_{message.from_user.id}")
    try:
        file_io = io.BytesIO()
        await bot.download(doc, destination=file_io)
        with open(temp_path, "wb") as f:
            f.write(file_io.getvalue())

        caption = message.caption or "Проанализируй прикрепленный файл."
        await ask_gemini_web(message, caption, file_path=temp_path)
    except Exception as e:
        await message.answer(f"Ошибка файла: {e}", reply_markup=get_main_reply_kb())
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)

async def start_aiogram_loop():
    while True:
        try:
            await bot.delete_webhook(drop_pending_updates=True)
            await dp.start_polling(bot, handle_signals=False)
        except Exception:
            await asyncio.sleep(3)

@st.cache_resource
def start_bot_background_v8():
    def worker():
        asyncio.run(start_aiogram_loop())

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    return t

start_bot_background_v8()

st.title("Gemini Bot")
st.success("Работает 24/7")
