# -*- coding: utf-8 -*-
"""
🤖 ReferalBot — t.me/giftsme_bot kloni
========================================
Yulduzlar (Stars) · Do'kon (Shop) · Jekpot (Jackpot) tizimi

Texnologiyalar:
    - Aiogram 3.x (Telegram Bot API)
    - aiosqlite (SQLite)
    - python-dotenv (sozlamalar)

Ishga tushirish:
    .env faylida BOT_TOKEN va ADMIN_IDS ko'rsating, keyin:
    pip install -r requirements.txt
    python bot.py

Render uchun: WEBHOOK_URL o'rnatilsa — webhook rejimi, aks holda polling.
"""

import asyncio
import logging
import os
import random
from datetime import datetime
from urllib.parse import quote

import aiosqlite
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatMemberStatus, ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

load_dotenv()

# ============================================================
#  GLOBAL SOZLAMALAR (env fayldan o'qiladi)
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x.strip()]

# DB fayli qayerdan ishga tushirilishidan qat'i nazar, bot.py yonida bo'ladi
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.getenv("DB_PATH", os.path.join(BASE_DIR, "bot.db"))

# Render / webhook sozlamalari
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").rstrip("/")
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "/webhook")
WEBAPP_HOST = os.getenv("WEBAPP_HOST", "0.0.0.0")
WEBAPP_PORT = int(os.getenv("PORT", "8000"))

# Do'kon toifalari
CATEGORIES = {
    "gift": "🎁 Gift",
    "star": "⭐ Yulduz",
    "premium": "💎 Premium",
}

# Telegram_id -> kutilayotgan referrer (majburiy kanalga a'zolikdan keyin berish uchun)
pending_ref = {}

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(name)s - %(message)s")
logger = logging.getLogger(__name__)

router = Router()
dp = Dispatcher()


# ============================================================
#  FSM (STATE) HOLATLARI
# ============================================================

class SettingsStates(StatesGroup):
    """Admin sozlamalarni matn orqali o'zgartirishi uchun."""
    ref_reward = State()      # bitta referal uchun yulduz
    min_referals = State()    # xarid uchun minimal referallar
    jackpot_cost = State()    # jekpot bilet narxi
    jackpot_interval = State() # avtomatik o'yin oralig'i (soat)
    min_withdraw = State()    # yulduz yechish uchun minimal
    pay_card = State()        # to'lov karta raqami
    reviews_channel = State() # otziv kanali


class AddItemStates(StatesGroup):
    """Do'konga yangi mahsulot qo'shish (faqat nom va narx)."""
    category = State()
    name = State()
    price = State()
    deliver_stars = State()


class BroadcastStates(StatesGroup):
    """Rassilka yuborish."""
    content = State()


class AddChannelStates(StatesGroup):
    """Majburiy kanal qo'shish."""
    channel_id = State()
    invite_link = State()


class AddContactStates(StatesGroup):
    """Aloqa kontaktini qo'shish."""
    label = State()
    username = State()


class UzsPaymentStates(StatesGroup):
    """UZS to'lov uchun chek (screenshot) kutish."""
    proof = State()


# ============================================================
#  MA'LUMOTLAR BAZASI (aiosqlite)
# ============================================================

async def db_init() -> None:
    """Jadvallarni yaratadi va default qiymatlarni kiritadi."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER UNIQUE NOT NULL,
                balance_stars INTEGER NOT NULL DEFAULT 0,
                referals_count INTEGER NOT NULL DEFAULT 0,
                referrer_id INTEGER,
                last_free_ticket TEXT DEFAULT '',
                joined_at TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                ref_reward_stars INTEGER NOT NULL DEFAULT 5,
                min_referals_required INTEGER NOT NULL DEFAULT 3,
                jackpot_ticket_cost INTEGER NOT NULL DEFAULT 10,
                jackpot_fund INTEGER NOT NULL DEFAULT 0,
                jackpot_interval_hours INTEGER NOT NULL DEFAULT 24,
                jackpot_next_draw INTEGER NOT NULL DEFAULT 0,
                min_withdraw_stars INTEGER NOT NULL DEFAULT 100,
                pay_card TEXT NOT NULL DEFAULT '9860180104681937',
                reviews_channel TEXT DEFAULT ''
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS shop_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category TEXT NOT NULL,
                name TEXT NOT NULL,
                price_stars INTEGER NOT NULL DEFAULT 0,
                price_uzs INTEGER NOT NULL DEFAULT 0,
                description TEXT DEFAULT '',
                image_file_id TEXT DEFAULT NULL,
                deliver_stars INTEGER NOT NULL DEFAULT 0
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS jackpot_participants (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER UNIQUE NOT NULL,
                tickets_count INTEGER NOT NULL DEFAULT 1
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS jackpot_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                winner_id INTEGER NOT NULL,
                winner_name TEXT DEFAULT '',
                prize INTEGER NOT NULL,
                drawn_at TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS mandatory_channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id TEXT NOT NULL,
                invite_link TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS contacts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                label TEXT NOT NULL,
                username TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                user_name TEXT DEFAULT '',
                username TEXT DEFAULT '',
                item_id INTEGER,
                item_name TEXT DEFAULT '',
                category TEXT DEFAULT '',
                amount_uzs INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'pending',
                proof_file_id TEXT DEFAULT '',
                created_at TEXT NOT NULL
            )
        """)

        # Eski DB bo'lsa, yangi ustunlarni qo'shamiz (migratsiya)
        for alter_sql in (
            "ALTER TABLE users ADD COLUMN last_free_ticket TEXT DEFAULT ''",
            "ALTER TABLE settings ADD COLUMN jackpot_interval_hours INTEGER NOT NULL DEFAULT 24",
            "ALTER TABLE settings ADD COLUMN jackpot_next_draw INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE settings ADD COLUMN min_withdraw_stars INTEGER NOT NULL DEFAULT 100",
            "ALTER TABLE shop_items ADD COLUMN price_uzs INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE settings ADD COLUMN pay_card TEXT NOT NULL DEFAULT '9860180104681937'",
            "ALTER TABLE shop_items ADD COLUMN deliver_stars INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE settings ADD COLUMN reviews_channel TEXT DEFAULT ''",
        ):
            try:
                await db.execute(alter_sql)
            except aiosqlite.OperationalError:
                pass  # ustun allaqachon mavjud

        # Default sozlamalar (faqat birinchi marta)
        await db.execute("""
            INSERT OR IGNORE INTO settings (id, ref_reward_stars, min_referals_required,
                                            jackpot_ticket_cost, jackpot_fund, jackpot_interval_hours,
                                            jackpot_next_draw, min_withdraw_stars, pay_card)
            VALUES (1, 5, 3, 10, 0, 24, 0, 100, '9860180104681937')
        """)

        # Namuna mahsulotlar (faqat birinchi marta)
        await db.execute("""
            INSERT OR IGNORE INTO shop_items (category, name, price_stars, price_uzs, description)
            SELECT 'gift', '🎁 Oltin Gift', 100, 20000, 'Eng zo''r sovg''a!'
            WHERE NOT EXISTS (SELECT 1 FROM shop_items)
        """)
        await db.execute("""
            INSERT OR IGNORE INTO shop_items (category, name, price_stars, price_uzs, description)
            SELECT 'star', '⭐ 1000 Yulduz', 0, 15000, 'Hisobingizga 1000 yulduz'
            WHERE NOT EXISTS (SELECT 1 FROM shop_items WHERE category = 'star')
        """)
        await db.execute("""
            INSERT OR IGNORE INTO shop_items (category, name, price_stars, price_uzs, description)
            SELECT 'premium', '💎 Premium 1 oy', 200, 30000, 'Telegram Premium 1 oy'
            WHERE NOT EXISTS (SELECT 1 FROM shop_items WHERE category = 'premium')
        """)

        # Default kontaktlar (faqat birinchi marta)
        await db.execute("""
            INSERT OR IGNORE INTO contacts (label, username)
            SELECT '👨‍💻 Dasturchi', 'abdurahmondasturchi'
            WHERE NOT EXISTS (SELECT 1 FROM contacts)
        """)
        await db.execute("""
            INSERT OR IGNORE INTO contacts (label, username)
            SELECT '👑 Bot egasi', 'Kottabolladan'
            WHERE NOT EXISTS (SELECT 1 FROM contacts WHERE username = 'Kottabolladan')
        """)

        await db.commit()


# ---------- Sozlamalar ----------

async def get_settings() -> dict:
    """Sozlamalarni dict ko'rinishida qaytaradi."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT ref_reward_stars, min_referals_required, jackpot_ticket_cost, jackpot_fund, "
            "jackpot_interval_hours, jackpot_next_draw, min_withdraw_stars, pay_card, reviews_channel FROM settings WHERE id = 1"
        )
        row = await cur.fetchone()
        return dict(row) if row else None


async def update_settings(**kwargs) -> None:
    """Berilgan kalitlarni settings jadvalida yangilaydi."""
    keys = ", ".join(f"{k} = ?" for k in kwargs)
    vals = list(kwargs.values())
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(f"UPDATE settings SET {keys} WHERE id = 1", vals)
        await db.commit()


async def increase_fund(amount: int) -> None:
    """Jekpot fondini oshiradi."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE settings SET jackpot_fund = jackpot_fund + ? WHERE id = 1", (amount,))
        await db.commit()


async def reset_fund() -> None:
    """Jekpot fondini nolga qaytaradi."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE settings SET jackpot_fund = 0 WHERE id = 1")
        await db.commit()


# ---------- Foydalanuvchilar ----------

async def get_user(telegram_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def add_user(telegram_id: int, referrer_id: int | None = None) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO users (telegram_id, referrer_id, joined_at) VALUES (?, ?, ?)",
            (telegram_id, referrer_id, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        )
        await db.commit()


async def add_stars(telegram_id: int, amount: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET balance_stars = balance_stars + ? WHERE telegram_id = ?", (amount, telegram_id))
        await db.commit()


async def deduct_stars(telegram_id: int, amount: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET balance_stars = balance_stars - ? WHERE telegram_id = ?", (amount, telegram_id))
        await db.commit()


async def increment_referals(telegram_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET referals_count = referals_count + 1 WHERE telegram_id = ?", (telegram_id,))
        await db.commit()


async def get_all_users() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM users")
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


# ---------- Do'kon ----------

async def get_shop_items(category: str) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM shop_items WHERE category = ? ORDER BY price_stars", (category,))
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def get_all_shop_items() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM shop_items ORDER BY category, id")
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def get_shop_item(item_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM shop_items WHERE id = ?", (item_id,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def add_shop_item(category: str, name: str, price_stars: int, price_uzs: int, description: str = "", image_file_id: str | None = None, deliver_stars: int = 0) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO shop_items (category, name, price_stars, price_uzs, description, image_file_id, deliver_stars) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (category, name, price_stars, price_uzs, description, image_file_id, deliver_stars),
        )
        await db.commit()


async def delete_shop_item(item_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM shop_items WHERE id = ?", (item_id,))
        await db.commit()


# ---------- Jekpot ----------

async def get_participants() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM jackpot_participants")
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def get_user_tickets(telegram_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT tickets_count FROM jackpot_participants WHERE telegram_id = ?", (telegram_id,))
        row = await cur.fetchone()
        return row[0] if row else 0


async def add_tickets(telegram_id: int, count: int) -> None:
    """Biletlar sonini oshiradi (foydalanuvchi yo'q bo'lsa, yaratadi)."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO jackpot_participants (telegram_id, tickets_count)
            VALUES (?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET tickets_count = tickets_count + excluded.tickets_count
        """, (telegram_id, count))
        await db.commit()


async def clear_participants() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM jackpot_participants")
        await db.commit()


async def set_last_free_ticket(telegram_id: int, date_str: str) -> None:
    """Foydalanuvchining kunlik bepul bilet olgan sanasini saqlaydi."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET last_free_ticket = ? WHERE telegram_id = ?", (date_str, telegram_id))
        await db.commit()


