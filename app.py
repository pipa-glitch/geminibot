import io
import re
import urllib.parse
import asyncio
import threading
import aiohttp
from datetime import datetime, timezone, timedelta
from PIL import Image
import streamlit as st
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart, Command
from google import genai
from google.genai import types as gtypes

TELEGRAM_TOKEN = st.secrets["TELEGRAM_TOKEN"]
GEMINI_KEYS = st.secrets["GEMINI_KEYS"]

bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()

user_history = {}

async def safe_send(message: types.Message, text: str):
    # 1. Проверяем, не нужно ли сгенерировать и отправить КАРТИНКУ
    if text.strip().startswith("SEND_IMAGE:"):
        prompt_en = text.strip().replace("SEND_IMAGE:", "").strip().split("\n")[0]
        await bot.send_chat_action(chat_id=message.chat.id, action="upload_photo")
        try:
            encoded_prompt = urllib.parse.quote(prompt_en)
            image_url = f"https://image.pollinations.ai/prompt/{encoded_prompt}?width=1024&height=1024&nologo=true"
            async with aiohttp.ClientSession() as session:
                async with session.get(image_url, timeout=45) as resp:
                    if resp.status == 200:
                        img_bytes = await resp.read()
                        photo_file = types.BufferedInputFile(img_bytes, filename="image.jpg")
                        await message.answer_photo(photo_file, caption="🎨 Держи картинку!")
                        return
        except Exception as e:
            await message.answer(f"⚠️ Не удалось нарисовать картинку: {e}")
            return

    # 2. Проверяем, не нужно ли отправить ФАЙЛ
    if text.strip().startswith("SEND_FILE:"):
        lines = text.strip().split("\n", 1)
        filename = lines[0].replace("SEND_FILE:", "").strip() or "code.py"
        file_content = lines[1] if len(lines) > 1 else ""

        file_content = re.sub(r"^```[a-zA-Z0-9_-]*\n", "", file_content.strip())
        file_content = re.sub(r"\n```$", "", file_content.strip())

        doc = types.BufferedInputFile(file_content.encode("utf-8"), filename=filename)
        await message.answer_document(doc, caption=f"📄 Готово! Твой файл: {filename}")
        return

    # 3. Обычная отправка текста
    max_len = 4000
    for i in range(0, len(text), max_len):
        part = text[i:i + max_len]
        try:
            await message.answer(part, parse_mode="Markdown")
        except Exception:
            await message.answer(part, parse_mode=None)

