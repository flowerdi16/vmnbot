import asyncio
import json
import html
import os
import re
import sqlite3
from collections import defaultdict

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message,
    KeyboardButton,
    ReplyKeyboardMarkup,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    CallbackQuery,
    InputMediaPhoto,
    InputMediaVideo,
    InputMediaDocument,
    MessageEntity,
    ReplyParameters,
)
from dotenv import load_dotenv


# =========================================================
# НАСТРОЙКИ
# =========================================================

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_CHAT_ID_1 = int(os.getenv("ADMIN_CHAT_ID_1"))
ADMIN_CHAT_ID_2 = int(os.getenv("ADMIN_CHAT_ID_2"))
CHANNEL_ID = int(os.getenv("CHANNEL_ID"))

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не найден в .env")


bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


ADMIN_CHATS = {
    ADMIN_CHAT_ID_1,
    ADMIN_CHAT_ID_2
}


# =========================================================
# СОСТОЯНИЯ
# =========================================================

class TakeState(StatesGroup):

    # Тейки
    waiting_for_anonymous_take = State()
    waiting_for_non_anonymous_take = State()

    # Вопросы
    waiting_for_anonymous_question = State()
    waiting_for_non_anonymous_question = State()

    # Анкета
    waiting_for_admin_application = State()


class TemplateState(StatesGroup):

    waiting_for_template = State()


# =========================================================
# АЛЬБОМЫ
# =========================================================

album_buffer = defaultdict(list)
album_tasks = {}


# =========================================================
# БАЗА ДАННЫХ
# =========================================================

def db_connect():
    return sqlite3.connect("bot.db")


def init_db():

    conn = db_connect()
    cursor = conn.cursor()

    # -----------------------------------------------------
    # Основная таблица
    # -----------------------------------------------------

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS submissions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            anonymous INTEGER NOT NULL,
            text TEXT,
            media_type TEXT,
            file_id TEXT,
            status TEXT DEFAULT 'new',
            admin_message_id_1 INTEGER,
            admin_message_id_2 INTEGER
        )
        """
    )

    cursor.execute(
        "PRAGMA table_info(submissions)"
    )

    columns = [
        row[1]
        for row in cursor.fetchall()
    ]

    if "submission_type" not in columns:

        cursor.execute(
            """
            ALTER TABLE submissions
            ADD COLUMN submission_type TEXT DEFAULT 'take'
            """
        )

    if "admin_media_ids_1" not in columns:

        cursor.execute(
            """
            ALTER TABLE submissions
            ADD COLUMN admin_media_ids_1 TEXT
            """
        )

    if "admin_media_ids_2" not in columns:

        cursor.execute(
            """
            ALTER TABLE submissions
            ADD COLUMN admin_media_ids_2 TEXT
            """
        )

    if "user_message_id" not in columns:

        cursor.execute(
            """
            ALTER TABLE submissions
            ADD COLUMN user_message_id INTEGER
            """
        )

    if "user_entities" not in columns:

        cursor.execute(
            """
            ALTER TABLE submissions
            ADD COLUMN user_entities TEXT
            """
        )

    # -----------------------------------------------------
    # Таблица шаблонов
    # -----------------------------------------------------

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS templates (
            template_type TEXT PRIMARY KEY,
            template_text TEXT NOT NULL,
            template_entities TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    # -----------------------------------------------------
    # Заблокированные пользователи
    # -----------------------------------------------------

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS banned_users (
            user_id INTEGER PRIMARY KEY,
            banned_by INTEGER,
            banned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    conn.commit()
    conn.close()


init_db()


# =========================================================
# ГЛАВНОЕ МЕНЮ
# =========================================================

def main_menu():

    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="Анонимный тейк"),
                KeyboardButton(text="Неанонимный тейк")
            ],
            [
                KeyboardButton(text="Задать вопрос администрации")
            ],
            [
                KeyboardButton(text="Стать админом!"),
                KeyboardButton(text="Правила")
            ]
        ],
        resize_keyboard=True
    )


# =========================================================
# МЕНЮ ВОПРОСОВ
# =========================================================

def question_menu():

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Анонимный вопрос",
                    callback_data="question:anonymous"
                )
            ],
            [
                InlineKeyboardButton(
                    text="Неанонимный вопрос",
                    callback_data="question:nonanonymous"
                )
            ]
        ]
    )


# =========================================================
# МЕНЮ /VID
# =========================================================

def template_menu():

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Оформление анон тейка",
                    callback_data="vid:anonymous"
                )
            ],
            [
                InlineKeyboardButton(
                    text="Оформление неанон тейка",
                    callback_data="vid:nonanonymous"
                )
            ]
        ]
    )


# =========================================================
# КНОПКА ПУБЛИКАЦИИ
# =========================================================

def publish_keyboard(submission_id):

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Опубликовать",
                    callback_data=f"publish:{submission_id}"
                )
            ]
        ]
    )


# =========================================================
# ПРОВЕРКА АДМИНА
# =========================================================

def is_admin_chat(message: Message):

    return message.chat.id in ADMIN_CHATS


def is_user_banned(user_id):

    conn = db_connect()
    cursor = conn.cursor()

    cursor.execute(
        "SELECT 1 FROM banned_users WHERE user_id = ? LIMIT 1",
        (user_id,)
    )

    result = cursor.fetchone()
    conn.close()

    return result is not None


def ban_user(user_id, banned_by):

    conn = db_connect()
    cursor = conn.cursor()

    cursor.execute(
        """
        INSERT OR REPLACE INTO banned_users (user_id, banned_by)
        VALUES (?, ?)
        """,
        (user_id, banned_by)
    )

    conn.commit()
    conn.close()


def unban_user(user_id):

    conn = db_connect()
    cursor = conn.cursor()

    cursor.execute(
        "DELETE FROM banned_users WHERE user_id = ?",
        (user_id,)
    )

    deleted = cursor.rowcount > 0

    conn.commit()
    conn.close()

    return deleted


# =========================================================
# АВТОР
# =========================================================

def get_author_text(user):

    name = user.full_name or "Без имени"

    if user.username:

        return f"@{user.username}"

    return (
        f"{name}\n"
        f"ID: {user.id}"
    )


def build_admin_content(message: Message, header: str):

    text = message.text or message.caption or ""

    full_text = (
        f"{header}\n\n{text}"
        if text
        else header
    )

    entities = []

    if text and message.entities:

        shift = utf16_length(f"{header}\n\n")

        for entity in message.entities:

            data = {
                "type": entity.type,
                "offset": entity.offset + shift,
                "length": entity.length
            }

            if entity.url is not None:
                data["url"] = entity.url

            if entity.language is not None:
                data["language"] = entity.language

            if entity.custom_emoji_id is not None:
                data["custom_emoji_id"] = entity.custom_emoji_id

            if entity.user is not None:
                data["user"] = entity.user

            entities.append(MessageEntity(**data))

    if not message.from_user.username:

        id_text = str(message.from_user.id)
        id_pos = header.rfind(id_text)

        if id_pos != -1:

            entities.append(
                MessageEntity(
                    type="text_link",
                    offset=utf16_length(header[:id_pos]),
                    length=utf16_length(id_text),
                    url=f"tg://user?id={message.from_user.id}"
                )
            )

    return full_text, entities


# =========================================================
# БЕЗОПАСНЫЙ CALLBACK
# =========================================================

async def safe_callback_answer(
    callback: CallbackQuery,
    text: str,
    show_alert: bool = False
):

    try:

        await callback.answer(
            text,
            show_alert=show_alert
        )

    except TelegramBadRequest:

        pass


# =========================================================
# UTF-16
# =========================================================

def utf16_length(text: str) -> int:

    return len(
        text.encode("utf-16-le")
    ) // 2


# =========================================================
# СЕРИАЛИЗАЦИЯ ENTITIES
# =========================================================

def serialize_entities(entities):

    if not entities:
        return []

    result = []

    for entity in entities:

        data = {
            "type": entity.type,
            "offset": entity.offset,
            "length": entity.length
        }

        if entity.url is not None:
            data["url"] = entity.url

        if entity.language is not None:
            data["language"] = entity.language

        if entity.custom_emoji_id is not None:
            data["custom_emoji_id"] = entity.custom_emoji_id

        if entity.user is not None:
            try:
                data["user"] = entity.user.model_dump(
                    mode="json",
                    exclude_none=True
                )
            except Exception:
                pass

        result.append(data)

    return result
# =========================================================
# ПОЛУЧЕНИЕ ШАБЛОНА
# =========================================================

def get_template(template_type):

    conn = db_connect()
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT
            template_text,
            template_entities
        FROM templates
        WHERE template_type = ?
        """,
        (template_type,)
    )

    result = cursor.fetchone()

    conn.close()

    if not result:

        return None

    return result[0], result[1]