async def add_jackpot_history(winner_id: int, winner_name: str, prize: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO jackpot_history (winner_id, winner_name, prize, drawn_at) VALUES (?, ?, ?, ?)",
            (winner_id, winner_name, prize, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        )
        await db.commit()


async def get_jackpot_history(limit: int = 3) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM jackpot_history ORDER BY id DESC LIMIT ?", (limit,))
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


def format_countdown(seconds: int) -> str:
    """Qolgan vaqtni HH:MM:SS ko'rinishida qaytaradi."""
    if seconds <= 0:
        return "00:00:00"
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


# ---------- Majburiy kanallar ----------

async def get_channels() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM mandatory_channels")
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def add_channel(channel_id: str, invite_link: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO mandatory_channels (channel_id, invite_link) VALUES (?, ?)",
            (channel_id, invite_link),
        )
        await db.commit()


async def delete_channel(channel_id_row: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM mandatory_channels WHERE id = ?", (channel_id_row,))
        await db.commit()


# ---------- Aloqa kontaktlari ----------

async def get_contacts() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM contacts ORDER BY id")
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def add_contact(label: str, username: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO contacts (label, username) VALUES (?, ?)",
            (label, username),
        )
        await db.commit()


async def delete_contact(row_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM contacts WHERE id = ?", (row_id,))
        await db.commit()


# ---------- Buyurtmalar (UZS to'lov) ----------

async def add_order(telegram_id: int, user_name: str, username: str, item_id: int,
                    item_name: str, category: str, amount_uzs: int, proof_file_id: str) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO orders (telegram_id, user_name, username, item_id, item_name, category, amount_uzs, proof_file_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (telegram_id, user_name, username, item_id, item_name, category, amount_uzs,
             proof_file_id, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        )
        await db.commit()
        return cur.lastrowid


async def get_order(order_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM orders WHERE id = ?", (order_id,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def update_order_status(order_id: int, status: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE orders SET status = ? WHERE id = ?", (status, order_id))
        await db.commit()


# ============================================================
#  YORDAMCHI FUNKSIYALAR
# ============================================================

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


async def check_subscriptions(bot: Bot, telegram_id: int, channels: list[dict]) -> list[dict]:
    """Foydalanuvchi majburiy kanallarga a'zo ekanligini tekshiradi.
    A'zo bo'lmagan kanallar ro'yxatini qaytaradi."""
    not_subscribed = []
    for ch in channels:
        try:
            member = await bot.get_chat_member(ch["channel_id"], telegram_id)
            if member.status not in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR):
                not_subscribed.append(ch)
        except (TelegramBadRequest, TelegramForbiddenError) as e:
            logger.warning("Kanalni tekshirib bo'lmadi %s: %s", ch["channel_id"], e)
            not_subscribed.append(ch)
    return not_subscribed


async def register_user_with_referral(telegram_id: int, referrer_id: int | None, friend_name: str = "") -> dict:
    """Yangi foydalanuvchini ro'yxatga oladi va referal mukofotini to'laydi."""
    user = await get_user(telegram_id)
    if user:
        return user, False

    # Referrer mavjudligini tekshiramiz (o'z-o'ziga referal bo'lmasin)
    referrer = None
    if referrer_id and referrer_id != telegram_id:
        referrer = await get_user(referrer_id)

    await add_user(telegram_id, referrer["telegram_id"] if referrer else None)
    user = await get_user(telegram_id)

    # Referal mukofoti: referrerning hisobiga yulduz + referals_count
    if referrer:
        settings = await get_settings()
        reward = settings["ref_reward_stars"]
        await add_stars(referrer["telegram_id"], reward)
        await increment_referals(referrer["telegram_id"])
        try:
            await bot_notify_referrer(referrer["telegram_id"], friend_name or str(telegram_id), reward)
        except Exception as e:
            logger.warning("Referrer ogohlantirish xatosi: %s", e)

    return user, True


# Bot instansiyasini saqlash uchun (referrer xabarnomasida kerak)
_bot: Bot | None = None


async def bot_notify_referrer(referrer_id: int, friend_name: str, reward: int) -> None:
    """Referal qabul qilinganda referrerga xabar (ism-familiya bilan)."""
    if _bot is None:
        return
    try:
        await _bot.send_message(
            referrer_id,
            f"🎉 <b>Tabriklaymiz!</b>\n"
            f"Yangicha taklif qildingiz: <b>{friend_name}</b>\n"
            f"Bonus: <b>+{reward} ⭐</b>",
        )
    except TelegramForbiddenError:
        pass


async def bot_notify_pending_referral(referrer_id: int, friend_name: str) -> None:
    """Do'st hali kanallarga a'zo bo'lmaganida referrerga xabar."""
    if _bot is None:
        return
    try:
        await _bot.send_message(
            referrer_id,
            f"🔔 <b>Yangi referal!</b>\n\n"
            f"Do'stingiz <b>{friend_name}</b> havolangiz orqali kirdi.\n"
            f"Agar u majburiy kanallarga a'zo bo'lsa, referal qabul qilinadi va bonus olasiz! 🎁",
        )
    except TelegramForbiddenError:
        pass


def main_menu_keyboard(user_id: int) -> ReplyKeyboardMarkup:
    """Asosiy reply klaviaturasi."""
    kb = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="👤 Profil"), KeyboardButton(text="🛍️ Do'kon")],
            [KeyboardButton(text="🎰 Jekpot"), KeyboardButton(text="📞 Aloqa")],
            [KeyboardButton(text="⭐ Otziv"), KeyboardButton(text="💸 Yulduz yechish")],
            [KeyboardButton(text="🔗 Referal")],
        ],
        resize_keyboard=True,
    )
    if is_admin(user_id):
        kb.keyboard.append([KeyboardButton(text="👑 Admin panel")])
    return kb


def channels_keyboard(channels: list[dict]) -> InlineKeyboardMarkup:
    """Majburiy kanallar ro'yxati + tekshirish tugmasi."""
    kb = InlineKeyboardBuilder()
    for i, ch in enumerate(channels, start=1):
        kb.button(text=f"📢 {i}-kanal", url=ch["invite_link"])
    kb.button(text="✅ A'zo bo'ldim", callback_data="check_sub")
    return kb.as_markup()


def inline_btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def product_keyboard(item: dict) -> InlineKeyboardMarkup:
    """Mahsulot uchun to'lov tugmalari (toifaga qarab ⭐ va/yo UZS)."""
    kb = InlineKeyboardBuilder()
    if item["category"] == "star":
        # Yulduzlar faqat UZS bilan sotib olinadi
        if item["price_uzs"] > 0:
            kb.button(text=f"💳 UZSda sotib olish ({item['price_uzs']:,} so'm)", callback_data=f"buy_uzs:{item['id']}")
    else:
        # Gift va Premium: ham yulduz, ham UZS ishlaydi
        if item["price_stars"] > 0:
            kb.button(text=f"⭐ Yulduzda ({item['price_stars']} ⭐)", callback_data=f"buy:{item['id']}")
        if item["price_uzs"] > 0:
            kb.button(text=f"💳 UZSda ({item['price_uzs']:,} so'm)", callback_data=f"buy_uzs:{item['id']}")
    kb.button(text="🔙 Ortga", callback_data="shop")
    return kb.as_markup()


def shop_categories_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for key, label in CATEGORIES.items():
        kb.button(text=label, callback_data=f"shop:{key}")
    kb.button(text="🔙 Bosh menyu", callback_data="main_menu")
    kb.adjust(1)
    return kb.as_markup()


def shop_items_keyboard(category: str) -> InlineKeyboardMarkup:
    """Toifa bo'yicha mahsulotlar tugmalarini qurish (asinxron emas,
    to'liq ro'yxat shop_category_callback ichida quriladi)."""
    return InlineKeyboardMarkup(inline_keyboard=[])


# ============================================================
#  /start VA MAJBURIY A'ZOLIK
# ============================================================

@router.message(CommandStart())
async def cmd_start(message: Message, bot: Bot, state: FSMContext) -> None:
    global _bot
    _bot = bot
    await state.clear()

    # Referal payload: /start <referrer_id>
    referrer_id = None
    payload = message.text.split()
    if len(payload) > 1 and payload[1].isdigit():
        referrer_id = int(payload[1])

    channels = await get_channels()
    user = await get_user(message.from_user.id)

    # Majburiy kanallarga a'zolik tekshiruvi
    if channels:
        not_sub = await check_subscriptions(bot, message.from_user.id, channels)
        if not_sub:
            # Referrer ehtiyojini saqlaymiz — tekshirishdan keyin berish uchun
            if referrer_id and referrer_id != message.from_user.id:
                pending_ref[message.from_user.id] = referrer_id
                # Referrerga xabar: do'st kanallarga a'zo bo'lsagina referal qabul qilinadi
                referrer = await get_user(referrer_id)
                if referrer:
                    try:
                        await bot_notify_pending_referral(referrer_id, message.from_user.full_name)
                    except Exception as e:
                        logger.warning("Referrer xabarnoma xatosi: %s", e)
            await message.answer(
                "❌ <b>Botdan foydalanish uchun quyidagi kanallarga a'zo bo'ling:</b>",
                reply_markup=channels_keyboard(not_sub),
            )
            return

    # Foydalanuvchi ro'yxatga olinadi + referal mukofoti to'lanadi
    await register_user_with_referral(message.from_user.id, referrer_id, message.from_user.full_name)
    pending_ref.pop(message.from_user.id, None)

    await message.answer(
        f"👋 <b>Xush kelibsiz!</b>\n\n"
        f"Bu yerda yulduzlar (⭐) yig'ib, do'kondan sovg'alar olasiz va jekpotda qatnashasiz!\n"
        f"Taklif qilgan har bir do'stingiz uchun bonus oling. 🎁",
        reply_markup=main_menu_keyboard(message.from_user.id),
    )


