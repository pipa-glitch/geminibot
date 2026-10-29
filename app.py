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

# Настройки каждого пользователя (по умолчанию сразу топовая модель PRO!)
user_prefs = {}

def get_prefs(uid: int):
    if uid not in user_prefs:
        user_prefs[uid] = {
            "model": "pro",            # "pro", "flash", "flash-lite" или имя из list_models()
            "thinking": "deep",        # "deep" (Глубокое), "normal" (Стандарт), "fast" (Быстрое)
            "show_thoughts": True,     # Показывать ли блок размышлений модели
            "tool": "auto",            # "auto", "banana", "search", "youtube", "research", "file"
        }
    return user_prefs[uid]

# Нижняя постоянная клавиатура-пульт
def get_main_reply_kb():
    kb = ReplyKeyboardBuilder()
    kb.button(text="🤖 Модель")
    kb.button(text="🧠 Размышления")
    kb.button(text="🛠 Инструменты")
    kb.button(text="🧹 Новый чат")
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

def resolve_account_model(client, model_key: str):
    # Ищем точную модель на аккаунте пользователя, чтобы сайт НЕ сбрасывал её на 3.5!
    try:
        return client.resolve_model(model_key)
    except Exception:
        try:
            models = client.list_models() or []
            for m in models:
                m_name = str(getattr(m, "model_name", "")).lower()
                d_name = str(getattr(m, "display_name", "")).lower()
                if model_key.lower() in m_name or model_key.lower() in d_name:
                    return m
            if models:
                return models[0]
        except Exception:
            pass
    return model_key

async def safe_send(message: types.Message, text: str):
    if not text:
        return
    if "SEND_FILE:" in text:
        after_tag = text.split("SEND_FILE:", 1)[1].strip()
        lines = after_tag.split("\n", 1)
        filename = lines[0].strip() or "code.py"
        file_content = lines[1] if len(lines) > 1 else ""
        file_content = re.sub(r"^```[a-zA-Z0-9_-]*\n", "", file_content.strip())
        file_content = re.sub(r"\n```$", "", file_content.strip())
        doc = types.BufferedInputFile(file_content.encode("utf-8"), filename=filename)
        await message.answer_document(doc, caption=f"📄 Твой файл: {filename}", reply_markup=get_main_reply_kb())
        return

    max_len = 4000
    for i in range(0, len(text), max_len):
        part = text[i:i + max_len]
        try:
            await message.answer(part, parse_mode="Markdown", reply_markup=get_main_reply_kb())
        except Exception:
            await message.answer(part, parse_mode=None, reply_markup=get_main_reply_kb())