# =========================================================
# СОХРАНЕНИЕ ШАБЛОНА
# =========================================================

def save_template(
    template_type,
    text,
    entities
):

    conn = db_connect()
    cursor = conn.cursor()

    entities_json = json.dumps(
        serialize_entities(entities),
        ensure_ascii=False
    )

    cursor.execute(
        """
        INSERT INTO templates (
            template_type,
            template_text,
            template_entities
        )
        VALUES (?, ?, ?)
        ON CONFLICT(template_type)
        DO UPDATE SET
            template_text = excluded.template_text,
            template_entities = excluded.template_entities,
            updated_at = CURRENT_TIMESTAMP
        """,
        (
            template_type,
            text,
            entities_json
        )
    )

    conn.commit()
    conn.close()

def deserialize_entities(data):
    if not data:
        return []

    try:
        raw_entities = json.loads(data)
    except Exception:
        return []

    result = []

    for item in raw_entities:
        try:
            result.append(MessageEntity(**item))
        except Exception as error:
            print("Ошибка восстановления entity:", repr(error))

    return result

# =========================================================
# ПРОВЕРКА ШАБЛОНА
# =========================================================

def validate_template(
    text,
    entities
):

    if "{text}" not in text:

        return (
            False,
            "❌ В шаблоне нет `{text}`.\n\n"
            "Добавь `{text}` туда, куда бот должен вставлять тейк."
        )

    markers = [
        "{text}",
        "{username}",
        "{name}"
    ]

    # Переменные должны быть целыми entities или находиться
    # внутри entity целиком. Частичное пересечение запрещаем.
    for marker in markers:

        search_from = 0

        while True:

            position = text.find(marker, search_from)

            if position == -1:
                break

            marker_start = utf16_length(text[:position])
            marker_end = marker_start + utf16_length(marker)

            for entity in entities or []:

                entity_start = entity.offset
                entity_end = entity.offset + entity.length

                # Entity полностью до/после переменной.
                if entity_end <= marker_start or entity_start >= marker_end:
                    continue

                # Entity полностью покрывает переменную:
                # например жирный {text}. Это разрешено.
                if entity_start <= marker_start and entity_end >= marker_end:
                    continue

                # Entity ровно совпадает с переменной:
                # например жирный только {text}. Разрешено.
                if entity_start == marker_start and entity_end == marker_end:
                    continue

                # Entity частично пересекает переменную — такое
                # оформление невозможно корректно перенести.
                return (
                    False,
                    f"❌ Форматирование частично пересекает `{marker}`.\n\n"
                    f"Можно выделить `{marker}` целиком (например жирным), "
                    f"но нельзя выделить только его часть."
                )

            search_from = position + len(marker)

    return True, None


# =========================================================
# ПРИМЕНЕНИЕ ШАБЛОНА
# =========================================================

def apply_template(
    template_text,
    template_entities,
    replacements,
    user_entities=None
):

    markers = [
        "{text}",
        "{username}",
        "{name}"
    ]

    replacements_list = []

    for marker in markers:

        value = replacements.get(marker, marker)

        search_from = 0

        while True:

            position = template_text.find(marker, search_from)

            if position == -1:
                break

            start_u16 = utf16_length(template_text[:position])
            end_u16 = start_u16 + utf16_length(marker)

            replacements_list.append({
                "start_u16": start_u16,
                "end_u16": end_u16,
                "start_py": position,
                "end_py": position + len(marker),
                "value": value,
                "marker": marker
            })

            search_from = position + len(marker)

    if not replacements_list:
        return template_text, template_entities or []

    replacements_list.sort(key=lambda item: item["start_py"])

    # -----------------------------------------------------
    # Собираем итоговый текст.
    # -----------------------------------------------------

    parts = []
    last_position = 0

    for replacement in replacements_list:

        parts.append(
            template_text[
                last_position:replacement["start_py"]
            ]
        )
        parts.append(replacement["value"])
        last_position = replacement["end_py"]

    parts.append(template_text[last_position:])

    final_text = "".join(parts)

    # -----------------------------------------------------
    # Переносим только entities ШАБЛОНА.
    # Entities исходного пользовательского сообщения сюда
    # вообще не передаются, поэтому пользователь не может
    # изменить оформление публикации.
    # -----------------------------------------------------

    final_entities = []

    for entity in template_entities or []:

        original_start = entity.offset
        original_end = entity.offset + entity.length
        new_start = original_start
        new_end = original_end
        valid = True

        for replacement in replacements_list:

            marker_start = replacement["start_u16"]
            marker_end = replacement["end_u16"]
            marker_length = marker_end - marker_start
            replacement_length = utf16_length(replacement["value"])
            delta = replacement_length - marker_length

            # Entity полностью после переменной — сдвигаем.
            if original_start >= marker_end:
                new_start += delta
                new_end += delta
                continue

            # Entity полностью до переменной — ничего не делаем.
            if original_end <= marker_start:
                continue

            # Entity полностью содержит переменную.
            # Например: **{text}**.
            if original_start <= marker_start and original_end >= marker_end:
                new_end += delta
                continue

            # Entity ровно равна переменной.
            # Например: **{text}** с entity только на {text}.
            if original_start == marker_start and original_end == marker_end:
                new_end = new_start + replacement_length
                continue

            valid = False
            break

        if not valid:
            raise ValueError(
                "Одно из форматирований шаблона частично "
                "пересекает переменную. Форматируй переменную "
                "целиком или не затрагивай её."
            )

        entity_data = {
            "type": entity.type,
            "offset": new_start,
            "length": new_end - new_start
        }

        if entity.url is not None:
            entity_data["url"] = entity.url

        if entity.language is not None:
            entity_data["language"] = entity.language

        if entity.custom_emoji_id is not None:
            entity_data["custom_emoji_id"] = entity.custom_emoji_id

        if entity.user is not None:
            entity_data["user"] = entity.user

        final_entities.append(MessageEntity(**entity_data))

    # -----------------------------------------------------
    # Переносим форматирование исходного текста пользователя.
    # Оно относится именно к значению {text}.
    # Например, ссылка внутри слова пользователя сохраняется.
    # -----------------------------------------------------

    user_entities = user_entities or []

    for source_entity in user_entities:

        source_data = {
            "type": source_entity.type,
            "offset": source_entity.offset,
            "length": source_entity.length
        }

        if source_entity.url is not None:
            source_data["url"] = source_entity.url

        if source_entity.language is not None:
            source_data["language"] = source_entity.language

        if source_entity.custom_emoji_id is not None:
            source_data["custom_emoji_id"] = source_entity.custom_emoji_id

        if source_entity.user is not None:
            source_data["user"] = source_entity.user

        # Пользовательские entities находятся внутри исходного {text}.
        # Находим позицию этого маркера в итоговом тексте.
        text_replacement = next(
            (
                item
                for item in replacements_list
                if item["marker"] == "{text}"
            ),
            None
        )

        if text_replacement is None:
            continue

        entity_end = (
            source_entity.offset + source_entity.length
        )

        text_length = utf16_length(
            text_replacement["value"]
        )

        # Защита от повреждённых/устаревших entities.
        if source_entity.offset < 0 or entity_end > text_length:
            continue

        source_data["offset"] = (
            text_replacement["start_u16"]
            + source_entity.offset
        )

        final_entities.append(
            MessageEntity(**source_data)
        )

    return final_text, final_entities