@router.callback_query(F.data == "check_sub")
async def check_sub_handler(call: CallbackQuery, bot: Bot, state: FSMContext) -> None:
    global _bot
    _bot = bot

    channels = await get_channels()
    not_sub = await check_subscriptions(bot, call.from_user.id, channels)

    if not_sub:
        await call.answer("❌ Hali ham a'zo bo'lmagansiz!", show_alert=True)
        return

    # A'zo bo'ldi — foydalanuvchini ro'yxatga olamiz (kutilayotgan referrer bilan)
    ref = pending_ref.get(call.from_user.id)
    user = await get_user(call.from_user.id)
    if not user:
        await register_user_with_referral(call.from_user.id, ref, call.from_user.full_name)
    pending_ref.pop(call.from_user.id, None)

    try:
        await call.message.delete()
    except TelegramBadRequest:
        pass
    await call.message.answer(
        "✅ <b>Tabriklaymiz! Endi botdan foydalanishingiz mumkin.</b>",
        reply_markup=main_menu_keyboard(call.from_user.id),
    )
    await call.answer()


# ============================================================
#  ASOSIY MENYU TUGMALARI (Reply)
# ============================================================

@router.message(F.text == "👤 Profil")
async def profile_handler(message: Message, bot: Bot) -> None:
    global _bot
    _bot = bot
    user = await get_user(message.from_user.id)
    if not user:
        await message.answer("❌ Avval /start ni bosing!")
        return

    bot_username = (await bot.me()).username
    ref_link = f"https://t.me/{bot_username}?start={user['telegram_id']}"

    await message.answer(
        f"👤 <b>Profil</b>\n\n"
        f"🆔 ID: <code>{user['telegram_id']}</code>\n"
        f"⭐ Yulduzlar: <b>{user['balance_stars']}</b>\n"
        f"👥 Taklif qilganlar: <b>{user['referals_count']}</b>\n\n"
        f"🔗 <b>Shaxsiy havolangiz:</b>\n<code>{ref_link}</code>\n\n"
        f"Shu havolani do'stlaringizga yuboring, ular a'zo bo'lganda bonus olasiz!",
    )


@router.message(F.text == "🔗 Referal")
async def referral_handler(message: Message, bot: Bot) -> None:
    global _bot
    _bot = bot
    user = await get_user(message.from_user.id)
    if not user:
        await message.answer("❌ Avval /start ni bosing!")
        return

    bot_username = (await bot.me()).username
    ref_link = f"https://t.me/{bot_username}?start={user['telegram_id']}"
    share_text = "Men bu bot orqali yulduzlar yig'ib, sovg'alar olaman! Qo'shil! 🎁"
    share_url = f"https://t.me/share/url?url={quote(ref_link)}&text={quote(share_text)}"
    kb = InlineKeyboardBuilder()
    kb.button(text="📤 Do'stlarga ulashish", url=share_url)
    kb.button(text="🔗 Havolani nusxalash", url=ref_link)

    await message.answer(
        f"🔗 <b>Referal havolangiz</b>\n\n"
        f"{ref_link}\n\n"
        f"Shu havolani do'stlaringizga yuboring. Har bir yangi a'zo uchun bonus olasiz! ⭐",
        reply_markup=kb.as_markup(),
    )


@router.message(F.text == "💸 Yulduz yechish")
async def withdraw_handler(message: Message) -> None:
    user = await get_user(message.from_user.id)
    if not user:
        await message.answer("❌ Avval /start ni bosing!")
        return

    settings = await get_settings()
    min_withdraw = settings["min_withdraw_stars"]

    if user["balance_stars"] < min_withdraw:
        need = min_withdraw - user["balance_stars"]
        await message.answer(
            f"💸 <b>Yulduz yechish</b>\n\n"
            f"Balansingiz: <b>{user['balance_stars']} ⭐</b>\n\n"
            f"❌ Yechib olish uchun minimal <b>{min_withdraw} ⭐</b> bo'lishi kerak.\n"
            f"Sizga yana <b>{need} ⭐</b> kerak.\n"
            f"Yana odam taklif qiling yoki bot egasi bilan bog'laning: @Kottabolladan",
        )
        return

    # To'lov turini tanlash
    kb = InlineKeyboardBuilder()
    kb.button(text="🎁 Gift sifatida", callback_data="withdraw:gift")
    kb.button(text="⭐ Yulduz sifatida", callback_data="withdraw:stars")
    kb.adjust(1)

    await message.answer(
        f"💸 <b>Yulduz yechish</b>\n\n"
        f"Balansingiz: <b>{user['balance_stars']} ⭐</b>\n"
        f"Minimal: <b>{min_withdraw} ⭐</b>\n\n"
        f"👇 <b>To'lov turini tanlang:</b>",
        reply_markup=kb.as_markup(),
    )


async def process_stars_withdrawal(call: CallbackQuery, bot: Bot, amount: int) -> None:
    """Yulduzlar yechiladi: balansdan ayriladi, egaga o'tkazish uchun avto buyurtma ketadi."""
    user = await get_user(call.from_user.id)
    if not user:
        await call.answer("❌ Xatolik", show_alert=True)
        return

    if user["balance_stars"] < amount:
        await call.answer("❌ Balans yetarli emas!", show_alert=True)
        return

    await deduct_stars(call.from_user.id, amount)

    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(
                admin_id,
                f"💸 <b>YULDUZ YECHISH!</b>\n\n"
                f"👤 Foydalanuvchi: {call.from_user.first_name} (@{call.from_user.username or '—'})\n"
                f"🆔 ID: <code>{call.from_user.id}</code>\n"
                f"💰 Miqdor: <b>{amount} ⭐</b>\n"
                f"📅 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                f"⚠️ Bot egasi ushbu yulduzlarni foydalanuvchiga o'tkazishi kerak!",
            )
        except TelegramForbiddenError:
            pass

    try:
        await call.message.edit_text(
            f"✅ <b>Yulduzlaringiz yechildi!</b>\n\n"
            f"💰 Miqdor: <b>{amount} ⭐</b>\n\n"
            f"📤 Yulduzlar bot egasidan hisobingizga o'tkaziladi.\n"
            f"👑 Ega: <b>@Kottabolladan</b>",
        )
    except TelegramBadRequest:
        await call.message.answer(
            f"✅ <b>Yulduzlaringiz yechildi!</b>\n\n"
            f"💰 Miqdor: <b>{amount} ⭐</b>\n\n"
            f"📤 Yulduzlar bot egasidan hisobingizga o'tkaziladi.\n"
            f"👑 Ega: <b>@Kottabolladan</b>",
        )
    await call.answer("✅ Yechildi!", show_alert=False)


@router.callback_query(F.data == "withdraw:stars")
async def withdraw_stars_callback(call: CallbackQuery, bot: Bot) -> None:
    user = await get_user(call.from_user.id)
    if not user:
        await call.answer("❌ Avval /start ni bosing!", show_alert=True)
        return
    settings = await get_settings()
    if user["balance_stars"] < settings["min_withdraw_stars"]:
        await call.answer("❌ Minimal chegaraga yetmadingiz!", show_alert=True)
        return
    await process_stars_withdrawal(call, bot, user["balance_stars"])


@router.callback_query(F.data == "withdraw:gift")
async def withdraw_gift_callback(call: CallbackQuery) -> None:
    user = await get_user(call.from_user.id)
    if not user:
        await call.answer("❌ Avval /start ni bosing!", show_alert=True)
        return

    settings = await get_settings()
    if user["balance_stars"] < settings["min_withdraw_stars"]:
        await call.answer("❌ Minimal chegaraga yetmadingiz!", show_alert=True)
        return

    # Balansga yetadigan giftlar (narxi 0 bo'lmagan)
    gifts = [g for g in await get_shop_items("gift") if 0 < g["price_stars"] <= user["balance_stars"]]

    if not gifts:
        await call.answer(
            "Gift uchun yulduzlaringiz yetarli emas. Yulduz sifatida yechib oling!",
            show_alert=True,
        )
        return

    kb = InlineKeyboardBuilder()
    for g in gifts:
        kb.button(text=f"{g['name']} — {g['price_stars']} ⭐", callback_data=f"withdraw_gift:{g['id']}")
    kb.button(text="⭐ Yulduz sifatida yechish", callback_data="withdraw:stars")
    kb.button(text="🔙 Ortga", callback_data="withdraw:gift")
    kb.adjust(1)

    await call.message.edit_text(
        f"🎁 <b>Gift sifatida yechish</b>\n\n"
        f"Balansingizga yetadigan giftlar:\n"
        f"(Balans: {user['balance_stars']} ⭐)",
        reply_markup=kb.as_markup(),
    )
    await call.answer()


@router.callback_query(F.data.startswith("withdraw_gift:"))
async def withdraw_gift_confirm(call: CallbackQuery, bot: Bot) -> None:
    item_id = int(call.data.split(":")[1])
    user = await get_user(call.from_user.id)
    item = await get_shop_item(item_id)

    if not user or not item:
        await call.answer("❌ Xatolik yuz berdi", show_alert=True)
        return

    if user["balance_stars"] < item["price_stars"]:
        await call.answer("❌ Balans yetarli emas!", show_alert=True)
        return

    # Gift yechib olinadi — yulduz ayriladi
    await deduct_stars(call.from_user.id, item["price_stars"])

    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(
                admin_id,
                f"🎁 <b>GIFT YECHIB OLINDI!</b>\n\n"
                f"👤 Foydalanuvchi: {call.from_user.first_name} (@{call.from_user.username or '—'})\n"
                f"🆔 ID: <code>{call.from_user.id}</code>\n"
                f"🎁 Gift: <b>{item['name']}</b>\n"
                f"💰 Narxi: <b>{item['price_stars']} ⭐</b>\n"
                f"📅 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                f"⚠️ Giftni foydalanuvchiga o'tkazing (Telegram'da yuborish mumkin)!",
            )
        except TelegramForbiddenError:
            pass

    try:
        await call.message.edit_text(
            f"✅ <b>Gift yechib olindi!</b>\n\n"
            f"🎁 <b>{item['name']}</b>\n"
            f"💰 {item['price_stars']} ⭐ ayirildi.\n\n"
            f"Gift sizga Telegram'da yuboriladi. Egasi: @Kottabolladan",
        )
    except TelegramBadRequest:
        await call.message.answer(
            f"✅ <b>Gift yechib olindi!</b>\n\n"
            f"🎁 <b>{item['name']}</b>\n"
            f"💰 {item['price_stars']} ⭐ ayirildi.\n\n"
            f"Gift sizga Telegram'da yuboriladi. Egasi: @Kottabolladan",
        )
    await call.answer("✅ Gift oldingiz!", show_alert=False)


