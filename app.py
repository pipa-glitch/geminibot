import io
import asyncio
import threading
from datetime import datetime
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

async def ask_gemini(contents):
    last_error = None
    current_time = datetime.now().strftime("%d.%m.%Y, %H:%M")

    for index, key in enumerate(GEMINI_KEYS):
        try:
            client = genai.Client(api_key=key)
            response = await asyncio.to_thread(
                client.models.generate_content,
                model="gemini-3.5-flash-lite",
                contents=contents,
                config=gtypes.GenerateContentConfig(
                    system_instruction=(
                        f"Сейчас точная реальная дата и время: {current_time}. "
                        "Ты — умный и понятный ассистент на базе Gemini 3.5. "
                        "Помогай с учебой, кодом и любыми вопросами.\n\n"
                        "СТРОГИЕ ПРАВИЛА ОФОРМЛЕНИЯ ДЛЯ TELEGRAM:\n"
                        "1. КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО использовать LaTeX и знаки доллара $! Никаких \\frac, \\sqrt, \\implies, \\cup и т.д.\n"
                        "2. Все математические формулы пиши обычным понятным текстом и Unicode-символами:\n"
                        "   - Степени пиши красиво: x², x³, x₀\n"
                        "   - Корни пиши знаком: √3, √(D)\n"
                        "   - Дроби пиши через слэш: (-4)/2 или (4 ± 4√3)/2\n"
                        "   - Знаки пиши символами: ±, ≈, ≠, ≤, ≥, стрелку через '=>' или словом 'следовательно', объединение значком U или словом 'и'.\n"
                        "3. Не используй решетки '###' для заголовков. Выделяй главное жирным шрифтом.\n"
                        "4. Ответ должен легко и приятно читаться на экране телефона без всякого мусора и лишнего кода."
                    )
                ),
            )
            return response.text
        except Exception as e:
            err_str = str(e).lower()
            last_error = e
            if "429" in err_str or "quota" in err_str or "resource_exhausted" in err_str:
                print(f"[!] Лимит ключа #{index + 1} исчерпан. Переключаю на запасной ключ...")
                continue
            else:
                raise e

    raise Exception(f"Все ключи Gemini исчерпали лимиты! Ошибка: {last_error}")

@dp.message(CommandStart())
async def start_cmd(message: types.Message):
    await message.answer(
        "👋 Привет! Я твой личный помощник на базе Google Gemini.\n\n"
        "• Можешь писать мне любые вопросы.\n"
        "• Можешь кидать фотографии задач — я всё решу без непонятных знаков и кода!"
    )

@dp.message(F.text)
async def handle_text(message: types.Message):
    await bot.send_chat_action(chat_id=message.chat.id, action="typing")
    try:
        answer = await ask_gemini(message.text)
        answer = answer or "Нейросеть не смогла сгенерировать ответ."
        for i in range(0, len(answer), 4000):
            await message.answer(answer[i:i + 4000])
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

        caption = message.caption or "Объясни подробно всё, что изображено на фото, и реши задание."

        answer = await ask_gemini([caption, image])
        answer = answer or "Не удалось разобрать фото."
        for i in range(0, len(answer), 4000):
            await message.answer(answer[i:i + 4000])
    except Exception as e:
        await message.answer(f"⚠️ Ошибка при обработке фото: {e}")

# Запуск телеграм-бота ровно один раз в фоновом потоке
@st.cache_resource
def start_bot_background():
    def run_polling():
        asyncio.run(dp.start_polling(bot))

    t = threading.Thread(target=run_polling, daemon=True)
    t.start()
    return t

start_bot_background()

# Интерфейс для страницы Streamlit
st.title("🤖 Telegram Gemini Bot")
st.success("✅ Бот успешно запущен в облаке и работает 24/7!")
st.info("Теперь компьютер можно выключать — бот отвечает прямо из дата-центра.")