# =========================================================
# START
# =========================================================

@dp.message(CommandStart())
async def start_handler(
    message: Message,
    state: FSMContext
):

    if is_user_banned(message.from_user.id):
        await message.answer("🚫 Вы заблокированы и не можете пользоваться ботом.")
        return

    await state.clear()

    await message.answer(
        "Добро пожаловать!\n\n"
        "Выберите раздел:",
        reply_markup=main_menu()
    )


# =========================================================
# /VID
# =========================================================

@dp.message(
    F.text == "/vid"
)
async def vid_handler(
    message: Message,
    state: FSMContext
):

    if not is_admin_chat(message):

        return

    await state.clear()

    await message.answer(
        "Выбери оформление, которое хочешь изменить:",
        reply_markup=template_menu()
    )


# =========================================================
# ВЫБОР ШАБЛОНА
# =========================================================

@dp.callback_query(
    F.data.startswith("vid:")
)
async def template_type_handler(
    callback: CallbackQuery,
    state: FSMContext
):

    if callback.message.chat.id not in ADMIN_CHATS:

        await safe_callback_answer(
            callback,
            "Нет доступа.",
            show_alert=True
        )

        return

    template_type = (
        callback.data.split(":", 1)[1]
    )

    if template_type == "anonymous":

        title = "Анонимный тейк"

    else:

        title = "Неанонимный тейк"

    await safe_callback_answer(
        callback,
        "Ожидаю шаблон"
    )

    await state.update_data(
        template_type=template_type
    )

    await state.set_state(
        TemplateState.waiting_for_template
    )

    await callback.message.answer(
        f"{title}\n\n"
        "Отправь мне готовый шаблон."
    )


# =========================================================
# ПОЛУЧЕНИЕ ШАБЛОНА
# =========================================================

@dp.message(
    TemplateState.waiting_for_template
)
async def receive_template(
    message: Message,
    state: FSMContext
):

    if message.chat.id not in ADMIN_CHATS:

        return

    if not message.text:

        await message.answer(
            "❌ Шаблон должен быть отправлен "
            "обычным текстовым сообщением.\n\n"
            "Premium Emoji внутри текста сохранятся."
        )

        return

    template_text = message.text

    # В Telegram entities для текста находятся здесь
    template_entities = (
        message.entities
        or []
    )

    valid, error_text = validate_template(
        template_text,
        template_entities
    )

    if not valid:

        await message.answer(
            error_text
        )

        return

    data = await state.get_data()

    template_type = data.get(
        "template_type"
    )

    if not template_type:

        await state.clear()

        await message.answer(
            "❌ Не удалось определить тип шаблона.\n"
            "Начни заново через /vid."
        )

        return

    # -----------------------------------------------------
    # Проверяем, что шаблон реально собирается
    # -----------------------------------------------------

    try:

        preview_text, preview_entities = (
            apply_template(
                template_text,
                template_entities,
                {
                    "{text}": "ТЕКСТ ТЕЙКА",
                    "{username}": "@username",
                    "{name}": "Имя пользователя"
                }
            )
        )

    except Exception as error:

        await state.clear()

        print(
            "Ошибка создания предпросмотра:",
            repr(error)
        )

        await message.answer(
            "❌ Не удалось создать предпросмотр.\n\n"
            f"{error}\n\n"
            "Состояние сброшено. Если хочешь изменить "
            "шаблон, снова введи /vid."
        )

        return

    # -----------------------------------------------------
    # Сохраняем сразу
    # -----------------------------------------------------

    save_template(
        template_type,
        template_text,
        template_entities
    )

    await state.clear()

    # -----------------------------------------------------
    # Предпросмотр
    # -----------------------------------------------------

    await message.answer(
        "👀 Предпросмотр нового оформления:"
    )

    try:

        await message.answer(
            preview_text,
            entities=preview_entities
        )

    except TelegramBadRequest as error:

        print(
            "Ошибка предпросмотра шаблона:",
            repr(error)
        )

        await message.answer(
            "⚠️ Шаблон сохранён, "
            "но Telegram не смог показать предпросмотр.\n\n"
            "Проверь Premium Emoji в самом шаблоне."
        )

        return

    await message.answer(
        "✅ Шаблон сохранён и уже используется "
        "для новых публикаций."
    )


# =========================================================
# АНОН ТЕЙК
# =========================================================

@dp.message(F.text == "Анонимный тейк")
async def anonymous_take_start(
    message: Message,
    state: FSMContext
):

    if is_user_banned(message.from_user.id):
        await message.answer("🚫 Вы заблокированы и не можете пользоваться ботом.")
        return

    await state.set_state(
        TakeState.waiting_for_anonymous_take
    )

    await message.answer(
        "Отправь свой тейк. Это полностью анонимно."
    )


# =========================================================
# НЕАНОН ТЕЙК
# =========================================================