@router.message(F.text == "🛍️ Do'kon")
async def shop_handler(message: Message) -> None:
    await message.answer(
        "🛍️ <b>Do'kon</b>\n\nKategoriyani tanlang:",
        reply_markup=shop_categories_keyboard(),
    )


@router.message(F.text == "🎰 Jekpot")
async def jackpot_handler(message: Message) -> None:
    await show_jackpot(message.answer, message.from_user.id)


async def show_jackpot(answer_func, telegram_id: int) -> None:
    """Jekpot menyusini ko'rsatadi (har qanday answer funksiya bilan)."""
    settings = await get_settings()
    participants = await get_participants()
    user = await get_user(telegram_id)
    user_tickets = await get_user_tickets(telegram_id)
    total_tickets = sum(p["tickets_count"] for p in participants)
    history = await get_jackpot_history(3)

    kb = InlineKeyboardBuilder()
    today = datetime.now().strftime("%Y-%m-%d")
    if not user or user["last_free_ticket"] != today:
        kb.button(text="🎟️ Kunlik bepul bilet (0 ⭐)", callback_data="jackpot_free")
    for qty in (1, 3, 5):
        kb.button(text=f"🎟️ {qty} bilet", callback_data=f"jackpot_buy:{qty}")
    kb.button(text="🔙 Bosh menyu", callback_data="main_menu")
    kb.adjust(1)

    next_draw = settings["jackpot_next_draw"] or 0
    remaining = next_draw - int(datetime.now().timestamp())

    text = (
        f"🎰 <b>JEKPOT</b>\n\n"
        f"💰 Jekpot fondi: <b>{settings['jackpot_fund']} ⭐</b>\n"
        f"🎟️ Bilet narxi: <b>{settings['jackpot_ticket_cost']} ⭐</b>\n"
        f"👥 Ishtirokchilar: <b>{len(participants)}</b>\n"
        f"📊 Jami biletlar: <b>{total_tickets}</b>\n"
        f"⏱ Navbatdagi o'yin: <b>{format_countdown(remaining)}</b>\n\n"
        f"⭐ Sizning biletlaringiz: <b>{user_tickets}</b>\n\n"
        f"Qancha ko'p bilet olsangiz, g'olib bo'lish imkoniyati shuncha yuqori!"
    )
    if history:
        text += "\n\n🏆 <b>Oxirgi g'oliblar:</b>\n"
        for h in history:
            text += f"• {h['winner_name']} — <b>+{h['prize']} ⭐</b> ({h['drawn_at'][:16]})\n"

    await answer_func(text, reply_markup=kb.as_markup())


@router.message(F.text == "📞 Aloqa")
async def contacts_handler(message: Message) -> None:
    contacts = await get_contacts()

    kb = InlineKeyboardBuilder()
    for c in contacts:
        kb.button(text=f"📩 {c['label']}", url=f"https://t.me/{c['username'].lstrip('@')}")
    kb.adjust(1)

    text = (
        "📞 <b>Aloqa</b>\n\n"
        "Savollaringiz bo'lsa, quyidagi kontaktlardan biriga murojaat qiling:\n\n"
    )
    if contacts:
        for c in contacts:
            text += f"{c['label']} — <b>@{c['username'].lstrip('@')}</b>\n"
    else:
        text += "Kontaktlar hozircha yo'q.\n"

    await message.answer(text, reply_markup=kb.as_markup())


def channel_url(value: str) -> str:
    """Kanal username/havolasini t.me URL'iga aylantiradi."""
    value = value.strip().lstrip("@")
    if value.startswith("http://") or value.startswith("https://"):
        return value
    return f"https://t.me/{value}"


@router.message(F.text == "⭐ Otziv")
async def reviews_handler(message: Message) -> None:
    s = await get_settings()

    text = (
        "⭐ <b>Otziv</b>\n\n"
        "Bizning kanalda fikringizni qoldiring:\n"
        "mahsulot sifati, yetkazish tezligi va xizmat haqida.\n\n"
        "Sizning fikringiz biz uchun juda muhim! 💛"
    )
    kb = InlineKeyboardBuilder()
    if s["reviews_channel"]:
        kb.button(text="✍️ Otziv qoldirish", url=channel_url(s["reviews_channel"]))
    kb.adjust(1)

    await message.answer(text, reply_markup=kb.as_markup() if s["reviews_channel"] else None)


# ============================================================
#  DO'KON (SHOP) — INLINE
# ============================================================

@router.callback_query(F.data == "shop")
async def shop_callback(call: CallbackQuery) -> None:
    await call.message.edit_text(
        "🛍️ <b>Do'kon</b>\n\nKategoriyani tanlang:",
        reply_markup=shop_categories_keyboard(),
    )
    await call.answer()


@router.callback_query(F.data.startswith("shop:"))
async def shop_category_callback(call: CallbackQuery) -> None:
    category = call.data.split(":")[1]
    label = CATEGORIES.get(category, category)
    items = await get_shop_items(category)

    if not items:
        await call.answer("❌ Bu toifada hozircha mahsulot yo'q", show_alert=True)
        return

    kb = InlineKeyboardBuilder()
    for item in items:
        if item["category"] == "star":
            btn_text = f"{item['name']} — {item['price_uzs']:,} so'm"
        else:
            btn_text = f"{item['name']} — {item['price_stars']} ⭐ / {item['price_uzs']:,} so'm"
        kb.button(text=btn_text, callback_data=f"item:{item['id']}")
    kb.button(text="🔙 Ortga", callback_data="shop")
    kb.adjust(1)

    await call.message.edit_text(f"{label} <b>bo'limi:</b>", reply_markup=kb.as_markup())
    await call.answer()


@router.callback_query(F.data.startswith("item:"))
async def item_detail_callback(call: CallbackQuery, bot: Bot) -> None:
    item_id = int(call.data.split(":")[1])
    item = await get_shop_item(item_id)
    if not item:
        await call.answer("❌ Mahsulot topilmadi", show_alert=True)
        return

    label = CATEGORIES.get(item["category"], item["category"])
    description = item["description"] or ""
    if item["category"] == "star":
        price_line = f"💰 Narxi: <b>{item['price_uzs']:,} so'm</b>"
    else:
        price_line = f"💰 Narxi: <b>{item['price_stars']} ⭐</b> yoki <b>{item['price_uzs']:,} so'm</b>"
    text = (
        f"{label}\n\n"
        f"<b>{item['name']}</b>\n"
        f"{price_line}"
    )
    if description:
        text += f"\n\n{description}"
    kb = product_keyboard(item)

    if item["image_file_id"]:
        try:
            await call.message.delete()
        except TelegramBadRequest:
            pass
        await bot.send_photo(call.from_user.id, item["image_file_id"], caption=text, reply_markup=kb)
    else:
        await call.message.edit_text(text, reply_markup=kb)
    await call.answer()


