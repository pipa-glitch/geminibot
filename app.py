import io
import re
import asyncio
import threading
from datetime import datetime, timezone, timedelta
from PIL import Image
import streamlit as st
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart, Command
from google import genai
from google.genai import types as gtypes

# Ключи и токен из сейфа Streamlit
TELEGRAM_TOKEN = st.secrets["TELEGRAM_TOKEN"]
GEMINI_KEYS = st.secrets["GEMINI_KEYS"]

bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()

# Память диалогов для каждого пользователя
user_history = {}

async def safe_send(message: types.Message, text: str):
    # Проверяем, не решила ли нейросеть отправить ответ файлом
    if text.strip().startswith("SEND_FILE:"):
        lines = text.strip().split("\n", 1)
        filename = lines[0].replace("SEND_FILE:", "").strip() or "code.py"
        file_content = lines[1] if len(lines) > 1 else ""

        # Очищаем от лишних кавычек ```python ... ```, если нейросеть их добавила
        file_content = re.sub(r"^```[a-zA-Z0-9_-]*\n", "", file_content.strip())
        file_content = re.sub(r"\n```$", "", file_content.strip())

        doc = types.BufferedInputFile(file_content.encode("utf-8"), filename=filename)
        await message.answer_document(doc, caption=f"📄 Готово! Твой файл: {filename}")
        return

    # Обычная отправка текста
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

    # Берём историю переписки пользователя (храним последние 14 сообщений)
    history = user_history.get(user_id, [])

    # Собираем контекст диалога + текущее сообщение
    full_contents = []
    for item in history:
        full_contents.append(item)
    full_contents.append(new_content)

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
                        "4. ОТПРАВКА ФАЙЛОВ: У тебя ЕСТЬ функция отправки файлов! Никогда не говори, что не можешь отправлять файлы. "
                        "Если пользователь просит отправить код или текст файлом (например: 'отправь файлом', 'скинь .py', 'сделай файлом'), "
                        "напиши в САМОЙ ПЕРВОЙ СТРОКЕ ответа строго: SEND_FILE:имя_файла.расширение (например SEND_FILE:game.py или SEND_FILE:essay.txt), "
                        "а со следующей строки напиши ТОЛЬКО чистый код или текст для этого файла без лишних комментариев."
                    )
                ),
            )
            answer_text = response.text or "Пустой ответ."

            # Сохраняем вопрос и ответ в память диалога
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
        "• Я помню контекст нашего разговора!\n"
        "• Присылай фотки задач или вопросы.\n"
        "• Напиши «отправь файлом» — и я скину любой код или текст готовым файлом!\n"
        "• Команда /clear — очистить память диалога."
    )

@dp.message(Command("clear"))
async def clear_cmd(message: types.Message):
    user_history[message.from_user.id] = []
    await message.answer("🧹 Память диалога очищена! Можем начать новую тему.")

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
def start_bot_background_v2():
    def worker():
        asyncio.run(start_aiogram_loop())

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    return t

start_bot_background_v2()

st.title("🤖 Telegram Gemini Bot v2.0")
st.success("✅ Бот с памятью и отправкой файлов работает 24/7!")
