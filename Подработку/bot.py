import asyncio
import logging
import os
import sqlite3
import io
from datetime import datetime
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from openai import AsyncOpenAI

# --- Загрузка переменных ---
load_dotenv()
BOT_TOKEN = os.getenv('BOT_TOKEN')
ADMIN_ID = int(os.getenv('ADMIN_ID', 0))
CHANNEL_ID = int(os.getenv('CHANNEL_ID', 0))
OPENROUTER_API_KEY = os.getenv('OPENROUTER_API_KEY')

if not BOT_TOKEN:
    raise ValueError("Не найден BOT_TOKEN в файле .env!")

# --- Настройка логирования ---
logging.basicConfig(level=logging.INFO)

# --- Инициализация бота ---
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# --- Инициализация OpenRouter клиента ---
client = AsyncOpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=OPENROUTER_API_KEY,
)

# --- Класс для хранения состояния (FSM) ---
class QuestionState(StatesGroup):
    waiting_for_question = State()

# --- Клавиатура для запроса контакта ---
contact_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📱 Отправить номер телефона", request_contact=True)]
    ],
    resize_keyboard=True,
    one_time_keyboard=True
)

# --- Клавиатура с кнопкой "Назад" (для диалога) ---
dialog_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🔙 Назад в меню")]
    ],
    resize_keyboard=True
)

# --- Главная клавиатура после регистрации ---
main_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="💰 Получить доступ к заданиям")],
        [KeyboardButton(text="❓ Задать вопрос")],
        [KeyboardButton(text="📊 Моя статистика")]
    ],
    resize_keyboard=True
)

# ============================================
# === БАЗА ДАННЫХ (SQLite) ===
# ============================================