@router.callback_query(F.data.startswith("buy:"))
async def buy_item_callback(call: CallbackQuery, bot: Bot) -> None:
    """Gift/Premiumni yulduz (⭐) bilan sotib olish."""
    item_id = int(call.data.split(":")[1])
    user = await get_user(call.from_user.id)
    item = await get_shop_item(item_id)

    if not user or not item:
        await call.answer("❌ Xatolik yuz berdi", show_alert=True)
        return

    if item["category"] == "star":
        await call.answer("⭐ Yulduzlar faqat UZS bilan sotib olinadi!", show_alert=True)
        return

    settings = await get_settings()
    label = CATEGORIES.get(item["category"], item["category"])

    # Minimal referallar cheklovi
    if user["referals_count"] < settings["min_referals_required"]:
        need = settings["min_referals_required"] - user["referals_count"]
        await call.answer(
            f"❌ Minimal {settings['min_referals_required']} ta odam taklif qilishingiz kerak! "
            f"Sizda {user['referals_count']} ta, yana {need} ta kerak.",
            show_alert=True,
        )
        return

    # Balans tekshiruvi — yetarli bo'lmasa taklif qilish yoki ega bilan bog'lanish
    if user["balance_stars"] < item["price_stars"]:
        need_stars = item["price_stars"] - user["balance_stars"]
        await call.answer(
            f"❌ Balansingiz yetarli emas!\n\n"
            f"Sizga yana <b>{need_stars} ⭐</b> kerak.\n"
            f"Yana odam taklif qiling yoki bot egasi bilan bog'laning: @Kottabolladan",
            show_alert=True,
        )
        return

    # Xarid — yulduz ayriladi
    await deduct_stars(call.from_user.id, item["price_stars"])

    # Adminga buyurtma yuboramiz
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(
                admin_id,
                f"🛒 <b>YANGI BUYURTMA!</b>\n\n"
                f"👤 Foydalanuvchi: {call.from_user.first_name} (@{call.from_user.username or '—'})\n"
                f"🆔 ID: <code>{call.from_user.id}</code>\n"
                f"{label} <b>{item['name']}</b>\n"
                f"💰 Narxi: <b>{item['price_stars']} ⭐</b> (yulduzda)\n"
                f"👥 Referallar: {user['referals_count']}\n"
                f"📅 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            )
        except TelegramForbiddenError:
            pass

    await call.message.delete()
    await call.message.answer(
        f"✅ <b>Xarid muvaffaqiyatli!</b>\n\n"
        f"{label} <b>{item['name']}</b> — {item['price_stars']} ⭐ ayirildi. "
        f"Buyurtma adminga yuborildi, tez orada siz bilan bog'lanamiz. 🎁",
    )
    await call.answer("✅ Sotib olindi!", show_alert=False)


def format_card(card: str) -> str:
    """Karta raqamini chiroyli ko'rsatadi. Emoji/yozuv bo'lsa, o'zidek qaytaradi."""
    digits = card.replace(" ", "")
    if digits.isdigit():
        return " ".join(digits[i:i + 4] for i in range(0, len(digits), 4))
    return card


@router.callback_query(F.data.startswith("buy_uzs:"))
async def buy_item_uzs_callback(call: CallbackQuery) -> None:
    """Har qanday mahsulotni UZS (so'm) bilan sotib olish — karta raqamiga to'lov."""
    item_id = int(call.data.split(":")[1])
    user = await get_user(call.from_user.id)
    item = await get_shop_item(item_id)

    if not user or not item:
        await call.answer("❌ Xatolik yuz berdi", show_alert=True)
        return

    if item["price_uzs"] <= 0:
        await call.answer("❌ Bu mahsulot uchun UZS narxi belgilanmagan!", show_alert=True)
        return

    settings = await get_settings()
    card_display = format_card(settings["pay_card"])
    label = CATEGORIES.get(item["category"], item["category"])

    kb = InlineKeyboardBuilder()
    kb.button(text="✅ To'lov qildim — chek yuborish", callback_data=f"uzs_paid:{item['id']}")
    kb.button(text="🔙 Ortga", callback_data=f"item:{item['id']}")
    kb.adjust(1)

    await call.message.edit_text(
        f"💳 <b>UZS to'lov</b>\n\n"
        f"{label} <b>{item['name']}</b>\n"
        f"💰 Summa: <b>{item['price_uzs']:,} so'm</b>\n\n"
        f"Pulni quyidagi karta raqamiga o'tkazing:\n"
        f"💳 <code>{card_display}</code>\n\n"
        f"To'lovni amalga oshirgach, pastdagi tugmani bosing va chek (screenshot) yuboring. "
        f"Admin tasdiqlagach mahsulot <b>avtomatik</b> yetkaziladi.",
        reply_markup=kb.as_markup(),
    )
    await call.answer()


@router.callback_query(F.data.startswith("uzs_paid:"))
async def uzs_paid_callback(call: CallbackQuery, state: FSMContext) -> None:
    """Foydalanuvchi to'lov qilganini bildirib, chek yuborishga o'tadi."""
    item_id = int(call.data.split(":")[1])
    item = await get_shop_item(item_id)
    if not item:
        await call.answer("❌ Mahsulot topilmadi", show_alert=True)
        return

    await state.set_state(UzsPaymentStates.proof)
    await state.update_data(item_id=item_id)

    settings = await get_settings()
    card_display = format_card(settings["pay_card"])

    await call.message.edit_text(
        f"📸 <b>Chek yuboring</b>\n\n"
        f"To'lovni <b>{item['price_uzs']:,} so'm</b> miqdorida "
        f"💳 <code>{card_display}</code> kartaga o'tkazganingizni tasdiqlovchi "
        f"<b>screenshot'ni (foto) yuboring</b>.\n\n"
        f"Chek tekshirilib, tasdiqlangach mahsulot avtomatik yetkaziladi.",
    )
    await call.answer()


@router.message(UzsPaymentStates.proof, F.photo)
async def uzs_proof_received(message: Message, state: FSMContext, bot: Bot) -> None:
    """Chek (screenshot) qabul qilinadi, adminlarga tasdiqlash uchun yuboriladi."""
    data = await state.get_data()
    item_id = data.get("item_id")
    item = await get_shop_item(item_id)
    user = await get_user(message.from_user.id)

    if not item or not user:
        await message.answer("❌ Xatolik yuz berdi, qaytadan urinib ko'ring.")
        await state.clear()
        return

    proof_file_id = message.photo[-1].file_id
    label = CATEGORIES.get(item["category"], item["category"])

    order_id = await add_order(
        telegram_id=message.from_user.id,
        user_name=message.from_user.first_name or "",
        username=message.from_user.username or "",
        item_id=item["id"],
        item_name=item["name"],
        category=item["category"],
        amount_uzs=item["price_uzs"],
        proof_file_id=proof_file_id,
    )

    caption = (
        f"🛒 <b>YANGI BUYURTMA #{order_id} (UZS)!</b>\n\n"
        f"👤 Foydalanuvchi: {message.from_user.first_name} (@{message.from_user.username or '—'})\n"
        f"🆔 ID: <code>{message.from_user.id}</code>\n"
        f"{label} <b>{item['name']}</b>\n"
        f"💰 Summa: <b>{item['price_uzs']:,} so'm</b>\n"
        f"📅 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        f"👇 Chekni tekshiring va qaror qabul qiling:"
    )
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Tasdiqlash", callback_data=f"order_approve:{order_id}")
    kb.button(text="❌ Rad etish", callback_data=f"order_reject:{order_id}")
    kb.adjust(1)

    for admin_id in ADMIN_IDS:
        try:
            await bot.send_photo(admin_id, proof_file_id, caption=caption, reply_markup=kb.as_markup())
        except TelegramForbiddenError:
            pass

    await state.clear()
    await message.answer(
        f"✅ <b>Chek qabul qilindi!</b>\n\n"
        f"#{order_id} buyurtma: {label} <b>{item['name']}</b> — {item['price_uzs']:,} so'm.\n"
        f"To'lov tasdiqlangach mahsulot <b>avtomatik yetkaziladi</b>.",
    )


@router.message(UzsPaymentStates.proof)
async def uzs_proof_invalid(message: Message) -> None:
    """Chek o'rniga matn yoki boshqa narsa yuborilsa."""
    await message.answer("❌ Iltimos, to'lov chekining <b>screenshot'ini (foto) yuboring</b>.")


@router.callback_query(F.data.startswith("order_approve:"))
async def order_approve_callback(call: CallbackQuery, bot: Bot) -> None:
    """Admin buyurtmani tasdiqlaydi — mahsulot avtomatik yetkaziladi."""
    if not is_admin(call.from_user.id):
        await call.answer("❌ Siz admin emassiz!", show_alert=True)
        return

    order_id = int(call.data.split(":")[1])
    order = await get_order(order_id)
    if not order:
        await call.answer("❌ Buyurtma topilmadi", show_alert=True)
        return
    if order["status"] != "pending":
        await call.answer("⚠️ Bu buyurtma allaqachon ko'rib chiqilgan!", show_alert=True)
        return

    await update_order_status(order_id, "approved")
    item = await get_shop_item(order["item_id"])

    # Yulduz mahsuloti bo'lsa — balansga avtomatik tushadi
    if order["category"] == "star" and item and item["deliver_stars"] > 0:
        await add_stars(order["telegram_id"], item["deliver_stars"])

    try:
        await bot.send_message(
            order["telegram_id"],
            f"✅ <b>To'lov tasdiqlandi!</b>\n\n"
            f"🛒 #{order_id} buyurtma: <b>{order['item_name']}</b> — {order['amount_uzs']:,} so'm\n\n"
            + (f"⭐ <b>{item['deliver_stars']} yulduz</b> hisobingizga qo'shildi!\n"
               if order["category"] == "star" and item and item["deliver_stars"] > 0 else "")
            + (f"🎁 {order['item_name']} sizga yuboriladi (egasi: @Kottabolladan).\n"
               if order["category"] != "star" else "")
            + "\nDo'kondan foydalanishda davom eting! 🛍️",
        )
    except TelegramForbiddenError:
        pass

    try:
        await call.message.edit_caption(
            caption=f"{call.message.caption}\n\n✅ <b>TASDIQLANDI</b> — {call.from_user.first_name}",
        )
    except TelegramBadRequest:
        pass
    await call.answer("✅ Tasdiqlandi!", show_alert=False)


@router.callback_query(F.data.startswith("order_reject:"))
async def order_reject_callback(call: CallbackQuery, bot: Bot) -> None:
    """Admin buyurtmani rad etadi."""
    if not is_admin(call.from_user.id):
        await call.answer("❌ Siz admin emassiz!", show_alert=True)
        return

    order_id = int(call.data.split(":")[1])
    order = await get_order(order_id)
    if not order:
        await call.answer("❌ Buyurtma topilmadi", show_alert=True)
        return
    if order["status"] != "pending":
        await call.answer("⚠️ Bu buyurtma allaqachon ko'rib chiqilgan!", show_alert=True)
        return

    await update_order_status(order_id, "rejected")

    try:
        await bot.send_message(
            order["telegram_id"],
            f"❌ <b>To'lov tasdiqlanmadi!</b>\n\n"
            f"🛒 #{order_id} buyurtma: <b>{order['item_name']}</b> — {order['amount_uzs']:,} so'm\n\n"
            f"Muammo bo'lsa, bot egasi bilan bog'laning: @Kottabolladan",
        )
    except TelegramForbiddenError:
        pass

    try:
        await call.message.edit_caption(
            caption=f"{call.message.caption}\n\n❌ <b>RAD ETILDI</b> — {call.from_user.first_name}",
        )
    except TelegramBadRequest:
        pass
    await call.answer("❌ Rad etildi!", show_alert=False)


# ============================================================
#  JEKPOT
# ============================================================

@router.callback_query(F.data.startswith("jackpot_buy:"))
async def jackpot_buy_callback(call: CallbackQuery) -> None:
    qty = int(call.data.split(":")[1])
    user = await get_user(call.from_user.id)
    if not user:
        await call.answer("❌ Avval /start ni bosing!", show_alert=True)
        return

    settings = await get_settings()
    total_cost = settings["jackpot_ticket_cost"] * qty

    if user["balance_stars"] < total_cost:
        await call.answer("❌ Balansingizda yetarli yulduz yo'q!", show_alert=True)
        return

    # Yulduz ayriladi, bilet va fondga qo'shiladi
    await deduct_stars(call.from_user.id, total_cost)
    await add_tickets(call.from_user.id, qty)
    await increase_fund(total_cost)

    await call.answer(f"✅ {qty} ta bilet sotib olindi!", show_alert=False)

    # Jekpot menyusini yangilaymiz
    try:
        await call.message.delete()
    except TelegramBadRequest:
        pass
    await show_jackpot(call.message.answer, call.from_user.id)


@router.callback_query(F.data == "jackpot_free")
async def jackpot_free_callback(call: CallbackQuery) -> None:
    """Kuniga 1 marta bepul bilet."""
    user = await get_user(call.from_user.id)
    if not user:
        await call.answer("❌ Avval /start ni bosing!", show_alert=True)
        return

    today = datetime.now().strftime("%Y-%m-%d")
    if user["last_free_ticket"] == today:
        await call.answer("❌ Kunlik bepul biletni allaqachon olgansiz!", show_alert=True)
        return

    await add_tickets(call.from_user.id, 1)
    await set_last_free_ticket(call.from_user.id, today)
    await call.answer("✅ Kunlik bepul bilet olindi!", show_alert=False)

    try:
        await call.message.delete()
    except TelegramBadRequest:
        pass
    await show_jackpot(call.message.answer, call.from_user.id)


# ============================================================
#  ADMIN PANEL
# ============================================================

def admin_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📊 Statistika", callback_data="admin:stats")
    kb.button(text="⚙️ Sozlamalar", callback_data="admin:settings")
    kb.button(text="🛒 Savdo boshqaruvi", callback_data="admin:shop")
    kb.button(text="🎯 Jekpot o'ynatish", callback_data="admin:jackpot")
    kb.button(text="📢 Rassilka", callback_data="admin:broadcast")
    kb.button(text="🔗 Kanallar", callback_data="admin:channels")
    kb.button(text="📞 Aloqa boshqaruvi", callback_data="admin:contacts")
    kb.adjust(2)
    return kb.as_markup()


@router.message(F.text == "👑 Admin panel")
@router.message(Command("admin"))
async def admin_panel(message: Message) -> None:
    if not is_admin(message.from_user.id):
        await message.answer("❌ Siz admin emassiz!")
        return
    await message.answer("👑 <b>Admin panel</b>\n\nQuyidagi bo'limlardan birini tanlang:", reply_markup=admin_keyboard())


@router.callback_query(F.data == "admin:stats")
async def admin_stats(call: CallbackQuery) -> None:
    if not is_admin(call.from_user.id):
        await call.answer("❌ Siz admin emassiz!", show_alert=True)
        return
    users = await get_all_users()
    settings = await get_settings()
    participants = await get_participants()
    total_tickets = sum(p["tickets_count"] for p in participants)

    text = (
        f"📊 <b>Statistika</b>\n\n"
        f"👥 Jami foydalanuvchilar: <b>{len(users)}</b>\n"
        f"💰 Jekpot fondi: <b>{settings['jackpot_fund']} ⭐</b>\n"
        f"🎟️ Faol biletlar: <b>{total_tickets}</b>\n"
        f"🎰 Ishtirokchilar: <b>{len(participants)}</b>\n"
        f"⭐ Referal mukofoti: {settings['ref_reward_stars']}\n"
        f"👥 Min. referallar: {settings['min_referals_required']}\n"
        f"🎟️ Bilet narxi: {settings['jackpot_ticket_cost']}\n"
        f"⏱ Avto o'yin oralig'i: {settings['jackpot_interval_hours']} soat\n"
        f"💸 Yechish minimumi: {settings['min_withdraw_stars']}"
    )
    await call.message.edit_text(text, reply_markup=back_to_admin_keyboard())
    await call.answer()


def back_to_admin_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🔙 Ortga", callback_data="admin")
    return kb.as_markup()


# ---------- Sozlamalar ----------

def settings_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="⭐ Referal mukofoti", callback_data="admin:set:ref_reward")
    kb.button(text="👥 Min. referallar", callback_data="admin:set:min_ref")
    kb.button(text="🎟️ Jekpot bilet narxi", callback_data="admin:set:jackpot_cost")
    kb.button(text="⏱ Jekpot oralig'i (soat)", callback_data="admin:set:jackpot_interval")
    kb.button(text="💸 Yulduz yechish minimumi", callback_data="admin:set:min_withdraw")
    kb.button(text="💳 To'lov kartasi", callback_data="admin:set:pay_card")
    kb.button(text="⭐ Otziv kanali", callback_data="admin:set:reviews_channel")
    kb.button(text="🔙 Ortga", callback_data="admin")
    kb.adjust(1)
    return kb.as_markup()


