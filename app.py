import io
import asyncio
import threading
import time
from datetime import datetime, timezone, timedelta
from PIL import Image
import streamlit as st
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart
from google import genai
from google.genai import types as gtypes

# ==========================================
TELEGRAM_TOKEN = "9099534534:AAExrwposBIWvrkBZKbN32FMfUl0ecaVFrk"

GEMINI_KEYS = [
    "AQ.Ab8RN6Jzv5bdS62FhADiyMOO8VG_qnUgaFD42AezOLmXUfxAUQ",
    "AQ.Ab8RN6JcLl4BftREZQvDUoGI0PBhw58D4ZSZsF5_a8c1dxlMfg",
    "AQ.Ab8RN6LI0dQCRuAdOjIQVst5hnjqnYsCBinRH6Pi74DlSvZ4_g",
    "AQ.Ab8RN6JPKdXq64cHyCjzQBU19YXiarugNcESphjIfAVmmfv9Pg",
    "AQ.Ab8RN6L8hdVSMKGKfgSTy5aYRWCSZXoXOCX1VGeUGxeaMkPhyw",
]
# ==========================================

bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()

# Функция безопасной отправки сообщений (если Telegram ругается на разметку)
async def safe_send(message: types.Message, text: str):
    max_len = 4000
    for i in range(0, len(text), max_len):
        part = text[i:i + max_len]
        try:
            await message.answer(part, parse_mode="Markdown")
        except Exception:
            # Если в тексте кривые спецсимволы и Markdown сломался — шлем простым текстом
            await message.answer(part, parse_mode=None)

# Запрос к Gemini с перебором 5 ключей
async def ask_gemini(contents):
    last_error = None
    # Время по МСК (UTC+3), чтобы на сервере не врало
    tz_msk = timezone(timedelta(hours=3))
    current_time = datetime.now(tz_msk).strftime("%d.%m.%Y, %H:%M")

    for index, key in enumerate(GEMINI_KEYS):
        try:
            client = genai.Client(api_key=key)
            response = await asyncio.to_thread(
                client.models.generate_content,
                model="gemini-3.5-flash-lite",
                contents=contents,
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
                        "4. Ответ должен быть чистым, структурированным и удобным для чтения с телефона."
                    )
                ),
            )
            return response.text
        except Exception as e:
            err_str = str(e).lower()
            last_error = e
            if "429" in err_str or "quota" in err_str or "resource_exhausted" in err_str:
                print(f"[!] Лимит ключа #{index + 1} исчерпан. Переключаюсь на запасной ключ...")
                continue
            else:
                raise e

    raise Exception(f"Все 5 ключей Gemini исчерпали лимиты! Ошибка: {last_error}")

@dp.message(CommandStart())
async def start_cmd(message: types.Message):
    await message.answer(
        "👋 Привет! Я твой школьный помощник на базе Google Gemini.\n\n"
        "• Задавай любые вопросы текстом.\n"
        "• Присылай фотки задач, карточек или тетрадей — всё распишу подробно и по шагам!"
    )

@dp.message(F.text)
async def handle_text(message: types.Message):
    await bot.send_chat_action(chat_id=message.chat.id, action="typing")
    try:
        answer = await ask_gemini(message.text)
        await safe_send(message, answer or "Нейросеть прислала пустой ответ.")
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
        
        # Гарантируем полную загрузку картинки в память
        image = Image.open(file_io)
        image.load()

        caption = message.caption or "Объясни подробно всё, что изображено на фото, и реши задание."

        answer = await ask_gemini([caption, image])
        await safe_send(message, answer or "Не удалось распознать фото.")
    except Exception as e:
        await message.answer(f"⚠️ Ошибка при обработке фото: {e}")

# Вечный цикл запуска с автоподъемом при сбоях сети
async def start_aiogram_loop():
    while True:
        try:
            await bot.delete_webhook(drop_pending_updates=True)
            # handle_signals=False обязательно для работы в фоне без краша
            await dp.start_polling(bot, handle_signals=False)
        except Exception as e:
            print(f"[!] Сбой подключения Telegram: {e}. Перезапуск через 3 сек...")
            await asyncio.sleep(3)

@st.cache_resource
def start_bot_background():
    def worker():
        asyncio.run(start_aiogram_loop())

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    return t

# Старт бота
start_bot_background()

# Интерфейс страницы
st.title("🤖 Telegram Gemini Bot")
st.success("✅ Бот успешно работает в облаке 24/7!")
st.caption("Сервер активен. Фоновый поток защищен от сбоев.")