# Запрос через Веб-интерфейс Gemini с удержанием выбранной модели, мыслей и инструментов
async def ask_gemini_web(message: types.Message, prompt: str, image_path: str = None):
    client = await get_web_client()
    if not client:
        raise Exception("Веб-куки не подключены")

    uid = message.from_user.id
    prefs = get_prefs(uid)
    target_model = resolve_account_model(client, prefs["model"])

    # Создаём или обновляем модель в текущей сессии чата, сохраняя историю на сайте!
    if uid not in web_chats:
        web_chats[uid] = client.start_chat(model=target_model)
    else:
        old_chat = web_chats[uid]
        try:
            web_chats[uid] = client.start_chat(model=target_model, metadata=old_chat.metadata)
        except Exception:
            old_chat.model = target_model

    chat = web_chats[uid]

    # Настраиваем инструмент
    tool = prefs["tool"]
    tool_prefix = ""
    if tool == "banana":
        tool_prefix = "Use Nano Banana image generation/editing tool to generate or edit the image as requested: "
    elif tool == "search":
        tool_prefix = "@Google Search Найди самую актуальную и свежую информацию в интернете: "
    elif tool == "youtube":
        tool_prefix = "@YouTube Найди и проанализируй видео по запросу: "
    elif tool == "research":
        tool_prefix = "Проведи максимально глубокое, детальное экспертное исследование со структурой, фактами и проверкой источников по теме: "
    elif tool == "file":
        tool_prefix = "ВАЖНО: Ответ или код обязательно верни файлом, начав первую строку со слов SEND_FILE:имя_файла.расширение\nЗапрос: "

    # Настраиваем уровень размышления
    thinking = prefs["thinking"]
    if thinking == "deep":
        think_note = "Рассуждай максимально глубоко, тщательно проверяй каждый шаг и логику."
    elif thinking == "fast":
        think_note = "Отвечай максимально быстро, кратко и сразу к сути без лишних вступлений."
    else:
        think_note = "Отвечай структурированно и понятно."

    full_prompt = (
        f"{tool_prefix}{prompt}\n\n"
        f"[Системная настройка: {think_note} "
        "Математические формулы пиши без знаков $ и без LaTeX — обычными символами x², √, /. "
        "Если пользователь просит отправить результат файлом — начни ответ со строки SEND_FILE:имя_файла.расширение]"
    )

    files = [image_path] if image_path else None
    response = await chat.send_message(full_prompt, files=files)

    # 1. Если включен показ размышлений и модель вернула свои мысли (response.thoughts)
    if prefs["show_thoughts"] and getattr(response, "thoughts", None):
        thoughts_text = response.thoughts.strip()
        if thoughts_text:
            short_thoughts = thoughts_text[:3500] + ("..." if len(thoughts_text) > 3500 else "")
            await message.answer(f"💭 **Размышления модели ({prefs['model'].upper()}):**\n\n_{short_thoughts}_")

    # 2. Если сгенерирована или отредактирована картинка (Nano Banana)
    if response.images:
        await bot.send_chat_action(chat_id=message.chat.id, action="upload_photo")
        for idx, img in enumerate(response.images):
            out_name = f"nano_banana_{uid}_{idx}.png"
            out_path = os.path.join("/tmp", out_name)
            await img.save(path="/tmp", filename=out_name)
            if os.path.exists(out_path):
                with open(out_path, "rb") as f:
                    photo_file = types.BufferedInputFile(f.read(), filename=out_name)
                    await message.answer_photo(
                        photo_file,
                        caption=f"🍌 Готово! (Модель: {prefs['model'].upper()})",
                        reply_markup=get_main_reply_kb()
                    )
                os.remove(out_path)

    # 3. Отправляем текстовый ответ
    if response.text:
        await safe_send(message, response.text)

# Запасной API на случай сбоя куков
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
    raise Exception("И веб-чат, и запасные ключи недоступны!")

# ================= МЕНЮ И КНОПКИ УПРАВЛЕНИЯ =================

@dp.message(CommandStart())
async def start_cmd(message: types.Message):
    uid = message.from_user.id
    web_chats.pop(uid, None)
    prefs = get_prefs(uid)
    await message.answer(
        "🎛 **Пульт управления Gemini Web Pro активирован!**\n\n"
        f"• **Текущая модель:** `{prefs['model'].upper()}`\n"
        f"• **Размышления:** `{prefs['thinking'].upper()}` (Мысли: `{'ВКЛ' if prefs['show_thoughts'] else 'ВЫКЛ'}`)\n"
        f"• **Инструмент:** `{prefs['tool'].upper()}`\n\n"
        "Переключай модели, глубину мыслей и инструменты кнопками внизу экрана 👇",
        parse_mode="Markdown",
        reply_markup=get_main_reply_kb()
    )

@dp.message(F.text == "🧹 Новый чат")
@dp.message(Command("clear"))
async def clear_cmd(message: types.Message):
    web_chats.pop(message.from_user.id, None)
    user_history[message.from_user.id] = []
    await message.answer("🧹 Начат новый чистый чат в Gemini!", reply_markup=get_main_reply_kb())