@router.callback_query(F.data == "admin:settings")
async def admin_settings(call: CallbackQuery) -> None:
    if not is_admin(call.from_user.id):
        await call.answer("❌ Siz admin emassiz!", show_alert=True)
        return
    s = await get_settings()
    reviews_display = s["reviews_channel"] or "❌ o'rnatilmagan"
    text = (
        f"⚙️ <b>Sozlamalar</b>\n\n"
        f"⭐ Referal mukofoti: <b>{s['ref_reward_stars']} ⭐</b>\n"
        f"👥 Xarid uchun min. referallar: <b>{s['min_referals_required']}</b>\n"
        f"🎟️ Jekpot bilet narxi: <b>{s['jackpot_ticket_cost']} ⭐</b>\n"
        f"⏱ Jekpot oralig'i: <b>{s['jackpot_interval_hours']} soat</b>\n"
        f"💸 Yulduz yechish minimumi: <b>{s['min_withdraw_stars']} ⭐</b>\n"
        f"💳 To'lov kartasi: <code>{s['pay_card']}</code>\n"
        f"⭐ Otziv: {reviews_display}\n\n"
        f"O'zgartirmoqchi bo'lgan qiymatni tanlang:"
    )
    await call.message.edit_text(text, reply_markup=settings_keyboard())
    await call.answer()


@router.callback_query(F.data == "admin:set:ref_reward")
async def set_ref_reward(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SettingsStates.ref_reward)
    await call.message.edit_text(
        "✏️ <b>Yangi referal mukofotini yozing:</b>\n"
        "(1 ta referal uchun nechta yulduz beriladi)",
    )
    await call.answer()


@router.message(SettingsStates.ref_reward)
async def ref_reward_input(message: Message, state: FSMContext) -> None:
    try:
        value = int(message.text)
    except ValueError:
        await message.answer("❌ Iltimos, butun son kiriting!")
        return
    if value < 0:
        await message.answer("❌ Mukofot manfiy bo'lishi mumkin emas!")
        return
    await update_settings(ref_reward_stars=value)
    await state.clear()
    await message.answer(f"✅ Referal mukofoti <b>{value} ⭐</b> qilib o'rnatildi.", reply_markup=admin_keyboard())


@router.callback_query(F.data == "admin:set:min_ref")
async def set_min_ref(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SettingsStates.min_referals)
    await call.message.edit_text(
        "✏️ <b>Yangi minimal cheklovni yozing:</b>\n"
        "(xarid qilish uchun nechta odam taklif qilingan bo'lishi kerak)",
    )
    await call.answer()


@router.message(SettingsStates.min_referals)
async def min_ref_input(message: Message, state: FSMContext) -> None:
    try:
        value = int(message.text)
    except ValueError:
        await message.answer("❌ Iltimos, butun son kiriting!")
        return
    if value < 0:
        await message.answer("❌ Minimal manfiy bo'lishi mumkin emas!")
        return
    await update_settings(min_referals_required=value)
    await state.clear()
    await message.answer(f"✅ Minimal referallar <b>{value}</b> qilib o'rnatildi.", reply_markup=admin_keyboard())


@router.callback_query(F.data == "admin:set:jackpot_cost")
async def set_jackpot_cost(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SettingsStates.jackpot_cost)
    await call.message.edit_text(
        "✏️ <b>Yangi jekpot bilet narxini yozing:</b>\n"
        "(1 ta bilet necha yulduz bo'ladi)",
    )
    await call.answer()


@router.message(SettingsStates.jackpot_cost)
async def jackpot_cost_input(message: Message, state: FSMContext) -> None:
    try:
        value = int(message.text)
    except ValueError:
        await message.answer("❌ Iltimos, butun son kiriting!")
        return
    if value < 1:
        await message.answer("❌ Bilet narxi kamida 1 ⭐ bo'lishi kerak!")
        return
    await update_settings(jackpot_ticket_cost=value)
    await state.clear()
    await message.answer(f"✅ Jekpot bilet narxi <b>{value} ⭐</b> qilib o'rnatildi.", reply_markup=admin_keyboard())