@dp.message(F.text == "Неанонимный тейк")
async def non_anonymous_take_start(
    message: Message,
    state: FSMContext
):

    if is_user_banned(message.from_user.id):
        await message.answer("🚫 Вы заблокированы и не можете пользоваться ботом.")
        return

    await state.set_state(
        TakeState.waiting_for_non_anonymous_take
    )

    await message.answer(
        "Отправь свой тейк."
    )


# =========================================================
# ВОПРОС
# =========================================================

@dp.message(F.text == "Задать вопрос администрации")
async def question_start(
    message: Message,
    state: FSMContext
):

    if is_user_banned(message.from_user.id):
        await message.answer("🚫 Вы заблокированы и не можете пользоваться ботом.")
        return

    await state.clear()

    await message.answer(
        "Выберите вариант:",
        reply_markup=question_menu()
    )


# =========================================================
# АНОНИМНЫЙ ВОПРОС
# =========================================================

@dp.callback_query(
    F.data == "question:anonymous"
)
async def anonymous_question_start(
    callback: CallbackQuery,
    state: FSMContext
):

    if is_user_banned(callback.from_user.id):
        await safe_callback_answer(
            callback,
            "🚫 Вы заблокированы.",
            show_alert=True
        )
        return

    await safe_callback_answer(
        callback,
        "Анонимный вопрос"
    )

    await state.set_state(
        TakeState.waiting_for_anonymous_question
    )

    await callback.message.answer(
        "Отправьте свой вопрос.\n\n"
        "Он будет отправлен администрации анонимно."
    )


# =========================================================
# НЕАНОНИМНЫЙ ВОПРОС
# =========================================================

@dp.callback_query(
    F.data == "question:nonanonymous"
)
async def non_anonymous_question_start(
    callback: CallbackQuery,
    state: FSMContext
):

    if is_user_banned(callback.from_user.id):
        await safe_callback_answer(
            callback,
            "🚫 Вы заблокированы.",
            show_alert=True
        )
        return

    await safe_callback_answer(
        callback,
        "Неанонимный вопрос"
    )

    await state.set_state(
        TakeState.waiting_for_non_anonymous_question
    )

    await callback.message.answer(
        "Отправь свой вопрос."
    )


# =========================================================
# СТАТЬ АДМИНОМ
# =========================================================

@dp.message(F.text == "Стать админом!")
async def admin_application_start(
    message: Message,
    state: FSMContext
):

    if is_user_banned(message.from_user.id):
        await message.answer("🚫 Вы заблокированы и не можете пользоваться ботом.")
        return

    await state.set_state(
        TakeState.waiting_for_admin_application
    )

    await message.answer(
        "Чтобы стать админом в ненависти нужно заполнить следующую анкету:\n\n"
        "1. ваш псевдоним, юз, если есть то тгк в мкм.\n"
        "2. ваш возраст, часовой пояс\n"
        "3. ваш опыт работы в проектах\n"
        "4. должность на которую претендуете\n"
        "5. опишите в крации как вы реагируете на стрессовые ситуации, умеете ли решать конфликты? стрессоустойчивы?\n"
        "6. ваше свободное время, сколько времени вы готовы уделять проекту?\n\n"
        "так же хочу напомнить, что вы подаете заявку на админство в канале где выражается НЕНАВИСТЬ и ее тут много. вы не избежите хейта и оскорблений, будьте готовы к этому."
    )


# =========================================================
# СОХРАНЕНИЕ ОДИНОЧНОГО СООБЩЕНИЯ
# =========================================================