# 1. Меню выбора МОДЕЛИ
@dp.message(F.text == "🤖 Модель")
async def menu_models(message: types.Message):
    prefs = get_prefs(message.from_user.id)
    cur = prefs["model"]

    kb = InlineKeyboardBuilder()
    base_models = [
        ("🧠 Pro (Максимум / 3.8 Pro)", "pro"),
        ("⚡ Flash (Быстрая / 3.8 Flash)", "flash"),
        ("🚀 Flash-Lite (Легкая)", "flash-lite"),
    ]
    for label, code in base_models:
        mark = "✅ " if cur == code else ""
        kb.button(text=f"{mark}{label}", callback_data=f"set_model:{code}")

    # Подтягиваем реальные модели напрямую с аккаунта на сайте
    discovered_text = ""
    try:
        client = await get_web_client()
        if client:
            acc_models = client.list_models() or []
            if acc_models:
                discovered_text = "\n\n📋 **Модели, найденные на твоём аккаунте:**\n"
                for m in acc_models:
                    m_name = getattr(m, "model_name", str(m))
                    d_name = getattr(m, "display_name", m_name)
                    discovered_text += f"• `{d_name}` (`{m_name}`)\n"
                    if m_name not in ["pro", "flash", "flash-lite"]:
                        mark = "✅ " if cur == m_name else ""
                        kb.button(text=f"{mark}{d_name}", callback_data=f"set_model:{m_name[:35]}")
    except Exception:
        pass

    kb.adjust(1)
    await message.answer(
        f"🤖 **Выбор модели Gemini**\nСейчас закреплена: `{cur.upper()}`\n"
        "*(Выбранная модель жестко фиксируется и не сбрасывается на сайте)*" + discovered_text,
        parse_mode="Markdown",
        reply_markup=kb.as_markup()
    )

@dp.callback_query(F.data.startswith("set_model:"))
async def cb_set_model(call: types.CallbackQuery):
    model_code = call.data.split(":", 1)[1]
    prefs = get_prefs(call.from_user.id)
    prefs["model"] = model_code
    await call.answer(f"Модель переключена на {model_code.upper()}!")
    await call.message.edit_text(
        f"✅ **Активная модель закреплена:** `{model_code.upper()}`\nТеперь сайт не будет сбрасывать её на 3.5!",
        parse_mode="Markdown"
    )

# 2. Меню выбора УРОВНЯ РАЗМЫШЛЕНИЯ
@dp.message(F.text == "🧠 Размышления")
async def menu_thinking(message: types.Message):
    prefs = get_prefs(message.from_user.id)
    cur_t = prefs["thinking"]
    show_t = prefs["show_thoughts"]

    kb = InlineKeyboardBuilder()
    levels = [
        ("🔥 Глубокое (Deep Thinking)", "deep"),
        ("⚖️ Стандартное (Баланс)", "normal"),
        ("⚡ Быстрое (Сразу ответ)", "fast"),
    ]
    for label, code in levels:
        mark = "✅ " if cur_t == code else ""
        kb.button(text=f"{mark}{label}", callback_data=f"set_think:{code}")

    thought_label = "👁 Показ мыслей модели: ВКЛ ✅" if show_t else "🙈 Показ мыслей модели: ВЫКЛ ❌"
    kb.button(text=thought_label, callback_data="toggle_thoughts")
    kb.adjust(1)

    await message.answer(
        "🧠 **Настройка уровня размышления (Thinking Mode)**\n\n"
        "• В режиме **Глубокое** нейросеть тщательно проверяет задачи по шагам.\n"
        "• Кнопка **Показ мыслей** присылает внутренние рассуждения модели перед ответом.",
        parse_mode="Markdown",
        reply_markup=kb.as_markup()
    )

@dp.callback_query(F.data.startswith("set_think:"))
async def cb_set_think(call: types.CallbackQuery):
    t_code = call.data.split(":", 1)[1]
    prefs = get_prefs(call.from_user.id)
    prefs["thinking"] = t_code
    if t_code == "deep":
        prefs["model"] = "pro"  # Для глубокого размышления автоматически ставим Pro
    await call.answer(f"Режим размышлений: {t_code.upper()}")
    await call.message.edit_text(
        f"✅ **Уровень размышления:** `{t_code.upper()}`\n"
        f"• Показ мыслей: `{'ВКЛ' if prefs['show_thoughts'] else 'ВЫКЛ'}`",
        parse_mode="Markdown"
    )