async def ask_gemini(user_id: int, new_content, history_text_for_save: str):
    last_error = None
    tz_msk = timezone(timedelta(hours=3))
    current_time = datetime.now(tz_msk).strftime("%d.%m.%Y, %H:%M")

    history = user_history.get(user_id, [])
    full_contents = list(history) + [new_content]

    for index, key in enumerate(GEMINI_KEYS):
        try:
            client = genai.Client(api_key=key.strip())
            response = await asyncio.to_thread(
                client.models.generate_content,
                model="gemini-3.5-flash-lite",
                contents=full_contents,
                config=gtypes.GenerateContentConfig(
                    system_instruction=(
                        f"Сейчас точная реальная дата и время (МСК): {current_time}. "
                        "Ты — умный и понятный ассистент на базе Gemini 3.5. "
                        "Помогай с учебой, кодом и любыми школьными предметами.\n\n"
                        "СТРОГИЕ ПРАВИЛА ОФОРМЛЕНИЯ ДЛЯ TELEGRAM:\n"
                        "1. КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО использовать LaTeX и знаки доллара $! Никаких \\frac, \\sqrt, \\implies, \\cup и т.д.\n"
                        "2. Все математические формулы пиши обычным понятным текстом:\n"
                        "   - Степени: x², x³, x₀\n"
                        "   - Корни: √3, √(D)\n"
                        "   - Дроби через слэш: (-4)/2 или (4 ± 4√3)/2\n"
                        "   - Знаки: ±, ≈, ≠, ≤, ≥, стрелку через '=>', объединение словом 'и' или 'U'.\n"
                        "3. Не используй решетки '###' для заголовков. Выделяй важное просто жирным шрифтом.\n"
                        "4. ОТПРАВКА ФАЙЛОВ: Если пользователь просит отправить код или текст файлом, "
                        "напиши в САМОЙ ПЕРВОЙ СТРОКЕ строго: SEND_FILE:имя_файла.расширение, а со следующей строки — чистый код/текст файла.\n"
                        "5. ГЕНЕРАЦИЯ КАРТИНОК: У тебя ЕСТЬ функция генерации и отправки картинок! Никогда не говори, что не умеешь отправлять картинки. "
                        "Если пользователь просит кинуть картинку, фото или что-то нарисовать (например: 'кинь картинку кота', 'нарисуй машину'), "
                        "ответь ОДНОЙ строкой строго в формате: SEND_IMAGE:подробное описание картинки на английском языке "
                        "(например: SEND_IMAGE:cute fluffy cat sitting on a windowsill, realistic photo, high quality). Никакого другого текста не добавляй!"
                    )
                ),
            )
            answer_text = response.text or "Пустой ответ."

            history.append(f"Пользователь: {history_text_for_save}")
            history.append(f"Ассистент: {answer_text}")
            user_history[user_id] = history[-14:]

            return answer_text
        except Exception as e:
            last_error = e
            continue

    raise Exception(f"Все ключи выдали ошибку! Последняя: {last_error}")

@dp.message(CommandStart())
async def start_cmd(message: types.Message):
    user_history[message.from_user.id] = []
    await message.answer(
        "👋 Привет! Я твой прокачанный помощник на базе Google Gemini.\n\n"
        "• Помню контекст диалога (очистить: /clear).\n"
        "• Решаю задачи по фото и тексту.\n"
        "• Умею кидать готовые файлы (напиши «отправь файлом»).\n"
        "• Умею рисовать и кидать любые картинки (напиши «кинь картинку ...»)!"
    )

@dp.message(Command("clear"))
async def clear_cmd(message: types.Message):
    user_history[message.from_user.id] = []
    await message.answer("🧹 Память диалога очищена!")

@dp.message(F.text)
async def handle_text(message: types.Message):
    await bot.send_chat_action(chat_id=message.chat.id, action="typing")
    try:
        answer = await ask_gemini(message.from_user.id, message.text, message.text)
        await safe_send(message, answer)
    except Exception as e:
        await message.answer(f"⚠️ Ошибка: {e}")

@dp.message(F.photo)
async def handle_photo(message: types.Message):
    await bot.send_chat_action(chat_id=message.chat.id, action="typing")
    try:
        photo = message.photo[-1]
        file_io = io.BytesIO()
        await bot.download(photo, destination=file_io)
        file_io.seek(0)
        image = Image.open(file_io)
        image.load()

        caption = message.caption or "Объясни подробно всё, что изображено на фото, и реши задание."

        answer = await ask_gemini(message.from_user.id, [caption, image], f"[Фото] {caption}")
        await safe_send(message, answer)
    except Exception as e:
        await message.answer(f"⚠️ Ошибка при обработке фото: {e}")

async def start_aiogram_loop():
    while True:
        try:
            await bot.delete_webhook(drop_pending_updates=True)
            await dp.start_polling(bot, handle_signals=False)
        except Exception as e:
            print(f"[!] Перезапуск через 3 сек: {e}")
            await asyncio.sleep(3)

@st.cache_resource
def start_bot_background_v3():
    def worker():
        asyncio.run(start_aiogram_loop())

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    return t

start_bot_background_v3()

st.title("🤖 Telegram Gemini Bot v3.0")
st.success("✅ Бот (Память + Файлы + Генерация картинок) работает 24/7!")