async def save_submission(
    message: Message,
    anonymous: bool,
    submission_type: str = "take"
):

    media_type = None
    file_id = None

    text = (
        message.text
        or message.caption
        or ""
    )

    source_entities = (
        message.entities
        if message.text is not None
        else message.caption_entities
    ) or []

    user_entities = json.dumps(
        serialize_entities(source_entities),
        ensure_ascii=False
    )

    if message.photo:

        media_type = "photo"
        file_id = message.photo[-1].file_id

    elif message.video:

        media_type = "video"
        file_id = message.video.file_id

    elif message.document:

        media_type = "document"
        file_id = message.document.file_id

    conn = db_connect()
    cursor = conn.cursor()

    cursor.execute(
        """
        INSERT INTO submissions (
            user_id,
            anonymous,
            text,
            media_type,
            file_id,
            submission_type,
            user_message_id,
            user_entities
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            message.from_user.id,
            1 if anonymous else 0,
            text,
            media_type,
            file_id,
            submission_type,
            message.message_id,
            user_entities
        )
    )

    submission_id = cursor.lastrowid

    conn.commit()
    conn.close()

    return submission_id


# =========================================================
# ЗАГОЛОВКИ
# =========================================================

def take_header(
    message: Message,
    anonymous: bool
):

    if anonymous:

        return (
            "НОВЫЙ ТЕЙК\n"
            "От: анонима"
        )

    author = get_author_text(
        message.from_user
    )

    return (
        "НОВЫЙ ТЕЙК\n"
        f"От: {author}"
    )


def question_header(
    message: Message,
    anonymous: bool
):

    if anonymous:

        return (
            "НОВЫЙ ВОПРОС\n"
            "От: анонима"
        )

    author = get_author_text(
        message.from_user
    )

    return (
        "НОВЫЙ ВОПРОС\n"
        f"От: {author}"
    )


# =========================================================
# ОТПРАВКА ОДИНОЧНОГО СООБЩЕНИЯ АДМИНАМ
# =========================================================

async def send_single_submission_to_admins(
    message: Message,
    submission_id: int,
    anonymous: bool,
    submission_type: str
):

    if submission_type == "question":

        header = question_header(
            message,
            anonymous
        )

    elif submission_type == "application":

        author = get_author_text(
            message.from_user
        )

        header = (
            "НОВАЯ АНКЕТА!\n"
            f"Анкета от: {author}"
        )

    else:

        header = take_header(
            message,
            anonymous
        )

    full_text, content_entities = build_admin_content(
        message,
        header
    )

    keyboard = None

    if submission_type == "take":

        keyboard = publish_keyboard(
            submission_id
        )

    sent_1 = None
    sent_2 = None

    if message.photo:

        file_id = message.photo[-1].file_id

        sent_1 = await bot.send_photo(
            ADMIN_CHAT_ID_1,
            photo=file_id,
            caption=full_text,
            caption_entities=content_entities,
            reply_markup=keyboard
        )

        sent_2 = await bot.send_photo(
            ADMIN_CHAT_ID_2,
            photo=file_id,
            caption=full_text,
            caption_entities=content_entities,
            reply_markup=keyboard
        )

    elif message.video:

        file_id = message.video.file_id

        sent_1 = await bot.send_video(
            ADMIN_CHAT_ID_1,
            video=file_id,
            caption=full_text,
            caption_entities=content_entities,
            reply_markup=keyboard
        )

        sent_2 = await bot.send_video(
            ADMIN_CHAT_ID_2,
            video=file_id,
            caption=full_text,
            caption_entities=content_entities,
            reply_markup=keyboard
        )

    elif message.document:

        file_id = message.document.file_id

        sent_1 = await bot.send_document(
            ADMIN_CHAT_ID_1,
            document=file_id,
            caption=full_text,
            caption_entities=content_entities,
            reply_markup=keyboard
        )

        sent_2 = await bot.send_document(
            ADMIN_CHAT_ID_2,
            document=file_id,
            caption=full_text,
            caption_entities=content_entities,
            reply_markup=keyboard
        )

    else:

        sent_1 = await bot.send_message(
            ADMIN_CHAT_ID_1,
            full_text,
            entities=content_entities,
            reply_markup=keyboard
        )

        sent_2 = await bot.send_message(
            ADMIN_CHAT_ID_2,
            full_text,
            entities=content_entities,
            reply_markup=keyboard
        )

    conn = db_connect()
    cursor = conn.cursor()

    cursor.execute(
        """
        UPDATE submissions
        SET
            admin_message_id_1 = ?,
            admin_message_id_2 = ?
        WHERE id = ?
        """,
        (
            sent_1.message_id,
            sent_2.message_id,
            submission_id
        )
    )

    conn.commit()
    conn.close()


# =========================================================
# ОБРАБОТКА ТЕЙКА
# =========================================================

async def process_single_take(
    message: Message,
    anonymous: bool,
    state: FSMContext
):

    await state.clear()

    submission_id = await save_submission(
        message,
        anonymous,
        "take"
    )

    await send_single_submission_to_admins(
        message,
        submission_id,
        anonymous,
        "take"
    )

    confirmation = await message.answer(
        "Сообщение доставлено!"
    )

    await asyncio.sleep(3)

    try:
        await confirmation.delete()
    except Exception:
        pass

    await message.answer(
        "Выберите действие:",
        reply_markup=main_menu()
    )


# =========================================================
# ОБРАБОТКА ВОПРОСА
# =========================================================

async def process_single_question(
    message: Message,
    anonymous: bool,
    state: FSMContext
):

    await state.clear()

    submission_id = await save_submission(
        message,
        anonymous,
        "question"
    )

    await send_single_submission_to_admins(
        message,
        submission_id,
        anonymous,
        "question"
    )

    confirmation = await message.answer(
        "Вопрос отправлен!"
    )

    await asyncio.sleep(3)

    try:
        await confirmation.delete()
    except Exception:
        pass

    await message.answer(
        "Выберите действие:",
        reply_markup=main_menu()
    )


# =========================================================
# ТЕЙКИ
# =========================================================

@dp.message(
    TakeState.waiting_for_anonymous_take
)
async def anonymous_take_received(
    message: Message,
    state: FSMContext
):

    if is_user_banned(message.from_user.id):
        await message.answer("🚫 Вы заблокированы и не можете пользоваться ботом.")
        await state.clear()
        return

    if message.media_group_id:

        group_id = message.media_group_id

        album_buffer[group_id].append(message)

        if group_id in album_tasks:

            album_tasks[group_id].cancel()

        album_tasks[group_id] = asyncio.create_task(
            finish_album(
                group_id,
                True,
                state,
                "take"
            )
        )

        return

    await process_single_take(
        message,
        True,
        state
    )


@dp.message(
    TakeState.waiting_for_non_anonymous_take
)
async def non_anonymous_take_received(
    message: Message,
    state: FSMContext
):

    if is_user_banned(message.from_user.id):
        await message.answer("🚫 Вы заблокированы и не можете пользоваться ботом.")
        await state.clear()
        return

    if message.media_group_id:

        group_id = message.media_group_id

        album_buffer[group_id].append(message)

        if group_id in album_tasks:

            album_tasks[group_id].cancel()

        album_tasks[group_id] = asyncio.create_task(
            finish_album(
                group_id,
                False,
                state,
                "take"
            )
        )

        return

    await process_single_take(
        message,
        False,
        state
    )


# =========================================================
# ВОПРОСЫ
# =========================================================

@dp.message(
    TakeState.waiting_for_anonymous_question
)
async def anonymous_question_received(
    message: Message,
    state: FSMContext
):

    if is_user_banned(message.from_user.id):
        await message.answer("🚫 Вы заблокированы и не можете пользоваться ботом.")
        await state.clear()
        return

    if message.media_group_id:

        group_id = message.media_group_id

        album_buffer[group_id].append(message)

        if group_id in album_tasks:

            album_tasks[group_id].cancel()

        album_tasks[group_id] = asyncio.create_task(
            finish_album(
                group_id,
                True,
                state,
                "question"
            )
        )

        return

    await process_single_question(
        message,
        True,
        state
    )


@dp.message(
    TakeState.waiting_for_non_anonymous_question
)
async def non_anonymous_question_received(
    message: Message,
    state: FSMContext
):

    if is_user_banned(message.from_user.id):
        await message.answer("🚫 Вы заблокированы и не можете пользоваться ботом.")
        await state.clear()
        return

    if message.media_group_id:

        group_id = message.media_group_id

        album_buffer[group_id].append(message)

        if group_id in album_tasks:

            album_tasks[group_id].cancel()

        album_tasks[group_id] = asyncio.create_task(
            finish_album(
                group_id,
                False,
                state,
                "question"
            )
        )

        return

    await process_single_question(
        message,
        False,
        state
    )


# =========================================================
# АНКЕТА
# =========================================================

@dp.message(
    TakeState.waiting_for_admin_application
)
async def admin_application_received(
    message: Message,
    state: FSMContext
):

    if is_user_banned(message.from_user.id):
        await message.answer("🚫 Вы заблокированы и не можете пользоваться ботом.")
        await state.clear()
        return

    if message.media_group_id:

        group_id = message.media_group_id

        album_buffer[group_id].append(message)

        if group_id in album_tasks:

            album_tasks[group_id].cancel()

        album_tasks[group_id] = asyncio.create_task(
            finish_album(
                group_id,
                False,
                state,
                "application"
            )
        )

        return

    await state.clear()

    submission_id = await save_submission(
        message,
        False,
        "application"
    )

    await send_single_submission_to_admins(
        message,
        submission_id,
        False,
        "application"
    )

    confirmation = await message.answer(
        "Анкета отправлена администрации."
    )

    await asyncio.sleep(3)

    try:
        await confirmation.delete()
    except Exception:
        pass

    await message.answer(
        "Выберите действие:",
        reply_markup=main_menu()
    )


# =========================================================
# СОХРАНЕНИЕ АЛЬБОМА
# =========================================================

async def save_album(
    messages,
    anonymous,
    submission_type
):

    first_message = messages[0]

    text = ""
    user_entities = "[]"

    for msg in messages:

        if msg.caption:

            text = msg.caption
            user_entities = serialize_entities(
                msg.caption_entities or []
            )
            break

    album_data = []

    for msg in messages:

        if msg.photo:

            album_data.append(
                {
                    "type": "photo",
                    "file_id": msg.photo[-1].file_id
                }
            )

        elif msg.video:

            album_data.append(
                {
                    "type": "video",
                    "file_id": msg.video.file_id
                }
            )

        elif msg.document:

            album_data.append(
                {
                    "type": "document",
                    "file_id": msg.document.file_id
                }
            )

    album_json = json.dumps(
        album_data,
        ensure_ascii=False
    )

    conn = db_connect()
    cursor = conn.cursor()

    cursor.execute(
        """
        INSERT INTO submissions (
            user_id,
            anonymous,
            text,
            media_type,
            file_id,
            submission_type,
            user_message_id,
            user_entities
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            first_message.from_user.id,
            1 if anonymous else 0,
            text,
            "album",
            album_json,
            submission_type,
            first_message.message_id,
            user_entities
        )
    )

    submission_id = cursor.lastrowid

    conn.commit()
    conn.close()

    return submission_id