@dp.callback_query(F.data == "toggle_thoughts")
async def cb_toggle_thoughts(call: types.CallbackQuery):
    prefs = get_prefs(call.from_user.id)
    prefs["show_thoughts"] = not prefs["show_thoughts"]
    status = "ВКЛЮЧЕН ✅" if prefs["show_thoughts"] else "ВЫКЛЮЧЕН ❌"
    await call.answer(f"Показ мыслей: {status}")
    await call.message.edit_text(f"✅ **Показ внутренних размышлений модели:** {status}", parse_mode="Markdown")

# 3. Меню выбора ИНСТРУМЕНТОВ
@dp.message(F.text == "🛠 Инструменты")
async def menu_tools(message: types.Message):
    prefs = get_prefs(message.from_user.id)
    cur_tool = prefs["tool"]

    kb = InlineKeyboardBuilder()
    tools = [
        ("💬 Авто (Универсальный чат)", "auto"),
        ("🍌 Nano Banana (Фотошоп / Генерация фото)", "banana"),
        ("🌐 Google Поиск (Свежая инфа из сети)", "search"),
        ("📺 YouTube (Разбор видео)", "youtube"),
        ("🔬 Deep Research (Глубокий доклад)", "research"),
        ("📄 Всегда отправлять файлом (.py / .txt)", "file"),
    ]
    for label, code in tools:
        mark = "✅ " if cur_tool == code else ""
        kb.button(text=f"{mark}{label}", callback_data=f"set_tool:{code}")

    kb.adjust(1)
    await message.answer(
        "🛠 **Выбери активный инструмент Gemini:**",
        parse_mode="Markdown",
        reply_markup=kb.as_markup()
    )

@dp.callback_query(F.data.startswith("set_tool:"))
async def cb_set_tool(call: types.CallbackQuery):
    tool_code = call.data.split(":", 1)[1]
    prefs = get_prefs(call.from_user.id)
    prefs["tool"] = tool_code
    await call.answer(f"Инструмент: {tool_code.upper()}")
    await call.message.edit_text(f"✅ **Активный инструмент:** `{tool_code.upper()}`", parse_mode="Markdown")

# ================= ОБРАБОТКА СООБЩЕНИЙ И ФОТО =================

@dp.message(F.text)
async def handle_text(message: types.Message):
    await bot.send_chat_action(chat_id=message.chat.id, action="typing")
    try:
        await ask_gemini_web(message, message.text)
    except Exception as web_err:
        print(f"[!] Ошибка веб-чата ({web_err}), включаю запасной API...")
        try:
            global web_client
            web_client = None
            ans = await ask_gemini_fallback(message.from_user.id, message.text, message.text)
            await safe_send(message, ans)
        except Exception as e:
            await message.answer(f"⚠️ Ошибка: {e}", reply_markup=get_main_reply_kb())

@dp.message(F.photo)
async def handle_photo(message: types.Message):
    await bot.send_chat_action(chat_id=message.chat.id, action="typing")
    temp_path = f"/tmp/input_{message.from_user.id}.jpg"
    try:
        photo = message.photo[-1]
        file_io = io.BytesIO()
        await bot.download(photo, destination=file_io)
        file_io.seek(0)

        with open(temp_path, "wb") as f:
            f.write(file_io.getvalue())

        caption = message.caption or "Объясни подробно всё, что изображено на фото, и реши задание."

        try:
            await ask_gemini_web(message, caption, image_path=temp_path)
        except Exception as web_err:
            print(f"[!] Веб-фото не прошло ({web_err}), запасной API...")
            image = Image.open(file_io)
            image.load()
            ans = await ask_gemini_fallback(message.from_user.id, [caption, image], f"[Фото] {caption}")
            await safe_send(message, ans)
    except Exception as e:
        await message.answer(f"⚠️ Ошибка фото: {e}", reply_markup=get_main_reply_kb())
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
def start_bot_background_v7():
    def worker():
        asyncio.run(start_aiogram_loop())

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    return t

start_bot_background_v7()

st.title("🎛 Telegram Gemini Web Pro (Full Control Panel)")
st.success("✅ Бот с выбором моделей, уровня размышлений и инструментов работает 24/7!")
