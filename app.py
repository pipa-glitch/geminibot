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
from gemini_webapi import GeminiClient
from google import genai
from google.genai import types as gtypes

TELEGRAM_TOKEN = st.secrets["TELEGRAM_TOKEN"]
SECURE_1PSID = st.secrets.get("SECURE_1PSID", "")
SECURE_1PSIDTS = st.secrets.get("SECURE_1PSIDTS", "")
GEMINI_KEYS = st.secrets.get("GEMINI_KEYS", [])

bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()

# Клиент веб-интерфейса Gemini Pro и сессии чатов
web_client = None
web_chats = {}
user_history = {}

async def get_web_client():
    global web_client
    if not SECURE_1PSID:
        return None
    if web_client is None:
        client = GeminiClient(SECURE_1PSID, SECURE_1PSIDTS)
        await client.init(timeout=60, auto_refresh=True)
        web_client = client
    return web_client

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
        await message.answer_document(doc, caption=f"📄 Твой файл: {filename}")
        return

    max_len = 4000
    for i in range(0, len(text), max_len):
        part = text[i:i + max_len]
        try:
            await message.answer(part, parse_mode="Markdown")
        except Exception:
            await message.answer(part, parse_mode=None)

# Основной запрос через ВЕБ-ИНТЕРФЕЙС (Pro подписка + родной Nano Banana)
async def ask_gemini_web(message: types.Message, prompt: str, image_path: str = None):
    client = await get_web_client()
    if not client:
        raise Exception("Веб-куки не настроены")

    uid = message.from_user.id
    if uid not in web_chats:
        web_chats[uid] = client.start_chat()

    chat = web_chats[uid]

    # Добавляем аккуратное правило без LaTeX для текста
    full_prompt = (
        f"{prompt}\n\n"
        "(Примечание: если пишешь математические формулы, пиши их без знаков $ и без LaTeX, обычными символами x², √, /. "
        "Если просят скинуть код/текст файлом — начни ответ со строки SEND_FILE:имя_файла.расширение)"
    )

    files = [image_path] if image_path else None
    response = await chat.send_message(full_prompt, files=files)

    # Если веб-чат сгенерировал или отредактировал картинку через Nano Banana!
    if response.images:
        await bot.send_chat_action(chat_id=message.chat.id, action="upload_photo")
        for idx, img in enumerate(response.images):
            out_name = f"nano_banana_{uid}_{idx}.png"
            out_path = os.path.join("/tmp", out_name)
            await img.save(path="/tmp", filename=out_name)
            if os.path.exists(out_path):
                with open(out_path, "rb") as f:
                    photo_file = types.BufferedInputFile(f.read(), filename=out_name)
                    await message.answer_photo(photo_file, caption="🍌 Готово (Nano Banana Web Pro)!")
                os.remove(out_path)

    if response.text:
        await safe_send(message, response.text)

# Запасной запрос через обычный API (если куки когда-нибудь протухнут)
async def ask_gemini_fallback(user_id: int, new_content, history_text: str):
    tz_msk = timezone(timedelta(hours=3))
    current_time = datetime.now(tz_msk).strftime("%d.%m.%Y, %H:%M")
    history = user_history.get(user_id, [])
    full_contents = list(history) + [new_content]

    for key in GEMINI_KEYS:
        try:
            client = genai.Client(api_key=key.strip())
            response = await asyncio.to_thread(
                client.models.generate_content,
                model="gemini-3.5-flash-lite",
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
    raise Exception("И веб-куки, и запасные ключи недоступны!")

@dp.message(CommandStart())
async def start_cmd(message: types.Message):
    web_chats.pop(message.from_user.id, None)
    user_history[message.from_user.id] = []
    await message.answer(
        "👋 Привет! Я подключен напрямую к **Gemini Web Pro (с Nano Banana)** 🍌!\n\n"
        "• Пиши любые запросы, проси нарисовать или отредактировать любое фото.\n"
        "• Кидай скриншоты и пиши «добавь сюда кота» — родной Nano Banana всё сделает!\n"
        "• Очистить диалог: /clear"
    )

@dp.message(Command("clear"))
async def clear_cmd(message: types.Message):
    web_chats.pop(message.from_user.id, None)
    user_history[message.from_user.id] = []
    await message.answer("🧹 Новый чат в Gemini Pro начат с чистого листа!")

@dp.message(F.text)
async def handle_text(message: types.Message):
    await bot.send_chat_action(chat_id=message.chat.id, action="typing")
    try:
        await ask_gemini_web(message, message.text)
    except Exception as web_err:
        print(f"[!] Веб-чат выдал ошибку ({web_err}), включаю запасной API...")
        try:
            global web_client
            web_client = None
            ans = await ask_gemini_fallback(message.from_user.id, message.text, message.text)
            await safe_send(message, ans)
        except Exception as e:
            await message.answer(f"⚠️ Ошибка: {e}")

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
            print(f"[!] Веб-фото не прошло ({web_err}), включаю запасной API...")
            image = Image.open(file_io)
            image.load()
            ans = await ask_gemini_fallback(message.from_user.id, [caption, image], f"[Фото] {caption}")
            await safe_send(message, ans)
    except Exception as e:
        await message.answer(f"⚠️ Ошибка фото: {e}")
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)

async def start_aiogram_loop():
    while True:
        try:
            await bot.delete_webhook(drop_pending_updates=True)
            await dp.start_polling(bot, handle_signals=False)
        except Exception as e:
            await asyncio.sleep(3)

@st.cache_resource
def start_bot_background_v5():
    def worker():
        asyncio.run(start_aiogram_loop())

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    return t

start_bot_background_v5()

st.title("🍌 Telegram Gemini Web Pro + Nano Banana")
st.success("✅ Бот подключен напрямую к веб-интерфейсу Gemini Pro!")