# =========================================================
# ПОЛУЧЕНИЕ ОФОРМЛЕНИЯ ДЛЯ ПУБЛИКАЦИИ
# =========================================================

async def build_public_content(
    template_type,
    text,
    user_id,
    user_entities=None
):

    template = get_template(
        template_type
    )

    # Если шаблон ещё не задан
    if not template:

        return text, user_entities or []

    template_text, entities_json = template

    entities = deserialize_entities(
        entities_json
    )

    # -----------------------------------------------------
    # Получаем пользователя
    # -----------------------------------------------------

    try:

        user = await bot.get_chat(
            user_id
        )

        # Если username есть —
        # используем его.
        #
        # Если username нет —
        # используем ID.
        if user.username:

            username = f"@{user.username}"

        else:

            username = str(user_id)

        name = user.full_name or "Пользователь"

    except Exception as error:

        print(
            "Ошибка получения пользователя:",
            repr(error)
        )

        username = str(user_id)
        name = "Пользователь"

    # -----------------------------------------------------
    # Значения переменных
    # -----------------------------------------------------

    replacements = {
        "{text}": text or "",
        "{username}": username,
        "{name}": name
    }

    print("ШАБЛОН ENTITIES:", [
        {
            "type": e.type,
            "offset": e.offset,
            "length": e.length,
            "custom_emoji_id": e.custom_emoji_id
        }
        for e in entities
    ])

    final_text, final_entities = apply_template(
        template_text,
        entities,
        replacements,
        user_entities=user_entities
    )

    print("ПОСЛЕ APPLY ENTITIES:", [
        {
            "type": e.type,
            "offset": e.offset,
            "length": e.length,
            "custom_emoji_id": e.custom_emoji_id
        }
        for e in final_entities
    ])

    print("ФИНАЛЬНЫЙ ТЕКСТ:", repr(final_text))

    return final_text, final_entities
# =========================================================
# ОПРЕДЕЛЕНИЕ ТИПА ШАБЛОНА
# =========================================================

def get_template_type_for_submission(
    anonymous
):

    if anonymous:

        return "anonymous"

    return "nonanonymous"


# =========================================================
# ОТПРАВКА АЛЬБОМА АДМИНАМ
# =========================================================

async def send_album_to_admins(
    messages,
    submission_id,
    anonymous,
    submission_type
):

    first_message = messages[0]

    if submission_type == "question":

        header = question_header(
            first_message,
            anonymous
        )

    elif submission_type == "application":

        author = get_author_text(
            first_message.from_user
        )

        header = (
            "НОВАЯ АНКЕТА\n"
            f"Анкета от: {author}"
        )

    else:

        header = take_header(
            first_message,
            anonymous
        )

    caption_message = next(
        (msg for msg in messages if msg.caption),
        None
    )

    if caption_message:
        caption_text, caption_entities = build_admin_content(
            caption_message,
            header
        )
    else:
        caption_text, caption_entities = header, []

    media_group_1 = []
    media_group_2 = []

    for index, msg in enumerate(messages):

        current_caption = (
            caption_text
            if index == 0
            else None
        )

        if msg.photo:

            media1 = InputMediaPhoto(
                media=msg.photo[-1].file_id,
                caption=current_caption,
                caption_entities=caption_entities if index == 0 else None
            )

            media2 = InputMediaPhoto(
                media=msg.photo[-1].file_id,
                caption=current_caption,
                caption_entities=caption_entities if index == 0 else None
            )

        elif msg.video:

            media1 = InputMediaVideo(
                media=msg.video.file_id,
                caption=current_caption,
                caption_entities=caption_entities if index == 0 else None
            )

            media2 = InputMediaVideo(
                media=msg.video.file_id,
                caption=current_caption,
                caption_entities=caption_entities if index == 0 else None
            )

        elif msg.document:

            media1 = InputMediaDocument(
                media=msg.document.file_id,
                caption=current_caption,
                caption_entities=caption_entities if index == 0 else None
            )

            media2 = InputMediaDocument(
                media=msg.document.file_id,
                caption=current_caption,
                caption_entities=caption_entities if index == 0 else None
            )

        else:

            continue

        media_group_1.append(media1)
        media_group_2.append(media2)

    if not media_group_1:

        return

    sent_album_1 = await bot.send_media_group(
        ADMIN_CHAT_ID_1,
        media=media_group_1
    )

    sent_album_2 = await bot.send_media_group(
        ADMIN_CHAT_ID_2,
        media=media_group_2
    )

    media_ids_1 = [
        msg.message_id
        for msg in sent_album_1
    ]

    media_ids_2 = [
        msg.message_id
        for msg in sent_album_2
    ]

    button_id_1 = None
    button_id_2 = None

    if submission_type == "take":

        sent_button_1 = await bot.send_message(
            ADMIN_CHAT_ID_1,
            "Действие с тейком:",
            reply_markup=publish_keyboard(
                submission_id
            )
        )

        sent_button_2 = await bot.send_message(
            ADMIN_CHAT_ID_2,
            "Действие с тейком:",
            reply_markup=publish_keyboard(
                submission_id
            )
        )

        button_id_1 = sent_button_1.message_id
        button_id_2 = sent_button_2.message_id

    conn = db_connect()
    cursor = conn.cursor()

    cursor.execute(
        """
        UPDATE submissions
        SET
            admin_message_id_1 = ?,
            admin_message_id_2 = ?,
            admin_media_ids_1 = ?,
            admin_media_ids_2 = ?
        WHERE id = ?
        """,
        (
            button_id_1,
            button_id_2,
            json.dumps(media_ids_1),
            json.dumps(media_ids_2),
            submission_id
        )
    )

    conn.commit()
    conn.close()


# =========================================================
# ЗАВЕРШЕНИЕ АЛЬБОМА
# =========================================================

async def finish_album(
    media_group_id,
    anonymous,
    state,
    submission_type
):

    await asyncio.sleep(0.5)

    messages = album_buffer.pop(
        media_group_id,
        []
    )

    album_tasks.pop(
        media_group_id,
        None
    )

    if not messages:

        return

    messages.sort(
        key=lambda msg: msg.message_id
    )

    submission_id = await save_album(
        messages,
        anonymous,
        submission_type
    )

    await send_album_to_admins(
        messages,
        submission_id,
        anonymous,
        submission_type
    )

    await state.clear()

    first_message = messages[0]

    if submission_type == "question":

        confirmation_text = "Вопрос отправлен."

    elif submission_type == "application":

        confirmation_text = (
            "Анкета отправлена администрации."
        )

    else:

        confirmation_text = (
            "Сообщение доставлено."
        )

    confirmation = await first_message.answer(
        confirmation_text
    )

    await asyncio.sleep(3)

    try:
        await confirmation.delete()
    except Exception:
        pass

    await first_message.answer(
        "Выберите действие:",
        reply_markup=main_menu()
    )