def init_db():
    """Создаёт таблицу пользователей, если её нет"""
    conn = sqlite3.connect('database.db')
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            first_name TEXT,
            phone TEXT,
            username TEXT,
            registered_at TEXT,
            tasks_completed INTEGER DEFAULT 0,
            total_earned INTEGER DEFAULT 0
        )
    ''')
    conn.commit()
    conn.close()
    logging.info("✅ База данных инициализирована")

def save_user(user_id: int, first_name: str, phone: str, username: str = ""):
    """Сохраняет или обновляет пользователя в базе"""
    conn = sqlite3.connect('database.db')
    cursor = conn.cursor()
    cursor.execute('''
        INSERT OR REPLACE INTO users 
        (user_id, first_name, phone, username, registered_at)
        VALUES (?, ?, ?, ?, ?)
    ''', (user_id, first_name, phone, username, datetime.now().isoformat()))
    conn.commit()
    conn.close()
    logging.info(f"✅ Пользователь {user_id} сохранен в SQLite")

def get_user(user_id: int):
    """Получает данные пользователя из базы"""
    conn = sqlite3.connect('database.db')
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM users WHERE user_id = ?', (user_id,))
    row = cursor.fetchone()
    conn.close()
    if row:
        return {
            'user_id': row[0],
            'first_name': row[1],
            'phone': row[2],
            'username': row[3],
            'registered_at': row[4],
            'tasks_completed': row[5],
            'total_earned': row[6]
        }
    return None

def get_all_users():
    """Получает всех пользователей из базы"""
    conn = sqlite3.connect('database.db')
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM users ORDER BY registered_at DESC')
    rows = cursor.fetchall()
    conn.close()
    return [{
        'user_id': row[0],
        'first_name': row[1],
        'phone': row[2],
        'username': row[3],
        'registered_at': row[4],
        'tasks_completed': row[5],
        'total_earned': row[6]
    } for row in rows]

def update_user_stats(user_id: int, tasks_done: int = 0, earned: int = 0):
    """Обновляет статистику пользователя"""
    conn = sqlite3.connect('database.db')
    cursor = conn.cursor()
    cursor.execute('''
        UPDATE users 
        SET tasks_completed = tasks_completed + ?,
            total_earned = total_earned + ?
        WHERE user_id = ?
    ''', (tasks_done, earned, user_id))
    conn.commit()
    conn.close()
    logging.info(f"✅ Статистика пользователя {user_id} обновлена")

# ============================================
# === ОТПРАВКА В TELEGRAM-КАНАЛ ===
# ============================================

async def send_to_channel(user_id: int, first_name: str, phone: str, username: str = ""):
    """Отправляет данные пользователя в канал"""
    if not CHANNEL_ID:
        return False
    
    try:
        text = (
            f"🆕 **Новый пользователь**\n\n"
            f"🆔 ID: `{user_id}`\n"
            f"👤 Имя: {first_name}\n"
            f"📱 Телефон: {phone}\n"
            f"👤 Username: @{username}\n"
            f"📅 Дата: {datetime.now().strftime('%d.%m.%Y %H:%M')}"
        )
        
        await bot.send_message(
            chat_id=CHANNEL_ID,
            text=text,
            parse_mode="Markdown"
        )
        logging.info(f"✅ Данные пользователя {user_id} отправлены в канал")
        return True
    except Exception as e:
        logging.error(f"❌ Ошибка отправки в канал: {e}")
        return False

# ============================================
# === OPENROUTER AI ===
# ============================================

async def get_ai_response(user_question: str) -> str:
    """Отправляет вопрос в OpenRouter и возвращает ответ"""
    
    if not OPENROUTER_API_KEY:
        return "❌ AI-помощник временно недоступен. Попробуй позже."
    
    try:
        completion = await client.chat.completions.create(
            model="openrouter/free",
            messages=[
                {
                    "role": "system", 
                    "content": (
                        "Ты — дружелюбный помощник в телеграм-канале по заработку для подростков 14-17 лет.\n\n"
                        "Твоя главная задача: помочь школьнику разобраться, если он что-то не понял.\n\n"
                        "Правила общения:\n"
                        "1. Говори простым языком, как старший брат или сестра. Никаких сложных терминов.\n"
                        "2. Отвечай коротко и по делу (2-3 предложения, максимум 5).\n"
                        "3. Если вопрос непонятный — уточни, что именно имеется в виду.\n"
                        "4. Если вопрос про задания — объясни, что это легко, быстро и безопасно. НЕ давай определений из словаря.\n"
                        "5. Если вопрос про выплаты — скажи, что деньги приходят на карту моментально.\n"
                        "6. Если вопрос про регистрацию — объясни, что нужно просто отправить номер телефона.\n"
                        "7. Будь позитивным и поддерживающим. Используй эмодзи: 😊 👍 🔥 💪\n"
                        "8. НЕ используй ссылки, НЕ цитируй Википедию, НЕ давай сухих определений.\n\n"
                        "Примеры ответов:\n"
                        "Вопрос: 'А это сложно?'\n"
                        "Ответ: 'Нет, всё очень просто! Задания занимают 5-10 минут. Даже если никогда не делал — мы всё объясним 👍'\n\n"
                        "Вопрос: 'А деньги точно придут?'\n"
                        "Ответ: 'Да, конечно! Все выплаты приходят на карту сразу после выполнения. Мы проверяем каждого, кто платит 😊'\n\n"
                        "Вопрос: 'А если я не пойму что делать?'\n"
                        "Ответ: 'Тогда просто напиши мне! Я объясню по шагам, что нужно сделать. Ничего сложного, обещаю 💪'\n\n"
                        "Вопрос: 'Что такое задания?'\n"
                        "Ответ: 'Это простые действия: ответить на пару вопросов, установить приложение или зарегистрироваться. Всё занимает 5-10 минут 👍'\n\n"
                        "Запомни: ты — помощник, а не учитель. Твоя задача — снять страхи и сомнения, чтобы подросток чувствовал себя уверенно."
                    )
                },
                {
                    "role": "user", 
                    "content": user_question
                }
            ],
            timeout=30.0
        )
        
        return completion.choices[0].message.content or "Извините, я не смог сформировать ответ."
        
    except Exception as e:
        logging.error(f"❌ Ошибка OpenRouter API: {e}")
        return "❌ Произошла техническая ошибка. Попробуйте позже."

# ============================================
# === КОМАНДЫ БОТА ===
# ============================================

# --- Команда /start ---
@dp.message(Command("start"))
async def start_command(message: Message, state: FSMContext):
    user_id = message.from_user.id
    first_name = message.from_user.first_name
    
    # Очищаем состояние (выход из режима диалога)
    await state.clear()
    
    existing_user = get_user(user_id)
    
    if existing_user and existing_user.get('phone'):
        await message.answer(
            f"👋 С возвращением, {first_name}!\n\n"
            "Ты уже зарегистрирован в системе.\n"
            "Нажми кнопку ниже, чтобы получить доступ к заданиям.",
            reply_markup=main_keyboard
        )
    else:
        await message.answer(
            f"👋 Привет, {first_name}!\n\n"
            "Добро пожаловать в Центр Заработка!\n"
            "Для начала работы мне нужен твой номер телефона.\n"
            "Это нужно для идентификации и выплат.\n\n"
            "Нажми на кнопку ниже:",
            reply_markup=contact_keyboard
        )

# --- Обработчик получения контакта ---
@dp.message(lambda message: message.contact is not None)
async def handle_contact(message: Message):
    user_id = message.from_user.id
    contact = message.contact
    phone_number = contact.phone_number
    first_name = contact.first_name or message.from_user.first_name
    username = message.from_user.username or ""
    
    save_user(user_id, first_name, phone_number, username)
    await send_to_channel(user_id, first_name, phone_number, username)
    
    await message.answer(
        f"✅ Отлично, {first_name}!\n\n"
        f"Твой номер **{phone_number}** сохранен.\n\n"
        "Теперь ты можешь:\n"
        "• Получать доступ к заданиям\n"
        "• Задавать вопросы нашему AI-помощнику\n"
        "• Отслеживать свою статистику\n\n"
        "Нажми на кнопку ниже, чтобы начать:",
        reply_markup=main_keyboard,
        parse_mode="Markdown"
    )
    
    await bot.send_message(
        ADMIN_ID,
        f"🆕 **Новый пользователь!**\n\n"
        f"👤 Имя: {first_name}\n"
        f"📱 Телефон: {phone_number}\n"
        f"🆔 ID: {user_id}\n"
        f"👤 Username: @{username}\n"
        f"📅 Дата: {datetime.now().strftime('%d.%m.%Y %H:%M')}",
        parse_mode="Markdown"
    )

# --- Обработчик кнопки "Назад в меню" ---
@dp.message(lambda message: message.text == "🔙 Назад в меню")
async def back_to_menu(message: Message, state: FSMContext):
    await state.clear()
    
    await message.answer(
        "✅ Ты вернулся в главное меню.\n"
        "Выбери нужный пункт:",
        reply_markup=main_keyboard
    )

# --- Обработчик кнопки "Получить доступ к заданиям" ---
@dp.message(lambda message: message.text == "💰 Получить доступ к заданиям")
async def get_tasks(message: Message):
    user_id = message.from_user.id
    user = get_user(user_id)
    
    if not user or not user.get('phone'):
        await message.answer(
            "❌ Сначала зарегистрируйся!\n"
            "Напиши /start и отправь свой номер телефона."
        )
        return
    
    # --- ТВОИ РЕФЕРАЛЬНЫЕ ССЫЛКИ (ЗАМЕНИ НА СВОИ) ---
    REFERRAL_URL = "https://t.me/YourBot?start=ref_123456"  # ЗАМЕНИ
    EXTRA_URL = "https://example.com/app"  # ЗАМЕНИ
    
    tasks_keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(
                text="🚀 Получить доступ к заданиям",
                url=REFERRAL_URL
            )],
            [InlineKeyboardButton(
                text="📱 Скачать приложение",
                url=EXTRA_URL
            )]
        ]
    )
    
    update_user_stats(user_id, tasks_done=1, earned=0)
    
    await message.answer(
        f"🔥 **Доступ к заданиям**\n\n"
        f"Сегодня доступны следующие задания:\n"
        f"• Опросы — от 150₽\n"
        f"• Установка приложений — от 250₽\n"
        f"• Регистрации — от 100₽\n"
        f"• Тестирования — от 200₽\n\n"
        f"Для получения доступа нажми на кнопку ниже:\n"
        f"⚠️ Требуется верификация (занимает 2 минуты)",
        reply_markup=tasks_keyboard,
        parse_mode="Markdown"
    )

# --- Обработчик кнопки "Задать вопрос" ---
@dp.message(lambda message: message.text == "❓ Задать вопрос")
async def ask_question(message: Message, state: FSMContext):
    user_id = message.from_user.id
    user = get_user(user_id)
    
    if not user or not user.get('phone'):
        await message.answer(
            "❌ Сначала зарегистрируйся!\n"
            "Напиши /start и отправь свой номер телефона."
        )
        return
    
    await state.set_state(QuestionState.waiting_for_question)
    
    await message.answer(
        "💬 Задай свой вопрос\n\n"
        "Просто напиши свой вопрос.\n"
        "Нажми «Назад в меню», чтобы выйти.",
        reply_markup=dialog_keyboard
    )

# --- Обработчик вопроса (AI-агент через OpenRouter) ---
@dp.message(QuestionState.waiting_for_question)
async def handle_question(message: Message, state: FSMContext):
    if message.text == "🔙 Назад в меню":
        await back_to_menu(message, state)
        return
    
    user_text = message.text
    user_id = message.from_user.id
    
    processing_msg = await message.answer("🤔 Думаю над ответом...")
    
    try:
        response = await get_ai_response(user_text)
        
        await processing_msg.delete()
        await message.answer(
            f"💬 **Ответ:**\n\n{response}\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"❓ Можешь задать ещё вопрос или нажми «Назад в меню».\n"
            f"👨‍💻 Поддержка: @olecrypto1",
            parse_mode="Markdown",
            reply_markup=dialog_keyboard
        )
        
    except Exception as e:
        logging.error(f"❌ Ошибка AI: {e}")
        await processing_msg.edit_text(
            "❌ Произошла техническая ошибка. Попробуйте позже.\n\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            "👨‍💻 Поддержка: @olecrypto1",
            reply_markup=dialog_keyboard
        )

# --- Обработчик кнопки "Моя статистика" ---
@dp.message(lambda message: message.text == "📊 Моя статистика")
async def show_stats(message: Message):
    user_id = message.from_user.id
    user = get_user(user_id)
    
    if not user or not user.get('phone'):
        await message.answer(
            "❌ Сначала зарегистрируйся!\n"
            "Напиши /start и отправь свой номер телефона."
        )
        return
    
    stats_text = (
        f"📊 **Твоя статистика**\n\n"
        f"👤 Имя: {user.get('first_name', 'Не указано')}\n"
        f"📱 Телефон: {user.get('phone', 'Не указан')}\n"
        f"✅ Выполнено заданий: {user.get('tasks_completed', 0)}\n"
        f"💰 Заработано: {user.get('total_earned', 0)}₽\n"
        f"📅 Зарегистрирован: {user.get('registered_at', 'Неизвестно')[:10]}\n\n"
        f"🔥 Продолжай в том же духе!"
    )
    
    await message.answer(stats_text, parse_mode="Markdown", reply_markup=main_keyboard)

# --- Команда /stats (для админа) ---
@dp.message(Command("stats"))
async def admin_stats(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ У тебя нет прав для этой команды.")
        return
    
    users = get_all_users()
    
    if not users:
        await message.answer("📊 Пока нет зарегистрированных пользователей.")
        return
    
    total_users = len(users)
    
    stats_text = f"📊 **Статистика бота**\n\n"
    stats_text += f"👥 Всего пользователей: {total_users}\n"
    stats_text += f"📁 Хранилище: SQLite + Telegram-канал\n"
    stats_text += f"🤖 AI: OpenRouter (openrouter/free)\n\n"
    stats_text += "**Последние 10 пользователей:**\n"
    
    for user in users[:10]:
        name = user.get('first_name', 'Без имени')
        phone = user.get('phone', 'Нет номера')
        date = user.get('registered_at', '')[:10]
        stats_text += f"• {name} | {phone} | {date}\n"
    
    await message.answer(stats_text, parse_mode="Markdown")

# --- Команда /export (для админа) ---
@dp.message(Command("export"))
async def export_users(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ У тебя нет прав для этой команды.")
        return
    
    users = get_all_users()
    
    if not users:
        await message.answer("Нет пользователей для экспорта.")
        return
    
    csv_data = "ID,Имя,Телефон,Username,Дата регистрации,Заданий,Заработано\n"
    for user in users:
        csv_data += f"{user.get('user_id', '')}," \
                    f"{user.get('first_name', '')}," \
                    f"{user.get('phone', '')}," \
                    f"{user.get('username', '')}," \
                    f"{user.get('registered_at', '')}," \
                    f"{user.get('tasks_completed', 0)}," \
                    f"{user.get('total_earned', 0)}\n"
    
    file = io.BytesIO(csv_data.encode('utf-8'))
    await message.answer_document(
        types.BufferedInputFile(file.getvalue(), filename='users_export.csv'),
        caption=f"📊 Экспорт пользователей ({len(users)} записей)"
    )

# --- Команда /broadcast (для админа) ---
@dp.message(Command("broadcast"))
async def broadcast(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ У тебя нет прав для этой команды.")
        return
    
    text = message.text.replace("/broadcast", "").strip()
    if not text:
        await message.answer("📝 Использование: /broadcast текст сообщения")
        return
    
    users = get_all_users()
    
    if not users:
        await message.answer("Нет пользователей для рассылки.")
        return
    
    sent = 0
    failed = 0
    
    await message.answer(f"📤 Начинаю рассылку для {len(users)} пользователей...")
    
    for user in users:
        try:
            await bot.send_message(
                user.get('user_id'),
                f"📢 **Важное сообщение от Центра Заработка**\n\n{text}",
                parse_mode="Markdown"
            )
            sent += 1
            await asyncio.sleep(0.3)
        except Exception as e:
            logging.error(f"❌ Ошибка отправки {user.get('user_id')}: {e}")
            failed += 1
    
    await message.answer(
        f"✅ Рассылка завершена!\n"
        f"📨 Отправлено: {sent}\n"
        f"❌ Ошибок: {failed}"
    )

# --- Обработка всех остальных сообщений ---
@dp.message()
async def handle_other_messages(message: Message):
    await message.answer(
        "🤔 Я не понял команду.\n\n"
        "Используй кнопки меню или напиши:\n"
        "/start - начать заново\n\n"
        "Если хочешь задать вопрос - нажми кнопку '❓ Задать вопрос'",
        reply_markup=main_keyboard
    )

# ============================================
# === ЗАПУСК БОТА ===
# ============================================

async def main():
    init_db()
    
    logging.info("🚀 Бот запущен...")
    
    if CHANNEL_ID:
        logging.info(f"📁 Данные дублируются в канал {CHANNEL_ID}")
    else:
        logging.warning("⚠️ CHANNEL_ID не указан!")
    
    if OPENROUTER_API_KEY:
        logging.info("🤖 AI-агент: OpenRouter (openrouter/free) - активен")
    else:
        logging.warning("⚠️ OpenRouter API ключ не найден!")
    
    logging.info("📊 Для просмотра статистики используй /stats")
    await dp.start_polling(bot, skip_updates=True)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Бот остановлен")