@router.callback_query(F.data == "admin:set:jackpot_interval")
async def set_jackpot_interval(call: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(call.from_user.id):
        await call.answer("❌ Siz admin emassiz!", show_alert=True)
        return
    await state.set_state(SettingsStates.jackpot_interval)
    await call.message.edit_text(
        "⏱ <b>Avtomatik o'yin oralig'ini yozing (soat):</b>\n"
        "(masalan: 24 — har kuni, 12 — har 12 soatda o'yin o'tkaziladi)\n"
        "0 yozsangiz, avtomatik o'yin o'chadi (faqat qo'lda).",
    )
    await call.answer()


@router.message(SettingsStates.jackpot_interval)
async def jackpot_interval_input(message: Message, state: FSMContext) -> None:
    try:
        value = int(message.text)
    except ValueError:
        await message.answer("❌ Iltimos, butun son kiriting!")
        return
    if value < 0:
        await message.answer("❌ Oraliq manfiy bo'lishi mumkin emas!")
        return
    await update_settings(jackpot_interval_hours=value)
    # Yangi intervalga qarab keyingi o'yin vaqtini qayta hisoblaymiz
    if value > 0:
        now = int(datetime.now().timestamp())
        await update_settings(jackpot_next_draw=now + value * 3600)
    await state.clear()
    mode = "o'chirildi (faqat qo'lda o'yin)" if value == 0 else f"{value} soatga o'rnatildi"
    await message.answer(f"✅ Avtomatik jekpot oralig'i <b>{mode}</b>.", reply_markup=admin_keyboard())


@router.callback_query(F.data == "admin:set:min_withdraw")
async def set_min_withdraw(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SettingsStates.min_withdraw)
    await call.message.edit_text(
        "✏️ <b>Yangi yulduz yechish minimumini yozing:</b>\n"
        "(foydalanuvchi qancha yulduz yig'sa, yechib olish mumkin bo'ladi)",
    )
    await call.answer()


@router.message(SettingsStates.min_withdraw)
async def min_withdraw_input(message: Message, state: FSMContext) -> None:
    try:
        value = int(message.text)
    except ValueError:
        await message.answer("❌ Iltimos, butun son kiriting!")
        return
    if value < 0:
        await message.answer("❌ Minimal manfiy bo'lishi mumkin emas!")
        return
    await update_settings(min_withdraw_stars=value)
    await state.clear()
    await message.answer(f"✅ Yulduz yechish minimumi <b>{value} ⭐</b> qilib o'rnatildi.", reply_markup=admin_keyboard())


@router.callback_query(F.data == "admin:set:pay_card")
async def set_pay_card(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SettingsStates.pay_card)
    await call.message.edit_text(
        "💳 <b>Yangi to'lov ma'lumotlarini yozing:</b>\n"
        "Karta raqami, emojilar va yozuvlar qo'shishingiz mumkin.\n"
        "Masalan:\n"
        "<code>9860 1801 0468 1937</code>\n"
        "yoki\n"
        "⭐ <code>9860 1801 0468 1937</code> 💎 Humo — rahmat! 🙏",
    )
    await call.answer()


@router.message(SettingsStates.pay_card)
async def pay_card_input(message: Message, state: FSMContext) -> None:
    raw = message.text.strip()
    if len(raw) < 3:
        await message.answer("❌ Iltimos, kamida 3 ta belgi kiriting!")
        return
    await update_settings(pay_card=raw)
    await state.clear()
    await message.answer(f"✅ To'lov ma'lumotlari saqlandi:\n\n💳 <code>{raw}</code>", reply_markup=admin_keyboard())


@router.callback_query(F.data == "admin:set:reviews_channel")
async def set_reviews_channel(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SettingsStates.reviews_channel)
    await call.message.edit_text(
        "⭐ <b>Otziv kanalini yuboring:</b>\n"
        "(masalan: <code>@mychannel</code> yoki <code>https://t.me/mychannel</code>)",
    )
    await call.answer()


@router.message(SettingsStates.reviews_channel)
async def reviews_channel_input(message: Message, state: FSMContext) -> None:
    raw = message.text.strip()
    if len(raw) < 3:
        await message.answer("❌ Iltimos, to'g'ri havola yuboring!")
        return
    await update_settings(reviews_channel=raw)
    await state.clear()
    await message.answer(f"✅ Otziv kanali saqlandi: <b>{raw}</b>", reply_markup=admin_keyboard())


# ---------- Savdo boshqaruvi ----------

def shop_management_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ 🎁 Gift qo'shish", callback_data="admin:add:gift")
    kb.button(text="➕ ⭐ Yulduz qo'shish", callback_data="admin:add:star")
    kb.button(text="➕ 💎 Premium qo'shish", callback_data="admin:add:premium")
    kb.button(text="🗑️ Mahsulot o'chirish", callback_data="admin:shop_del")
    kb.button(text="🔙 Ortga", callback_data="admin")
    kb.adjust(1)
    return kb.as_markup()


@router.callback_query(F.data == "admin:shop")
async def admin_shop(call: CallbackQuery) -> None:
    if not is_admin(call.from_user.id):
        await call.answer("❌ Siz admin emassiz!", show_alert=True)
        return
    await call.message.edit_text("🛒 <b>Savdo boshqaruvi</b>\n\nAmalni tanlang:", reply_markup=shop_management_keyboard())
    await call.answer()


# --- Yangi mahsulot qo'shish (FSM) ---

@router.callback_query(F.data.startswith("admin:add:"))
async def admin_add_item_start(call: CallbackQuery, state: FSMContext) -> None:
    category = call.data.split(":")[2]
    await state.set_state(AddItemStates.category)
    await state.update_data(category=category)
    await call.message.edit_text(
        f"✏️ {CATEGORIES.get(category, category)} toifasiga yangi mahsulot qo'shilyapti.\n"
        f"<b>Mahsulot nomini yozing:</b>",
    )
    await call.answer()


@router.message(AddItemStates.category)
async def add_item_name(message: Message, state: FSMContext) -> None:
    await state.update_data(name=message.text.strip())
    await state.set_state(AddItemStates.name)
    await message.answer("💲 <b>UZS narxini yozing</b> (so'mda, son ko'rinishida):")


@router.message(AddItemStates.name)
async def add_item_price(message: Message, state: FSMContext) -> None:
    try:
        price_uzs = int(message.text.replace(" ", ""))
    except ValueError:
        await message.answer("❌ Iltimos, butun son kiriting!")
        return
    await state.update_data(price_uzs=price_uzs)

    data = await state.get_data()
    if data["category"] == "star":
        # Yulduzlar toifasi faqat UZS bilan — yulduz miqdori ham kerak
        await state.update_data(price_uzs=price_uzs)
        await state.set_state(AddItemStates.deliver_stars)
        await message.answer("⭐ <b>Mahsulotda nechta yulduz bor?</b>\n(to'lov tasdiqlangach shuncha yulduz balansga tushadi)")
    else:
        # Gift / Premium: yulduz narxi ham kerak
        await state.set_state(AddItemStates.price)
        await message.answer("⭐ <b>Yulduz narxini yozing</b> (yulduzlarda, son ko'rinishida):")


@router.message(AddItemStates.deliver_stars)
async def add_item_deliver_stars(message: Message, state: FSMContext) -> None:
    try:
        deliver = int(message.text)
    except ValueError:
        await message.answer("❌ Iltimos, butun son kiriting!")
        return
    data = await state.get_data()
    await add_shop_item(
        category=data["category"],
        name=data["name"],
        price_stars=0,
        price_uzs=data["price_uzs"],
        deliver_stars=deliver,
    )
    await state.clear()
    await message.answer(
        f"✅ Mahsulot qo'shildi!\n\n{data['name']} — {data['price_uzs']:,} so'm ({deliver} ⭐ yetkaziladi)",
        reply_markup=admin_keyboard(),
    )


@router.message(AddItemStates.price)
async def add_item_stars_price(message: Message, state: FSMContext) -> None:
    try:
        price_stars = int(message.text)
    except ValueError:
        await message.answer("❌ Iltimos, butun son kiriting!")
        return
    data = await state.get_data()
    await add_shop_item(
        category=data["category"],
        name=data["name"],
        price_stars=price_stars,
        price_uzs=data["price_uzs"],
    )
    await state.clear()
    await message.answer(
        f"✅ Mahsulot qo'shildi!\n\n{data['name']} — {price_stars} ⭐ / {data['price_uzs']:,} so'm",
        reply_markup=admin_keyboard(),
    )


# --- Mahsulot o'chirish ---

@router.callback_query(F.data == "admin:shop_del")
async def admin_shop_delete_list(call: CallbackQuery) -> None:
    items = await get_all_shop_items()
    if not items:
        await call.answer("❌ Hech qanday mahsulot yo'q", show_alert=True)
        return
    kb = InlineKeyboardBuilder()
    for item in items:
        label = CATEGORIES.get(item["category"], item["category"])
        kb.button(text=f"❌ {label} {item['name']} ({item['price_stars']}⭐)", callback_data=f"admin:delitem:{item['id']}")
    kb.button(text="🔙 Ortga", callback_data="admin:shop")
    kb.adjust(1)
    await call.message.edit_text("🗑️ <b>O'chirmoqchi bo'lgan mahsulotni tanlang:</b>", reply_markup=kb.as_markup())
    await call.answer()


@router.callback_query(F.data.startswith("admin:delitem:"))
async def admin_delete_item(call: CallbackQuery) -> None:
    item_id = int(call.data.split(":")[2])
    await delete_shop_item(item_id)
    await call.answer("✅ O'chirildi!", show_alert=True)
    await call.message.edit_text("🗑️ <b>Yana mahsulot o'chirasizmi?</b>", reply_markup=shop_management_keyboard())


# ---------- Jekpot o'ynatish ----------

@router.callback_query(F.data == "admin:jackpot")
async def admin_jackpot(call: CallbackQuery) -> None:
    if not is_admin(call.from_user.id):
        await call.answer("❌ Siz admin emassiz!", show_alert=True)
        return
    settings = await get_settings()
    participants = await get_participants()
    total_tickets = sum(p["tickets_count"] for p in participants)

    if not participants:
        await call.answer("❌ Ishtirokchilar yo'q, o'yin o'tkazib bo'lmaydi!", show_alert=True)
        return

    kb = InlineKeyboardBuilder()
    kb.button(text="🎯 Ha, o'ynatish", callback_data="admin:jackpot_confirm")
    kb.button(text="❌ Bekor qilish", callback_data="admin")
    kb.adjust(1)

    await call.message.edit_text(
        f"🎯 <b>Jekpot o'yinini o'tkazish</b>\n\n"
        f"💰 Fond: <b>{settings['jackpot_fund']} ⭐</b>\n"
        f"🎟️ Jami biletlar: <b>{total_tickets}</b>\n"
        f"👥 Ishtirokchilar: <b>{len(participants)}</b>\n\n"
        f"G'olib tasodifiy tanlanadi (biletlar soniga ko'ra imkoniyat). Davom etasizmi?",
        reply_markup=kb.as_markup(),
    )
    await call.answer()


async def run_jackpot_draw(bot: Bot, admin_call: CallbackQuery | None = None) -> bool:
    """Jekpot o'yinini o'tkazadi.

    admin_call berilsa — qo'lda (admin) o'yin, xabar shu chatda yangilanadi.
    Aks holda — avtomatik taymer o'yini.
    """
    settings = await get_settings()
    participants = await get_participants()
    if not participants:
        if admin_call:
            await admin_call.answer("❌ Ishtirokchilar yo'q!", show_alert=True)
        return False

    fund = settings["jackpot_fund"]
    if fund <= 0:
        if admin_call:
            await admin_call.answer("❌ Jekpot fondi bo'sh (0 ⭐)! Avval biletlar sotilishi kerak.", show_alert=True)
        return False

    # Og'irlik bo'yicha g'olib tanlash (har bilet = 1 imkoniyat) — samarali usul
    total = sum(p["tickets_count"] for p in participants)
    r = random.randint(1, total)
    winner_id = None
    cum = 0
    for p in participants:
        cum += p["tickets_count"]
        if r <= cum:
            winner_id = p["telegram_id"]
            break

    if winner_id is None:
        if admin_call:
            await admin_call.answer("❌ G'olib tanlashda xatolik!", show_alert=True)
        return False

    # G'olibga fondni o'tkazamiz
    await add_stars(winner_id, fund)
    await reset_fund()
    await clear_participants()

    winner = await get_user(winner_id)
    winner_name = str(winner_id)
    try:
        wm = await bot.get_chat(winner_id)
        winner_name = wm.full_name or str(winner_id)
    except Exception:
        pass

    await add_jackpot_history(winner_id, winner_name, fund)

    announcement = (
        f"🏆 <b>JEKPOT G'OLIBI!</b>\n\n"
        f"👤 {winner_name}\n"
        f"🆔 <code>{winner_id}</code>\n"
        f"💰 Yutug'i: <b>{fund} ⭐</b>\n\n"
        f"Tabriklaymiz! 🎉"
    )

    if admin_call:
        try:
            await admin_call.message.edit_text(announcement)
        except TelegramBadRequest:
            pass
        await admin_call.answer("✅ O'yin yakunlandi!", show_alert=True)

    # G'olibga xabar
    try:
        await bot.send_message(winner_id, f"🏆 <b>Tabriklaymiz, siz jekpot yutdingiz!</b>\n💰 +{fund} ⭐ hisobingizga qo'shildi!")
    except (TelegramBadRequest, TelegramForbiddenError):
        pass

    # Barchaga e'lon
    users = await get_all_users()
    for i, u in enumerate(users):
        if u["telegram_id"] == winner_id:
            continue
        try:
            await bot.send_message(u["telegram_id"], announcement)
        except (TelegramBadRequest, TelegramForbiddenError):
            continue
        # Telegram flood'iga tushmaslik uchun kichik pauza
        if i % 20 == 19:
            await asyncio.sleep(1)

    return True


async def jackpot_timer_loop(bot: Bot) -> None:
    """Avtomatik jekpot: belgilangan vaqt yetganda o'yin o'tkazadi."""
    while True:
        try:
            settings = await get_settings()
            if settings:
                now = int(datetime.now().timestamp())
                interval = settings.get("jackpot_interval_hours") or 0
                next_draw = settings.get("jackpot_next_draw") or 0
                if interval <= 0:
                    # Avtomatik o'yin o'chiq — faqat qo'lda
                    if next_draw:
                        await update_settings(jackpot_next_draw=0)
                elif not next_draw:
                    # Birinchi o'yin vaqtini belgilaymiz
                    await update_settings(jackpot_next_draw=now + interval * 3600)
                elif now >= next_draw:
                    await run_jackpot_draw(bot)
                    # Keyingi o'yinni rejalashtiramiz (o'yin o'tmasa ham)
                    await update_settings(jackpot_next_draw=now + interval * 3600)
        except Exception as e:
            logger.exception("Jekpot taymerida xatolik: %s", e)
        await asyncio.sleep(30)


@router.callback_query(F.data == "admin:jackpot_confirm")
async def admin_jackpot_confirm(call: CallbackQuery, bot: Bot) -> None:
    if not is_admin(call.from_user.id):
        await call.answer("❌ Siz admin emassiz!", show_alert=True)
        return
    await run_jackpot_draw(bot, admin_call=call)


# ---------- Rassilka ----------

@router.callback_query(F.data == "admin:broadcast")
async def admin_broadcast(call: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(call.from_user.id):
        await call.answer("❌ Siz admin emassiz!", show_alert=True)
        return
    await state.set_state(BroadcastStates.content)
    await call.message.edit_text(
        "📢 <b>Rassilka</b>\n\n"
        "Barcha foydalanuvchilarga yuboriladigan matn yoki rasmni yuboring.\n"
        "(Rasm yuborsangiz, caption sifatida matn ham jo'natiladi)\n"
        "Bekor qilish: /cancel",
    )
    await call.answer()


@router.message(BroadcastStates.content)
async def broadcast_received(message: Message, bot: Bot, state: FSMContext) -> None:
    await state.clear()

    photo_id = message.photo[-1].file_id if message.photo else None
    text = message.caption or message.text or ""

    users = await get_all_users()
    sent, failed = 0, 0
    for u in users:
        try:
            if photo_id:
                await bot.send_photo(u["telegram_id"], photo_id, caption=text)
            else:
                await bot.send_message(u["telegram_id"], text)
            sent += 1
        except (TelegramBadRequest, TelegramForbiddenError):
            failed += 1

    await message.answer(
        f"📢 <b>Rassilka yakunlandi!</b>\n\n"
        f"✅ Yuborildi: <b>{sent}</b>\n"
        f"❌ Xatolik: <b>{failed}</b>",
        reply_markup=admin_keyboard(),
    )


# ---------- Kanallar ----------

def channels_menu_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Kanal qo'shish", callback_data="admin:channel_add")
    kb.button(text="🔙 Ortga", callback_data="admin")
    kb.adjust(1)
    return kb.as_markup()


@router.callback_query(F.data == "admin:channels")
async def admin_channels(call: CallbackQuery) -> None:
    if not is_admin(call.from_user.id):
        await call.answer("❌ Siz admin emassiz!", show_alert=True)
        return
    channels = await get_channels()

    text = "🔗 <b>Majburiy kanallar</b>\n\n"
    if channels:
        for i, ch in enumerate(channels, start=1):
            text += f"{i}. {ch['channel_id']} — {ch['invite_link']}\n"
    else:
        text += "Hozircha kanallar yo'q.\n"

    kb = InlineKeyboardBuilder()
    for ch in channels:
        kb.button(text=f"🗑️ {ch['invite_link'][:30]}", callback_data=f"admin:chdel:{ch['id']}")
    kb.button(text="➕ Kanal qo'shish", callback_data="admin:channel_add")
    kb.button(text="🔙 Ortga", callback_data="admin")
    kb.adjust(1)

    await call.message.edit_text(text, reply_markup=kb.as_markup())
    await call.answer()


@router.callback_query(F.data.startswith("admin:chdel:"))
async def admin_delete_channel(call: CallbackQuery) -> None:
    row_id = int(call.data.split(":")[2])
    await delete_channel(row_id)
    await call.answer("✅ Kanal o'chirildi!", show_alert=True)
    await admin_channels(call)


@router.callback_query(F.data == "admin:channel_add")
async def admin_add_channel(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AddChannelStates.channel_id)
    await call.message.edit_text(
        "🔗 <b>Kanal ID sini yuboring:</b>\n"
        "(masalan: <code>-1001234567890</code>)\n"
        "Bot kanalda admin bo'lishi kerak!",
    )
    await call.answer()


@router.message(AddChannelStates.channel_id)
async def channel_id_received(message: Message, state: FSMContext) -> None:
    raw = message.text.strip()
    await state.update_data(channel_id=raw)
    await state.set_state(AddChannelStates.invite_link)
    await message.answer("🔗 <b>Kanal havolasini yuboring:</b>\n(masalan: <code>https://t.me/mychannel</code>)")


@router.message(AddChannelStates.invite_link)
async def channel_link_received(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    await add_channel(data["channel_id"], message.text.strip())
    await state.clear()
    await message.answer("✅ <b>Kanal qo'shildi!</b>\nEndi foydalanuvchilar shu kanalga a'zo bo'lmaguncha botdan foydalana olmaydi.", reply_markup=admin_keyboard())


# ---------- Aloqa kontaktlari (admin) ----------

@router.callback_query(F.data == "admin:contacts")
async def admin_contacts(call: CallbackQuery) -> None:
    if not is_admin(call.from_user.id):
        await call.answer("❌ Siz admin emassiz!", show_alert=True)
        return
    contacts = await get_contacts()

    text = "📞 <b>Aloqa boshqaruvi</b>\n\n"
    if contacts:
        for i, c in enumerate(contacts, start=1):
            text += f"{i}. {c['label']} — @{c['username']}\n"
    else:
        text += "Hozircha kontaktlar yo'q.\n"

    kb = InlineKeyboardBuilder()
    for c in contacts:
        kb.button(text=f"🗑️ {c['label']}", callback_data=f"admin:cdel:{c['id']}")
    kb.button(text="➕ Kontakt qo'shish", callback_data="admin:contact_add")
    kb.button(text="🔙 Ortga", callback_data="admin")
    kb.adjust(1)

    await call.message.edit_text(text, reply_markup=kb.as_markup())
    await call.answer()


@router.callback_query(F.data.startswith("admin:cdel:"))
async def admin_delete_contact(call: CallbackQuery) -> None:
    if not is_admin(call.from_user.id):
        await call.answer("❌ Siz admin emassiz!", show_alert=True)
        return
    row_id = int(call.data.split(":")[2])
    await delete_contact(row_id)
    await call.answer("✅ Kontakt o'chirildi!", show_alert=True)
    await admin_contacts(call)


@router.callback_query(F.data == "admin:contact_add")
async def admin_add_contact(call: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(call.from_user.id):
        await call.answer("❌ Siz admin emassiz!", show_alert=True)
        return
    await state.set_state(AddContactStates.label)
    await call.message.edit_text(
        "📞 <b>Kontakt yorlig'ini yuboring:</b>\n"
        "(masalan: 🎁 Sponsor, 👨‍💻 Dasturchi)",
    )
    await call.answer()


@router.message(AddContactStates.label)
async def contact_label_received(message: Message, state: FSMContext) -> None:
    await state.update_data(label=message.text.strip())
    await state.set_state(AddContactStates.username)
    await message.answer(
        "📞 <b>Telegram username yuboring:</b>\n"
        "(masalan: <code>mychannel</code> yoki <code>@mychannel</code>)",
    )


@router.message(AddContactStates.username)
async def contact_username_received(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    username = message.text.strip().lstrip("@")
    await add_contact(data["label"], username)
    await state.clear()
    await message.answer("✅ <b>Kontakt qo'shildi!</b>", reply_markup=admin_keyboard())


# ---------- /cancel va umumiy boshqaruv ----------

@router.message(Command("cancel"))
async def cancel_handler(message: Message, state: FSMContext) -> None:
    current = await state.get_state()
    if current is None:
        await message.answer("❌ Hozir hech qanday jarayon yo'q.")
        return
    await state.clear()
    await message.answer("❌ Jarayon bekor qilindi.", reply_markup=admin_keyboard() if is_admin(message.from_user.id) else main_menu_keyboard(message.from_user.id))


# ============================================================
#  ASOSIY / ORTGA (main menu)
# ============================================================

@router.callback_query(F.data == "main_menu")
async def main_menu_callback(call: CallbackQuery) -> None:
    try:
        await call.message.delete()
    except TelegramBadRequest:
        pass
    await call.message.answer(
        "Bosh menyu 👇",
        reply_markup=main_menu_keyboard(call.from_user.id),
    )
    await call.answer()


@router.callback_query(F.data == "admin")
async def admin_back(call: CallbackQuery) -> None:
    await call.message.edit_text("👑 <b>Admin panel</b>\n\nQuyidagi bo'limlardan birini tanlang:", reply_markup=admin_keyboard())
    await call.answer()


# ============================================================
#  MAIN (ISHLAB CHIQARISH)
# ============================================================

async def on_startup(bot: Bot) -> None:
    global _bot
    _bot = bot
    await db_init()
    if WEBHOOK_URL:
        webhook_full = f"{WEBHOOK_URL}{WEBHOOK_PATH}"
        await bot.set_webhook(webhook_full)
        logger.info("Webhook o'rnatildi: %s", webhook_full)
    else:
        await bot.delete_webhook(drop_pending_updates=True)
        logger.info("Polling rejimi (webhook o'chirildi)")


async def on_shutdown(bot: Bot) -> None:
    logger.info("Bot to'xtatilmoqda...")


async def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN sozlanmagan! .env faylini tekshiring.")

    # DB jadvallarini darhol yaratamiz (startup hooklarga tayanmasdan)
    await db_init()

    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp.include_router(router)

    # Aiogram 3.x da on_startup/on_shutdown start_polling kwarg'i emas,
    # alohida register qilinadi:
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    # Avtomatik jekpot o'yin taymeri (polling ham, webhook ham ishlaydi)
    asyncio.create_task(jackpot_timer_loop(bot))

    if WEBHOOK_URL:
        # Render/webhook rejimi
        from aiohttp import web
        from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

        app = web.Application()
        SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path=WEBHOOK_PATH)
        setup_application(app, dp, bot=bot)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, host=WEBAPP_HOST, port=WEBAPP_PORT)
        await on_startup(bot)
        await site.start()
        logger.info("Webhook server ishga tushdi: %s:%s%s", WEBAPP_HOST, WEBAPP_PORT, WEBHOOK_PATH)
        await asyncio.Event().wait()
    else:
        await dp.start_polling(bot)


if __name__ == "__main__":
    import asyncio

    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot to'xtatildi")