# =========================================================
# ПОИСК SUBMISSION ПО ADMIN MESSAGE ID
# =========================================================

def find_submission_by_admin_message(
    message_id
):

    conn = db_connect()
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT
            id,
            user_id,
            submission_type,
            user_message_id
        FROM submissions
        WHERE admin_message_id_1 = ?
           OR admin_message_id_2 = ?
        LIMIT 1
        """,
        (
            message_id,
            message_id
        )
    )

    result = cursor.fetchone()

    if result:

        conn.close()

        return result

    cursor.execute(
        """
        SELECT
            id,
            user_id,
            submission_type,
            user_message_id,
            admin_media_ids_1,
            admin_media_ids_2
        FROM submissions
        WHERE admin_media_ids_1 IS NOT NULL
           OR admin_media_ids_2 IS NOT NULL
        """
    )

    rows = cursor.fetchall()

    for row in rows:

        submission_id = row[0]
        user_id = row[1]
        submission_type = row[2]
        user_message_id = row[3]

        media_ids_1 = row[4]
        media_ids_2 = row[5]

        try:

            ids_1 = (
                json.loads(media_ids_1)
                if media_ids_1
                else []
            )

            ids_2 = (
                json.loads(media_ids_2)
                if media_ids_2
                else []
            )

        except Exception:

            continue

        if (
            message_id in ids_1
            or message_id in ids_2
        ):

            conn.close()

            return (
                submission_id,
                user_id,
                submission_type,
                user_message_id
            )

    conn.close()

    return None


# =========================================================
# /BAN И /UNBAN
# =========================================================

async def get_user_from_admin_reply(message: Message):

    if message.chat.id not in ADMIN_CHATS:
        return None

    if not message.reply_to_message:
        await message.answer(
            "❌ Используй команду ответом на тейк, вопрос или анкету."
        )
        return None

    result = find_submission_by_admin_message(
        message.reply_to_message.message_id
    )

    if not result:
        await message.answer(
            "❌ Не удалось найти пользователя этого сообщения."
        )
        return None

    return result


@dp.message(Command("ban"))
async def ban_command(message: Message):

    if message.chat.id not in ADMIN_CHATS:
        return

    result = await get_user_from_admin_reply(message)

    if not result:
        return

    user_id = result[1]

    if is_user_banned(user_id):
        await message.answer(
            "⚠️ Пользователь уже заблокирован."
        )
        return

    ban_user(
        user_id,
        message.from_user.id
    )

    await message.answer(
        f"🚫 Пользователь {user_id} заблокирован.\n\n"
        "Он больше не сможет пользоваться ботом во всех разделах."
    )


@dp.message(Command("unban"))
async def unban_command(message: Message):

    if message.chat.id not in ADMIN_CHATS:
        return

    result = await get_user_from_admin_reply(message)

    if not result:
        return

    user_id = result[1]

    if not unban_user(user_id):
        await message.answer(
            "⚠️ Пользователь не был заблокирован."
        )
        return

    await message.answer(
        f"✅ Пользователь {user_id} разблокирован.\n\n"
        "Теперь он снова может пользоваться ботом."
    )


# =========================================================
# /LINK
# =========================================================

@dp.message(F.text == "/link")
async def link_handler(
    message: Message
):

    if message.chat.id not in ADMIN_CHATS:

        return

    if not message.reply_to_message:

        await message.answer(
            "❌ Используй /link ответом "
            "на тейк, вопрос или анкету."
        )

        return

    result = find_submission_by_admin_message(
        message.reply_to_message.message_id
    )

    if not result:

        await message.answer(
            "❌ Не удалось найти автора "
            "этого сообщения."
        )

        return

    submission_id = result[0]
    user_id = result[1]
    submission_type = result[2]

    try:

        user = await bot.get_chat(
            user_id
        )

        username = (
            f"@{user.username}"
            if user.username
            else "нет username"
        )

        name = user.full_name

    except Exception:

        username = "не удалось получить"
        name = "не удалось получить"

    profile_link = (
        f"tg://user?id={user_id}\n"
        f"tg://openmessage?user_id={user_id}"
    )

    if submission_type == "application":

        title = "Автор анкеты:"

    elif submission_type == "question":

        title = "Автор вопроса:"

    else:

        title = "Автор тейка:"

    await message.answer(
        f"{title}\n\n"
        f"ID: {user_id}\n"
        f"Username: {username}\n"
        f"Имя: {name}\n\n"
        f"Ссылки:\n{profile_link}"
    )


# =========================================================
# ОТВЕТ АДМИНА ПОЛЬЗОВАТЕЛЮ
# =========================================================

@dp.message(
    F.reply_to_message
)
async def admin_reply_handler(
    message: Message
):

    if message.chat.id not in ADMIN_CHATS:

        return

    if message.text == "/link":

        return

    replied = message.reply_to_message

    result = find_submission_by_admin_message(
        replied.message_id
    )

    if not result:

        return

    submission_id = result[0]
    user_id = result[1]
    submission_type = result[2]
    user_message_id = result[3]

    try:

        # -------------------------------------------------
        # Копируем сообщение администратора пользователю
        #
        # Это сохраняет форматирование, entities,
        # Premium Emoji, фото, видео и документы.
        # -------------------------------------------------

        reply_parameters = None

        if user_message_id:
            reply_parameters = ReplyParameters(
                message_id=user_message_id
            )

        await bot.copy_message(
            chat_id=user_id,
            from_chat_id=message.chat.id,
            message_id=message.message_id,
            reply_parameters=reply_parameters
        )

        await message.answer(
            "Ответ отправлен пользователю."
        )

    except Exception as error:

        print(
            "Ошибка отправки ответа:",
            repr(error)
        )

        await message.answer(
            "❌ Не удалось отправить ответ пользователю.\n\n"
            "Возможно, пользователь заблокировал бота."
        )


# =========================================================
# ПУБЛИКАЦИЯ
# =========================================================

@dp.callback_query(
    F.data.startswith("publish:")
)
async def publish_handler(
    callback: CallbackQuery
):

    if callback.message.chat.id not in ADMIN_CHATS:

        await safe_callback_answer(
            callback,
            "Нет доступа.",
            show_alert=True
        )

        return

    submission_id = int(
        callback.data.split(":")[1]
    )

    conn = db_connect()
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT
            user_id,
            anonymous,
            text,
            media_type,
            file_id,
            status,
            submission_type,
            user_entities
        FROM submissions
        WHERE id = ?
        """,
        (submission_id,)
    )

    result = cursor.fetchone()

    conn.close()

    if not result:

        await safe_callback_answer(
            callback,
            "Сообщение не найдено.",
            show_alert=True
        )

        return

    (
        user_id,
        anonymous,
        text,
        media_type,
        file_id,
        status,
        submission_type,
        user_entities_json
    ) = result

    if submission_type != "take":

        await safe_callback_answer(
            callback,
            "⚠️ Публиковать можно только тейки.",
            show_alert=True
        )

        return

    if status == "published":

        await safe_callback_answer(
            callback,
            "⚠️ Этот тейк уже опубликован.",
            show_alert=True
        )

        return

    if media_type == "album" and not file_id:

        await safe_callback_answer(
            callback,
            "⚠️ Этот альбом создан старой версией бота.\n\n"
            "Отправь его заново.",
            show_alert=True
        )

        return

    await safe_callback_answer(
        callback,
        "⏳ Публикуем..."
    )

    user_entities = deserialize_entities(
        user_entities_json
    )

    try:

        template_type = (
            get_template_type_for_submission(
                anonymous
            )
        )

        # =================================================
        # АЛЬБОМ
        # =================================================

        if media_type == "album":

            album_data = json.loads(
                file_id
            )

            public_text, public_entities = (
                await build_public_content(
                    template_type,
                    text or "",
                    user_id,
                    user_entities=user_entities
                )
            )

            media_group = []

            for index, item in enumerate(
                album_data
            ):

                item_type = item["type"]
                item_file_id = item["file_id"]

                current_caption = (
                    public_text
                    if index == 0
                    else None
                )

                current_entities = (
                    public_entities
                    if index == 0
                    else None
                )

                if item_type == "photo":

                    media = InputMediaPhoto(
                        media=item_file_id,
                        caption=current_caption,
                        caption_entities=current_entities
                    )

                elif item_type == "video":

                    media = InputMediaVideo(
                        media=item_file_id,
                        caption=current_caption,
                        caption_entities=current_entities
                    )

                elif item_type == "document":

                    media = InputMediaDocument(
                        media=item_file_id,
                        caption=current_caption,
                        caption_entities=current_entities
                    )

                else:

                    continue

                media_group.append(media)

            if not media_group:

                raise ValueError(
                    "В альбоме нет файлов."
                )

            await bot.send_media_group(
                CHANNEL_ID,
                media=media_group
            )

        # =================================================
        # ТЕКСТ
        # =================================================

        elif media_type is None:

            public_text, public_entities = (
                await build_public_content(
                    template_type,
                    text or "",
                    user_id,
                    user_entities=user_entities
                )
            )

            await bot.send_message(
                CHANNEL_ID,
                public_text,
                entities=public_entities
            )

        # =================================================
        # ФОТО
        # =================================================

        elif media_type == "photo":

            public_text, public_entities = (
                await build_public_content(
                    template_type,
                    text or "",
                    user_id,
                    user_entities=user_entities
                )
            )

            await bot.send_photo(
                CHANNEL_ID,
                photo=file_id,
                caption=public_text,
                caption_entities=public_entities
            )

        # =================================================
        # ВИДЕО
        # =================================================

        elif media_type == "video":

            public_text, public_entities = (
                await build_public_content(
                    template_type,
                    text or "",
                    user_id,
                    user_entities=user_entities
                )
            )

            await bot.send_video(
                CHANNEL_ID,
                video=file_id,
                caption=public_text,
                caption_entities=public_entities
            )

        # =================================================
        # ДОКУМЕНТ
        # =================================================

        elif media_type == "document":

            public_text, public_entities = (
                await build_public_content(
                    template_type,
                    text or "",
                    user_id,
                    user_entities=user_entities
                )
            )

            await bot.send_document(
                CHANNEL_ID,
                document=file_id,
                caption=public_text,
                caption_entities=public_entities
            )

        else:

            raise ValueError(
                "Неизвестный тип сообщения."
            )

        # =================================================
        # СТАТУС
        # =================================================

        conn = db_connect()
        cursor = conn.cursor()

        cursor.execute(
            """
            UPDATE submissions
            SET status = 'published'
            WHERE id = ?
            """,
            (submission_id,)
        )

        conn.commit()
        conn.close()

        # =================================================
        # УБИРАЕМ КНОПКУ
        # =================================================

        try:

            await callback.message.edit_reply_markup(
                reply_markup=None
            )

        except Exception:

            pass

    except Exception as error:

        print(
            "Ошибка публикации:",
            repr(error)
        )

        try:

            await callback.message.edit_text(
                "Не удалось опубликовать.\n\n"
                f"Ошибка: {error}"
            )

        except Exception:

            try:

                await callback.message.edit_reply_markup(
                    reply_markup=None
                )

            except Exception:

                pass


# =========================================================
# ПРАВИЛА
# =========================================================

@dp.message(F.text == "Правила")
async def rules_handler(
    message: Message
):

    await message.answer(
        "Правила написания тейков в ВМН: Нельзя отправлять гс/кружки, спам, порнографию, личные данные. Допустимы темы про религию, политику и т.д., но запрещено выражать поддержку нацизму, фашизму, педофилии и т.п. Тейк должен быть связан с МКМ. Постоянное нытье и сожаления о том, что тейк не опубликован, не принимаются. Админы могут ответить или пообщаться. Полные правила можно увидеть в чате по команде “правила”.\n\n"
        "1. неанон тейки в анон бота не принимаются, для этого есть неанон бот.\n"
        "2. гс/кружки не принимаются.\n"
        "3. тейк должен являться продолжением фразы 'в мкм ненавидят'. больше тейки с ссылками на соо где вы просто кому-то отвечаете не будут приниматься.\n"
        "3.1. сливы выкладываются в любой форме и при любой формулировке.\n"
        "4. в тейках можно упоминать темы про: религию, селфхарм, политику, нацизм и т.д, но любая поддержа войны, фашизма, нацизма, рассизма, геноцида, педофилии, инцеста так же запрещена как и в чате.\n"
        "5. отправлять порнографию/расчлененку в чат/бота запрещено.\n"
        "6. распространение чужих личных данных запрещено.\n"
        "7. спам запрещен. за спам считается 3 одинаковых сообщения подряд.\n"
        "8. если вы пишите про малоизвестного/нового человека в мкм то вставьте юз или ссылку на канал. админы не могут знать всех в мкм и не могут проверить связан ли ваш тейк с мкм.\n"
        "9. ваше нытье, что люди все злые, что ненависть беспречинна так же больше выкладываться не будет. для этого пишите в другие проекты.\n"
        "9.1. если ваш тейк не выкладывают больше 12 часов то продублируйте его. опять же, хныкаться, что ваш тейк не пропускают не стоит. мы либо проигнорируем это, либо забаним. давайте уважать время и силы админов.\n"
        "10. все админы имеют доступ к тому, чтобы вам ответить и с вами поговорить. не удивляйтесь:(\n\n"
        "правила чата находятся в самом чате. просто напишите команду 'правила' и прочитайте их. проявляйте уважение к администрации. всем удачи!"
    )


# =========================================================
# ЗАПУСК
# =========================================================

async def main():

    print("Бот запущен!")

    await dp.start_polling(bot)


if __name__ == "__main__":

    asyncio.run(main())
