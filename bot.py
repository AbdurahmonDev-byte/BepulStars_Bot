# -*- coding: utf-8 -*-
"""
🤖 ReferalBot — t.me/giftsme_bot kloni
========================================
Yulduzlar (Stars) · Do'kon (Shop) · Boxlar (Lootbox)

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
import hashlib
import hmac
import html
import json
import logging
import os
import random
import string
from datetime import datetime, timedelta
from urllib.parse import parse_qsl, quote

import db_compat as aiosqlite  # noqa: N812 — Turso (doimiy tashqi baza) yoki lokal SQLite'ga
                                # shaffof ulanish uchun moslashtiruvchi qatlam (pastdagi
                                # izohga qarang: MA'LUMOTLARNI DOIMIY SAQLASH).
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatMemberStatus, ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    LabeledPrice,
    MenuButtonWebApp,
    Message,
    PreCheckoutQuery,
    ReplyKeyboardMarkup,
    WebAppInfo,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

load_dotenv()
from aiohttp import web

async def handle_ping(request):
    return web.Response(text="Bot is alive!")

async def start_web_server():
    app = web.Application()
    app.router.add_get('/', handle_ping)
    app.router.add_get('/webhook', handle_ping)  # Render health-check shu yo'lni tekshiradi
    register_webapp_routes(app)  # /webapp, /api/shop, /api/create_invoice
    runner = web.AppRunner(app)
    await runner.setup()
    # Render PORT env orqali portni beradi — qattiq yozilgan 10000 emas, o'shani ishlatamiz
    port = int(os.getenv("PORT", "10000"))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()

# ============================================================
#  GLOBAL SOZLAMALAR (env fayldan o'qiladi)
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x.strip()]

# DB fayli qayerdan ishga tushirilishidan qat'i nazar, bot.py yonida bo'ladi
# (faqat TURSO_DATABASE_URL sozlanmagan holatda ishlatiladi — pastga qarang).
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.getenv("DB_PATH", os.path.join(BASE_DIR, "bot.db"))

# ---------- MA'LUMOTLARNI DOIMIY SAQLASH (Render free tarifi uchun muhim!) ----------
# Render'ning free tarifida doimiy disk yo'q — bot.db kabi lokal fayllar har
# redeploy/restart'da o'chib ketadi. Buning oldini olish uchun .env (yoki Render
# muhit o'zgaruvchilari)da TURSO_DATABASE_URL va TURSO_AUTH_TOKEN sozlansa, bot
# ma'lumotlarni Turso (bepul, doimiy, tashqi libSQL bazasi)da saqlaydi va ular
# hech qachon yo'qolmaydi. Sozlanmasa, bot avvalgidek oddiy lokal bot.db bilan
# ishlayveradi (faqat lokal/test muhiti uchun mos, Render production uchun EMAS).
# Batafsil: TURSO_SETUP.md faylini qarang.

# Render / webhook sozlamalari
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").rstrip("/")
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "/webhook")
WEBAPP_HOST = os.getenv("WEBAPP_HOST", "0.0.0.0")
WEBAPP_PORT = int(os.getenv("PORT", "8000"))

# Mini App (Telegram Web App do'kon) uchun ochiq HTTPS manzil. Webhook rejimida
# WEBHOOK_URL allaqachon ochiq manzil bo'lgani uchun standart shu ishlatiladi;
# polling rejimida (Render'da WEBHOOK_URL bo'sh bo'lsa ham) xizmatning o'zi
# baribir ochiq https://...onrender.com manzilda turadi — shuni qo'lda
# PUBLIC_BASE_URL orqali ko'rsatish kerak. Batafsil: MINI_APP.md.
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", WEBHOOK_URL).rstrip("/")

# Do'kon toifalari
CATEGORIES = {
    "gift": "🎁 Gift",
    "star": "⭐ Yulduz",
    "premium": "💎 Premium",
    "nft": "🖼 NFT",
    "promo": "🎟 Promokodlar",
}

# Haqiqiy Telegram gift avtomatik yuborilganda unga qo'shiladigan sarlavha matni
# (admin panelda o'zgartirilishi mumkin). {item} — gift nomi bilan almashtiriladi.
DEFAULT_GIFT_CAPTION = "🎁 {item} — Stars Bot'dan sovg'a!"

# Telegram_id -> kutilayotgan referrer (majburiy kanalga a'zolikdan keyin berish uchun)
pending_ref = {}

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(name)s - %(message)s")
logger = logging.getLogger(__name__)


def safe_error_text(e: object, limit: int = 300) -> str:
    """Python xatosini Telegram HTML xabariga xavfsiz qo'yish uchun tayyorlaydi:
    HTML-escape qiladi (Telegram "<...>" ni noto'g'ri teg deb hisoblab, butun
    xabarni rad etmasligi uchun) VA uzunligini cheklaydi (ba'zi xatolar,
    masalan pydantic validatsiya xatolari, minglab belgidan iborat bo'lishi
    mumkin — Telegram xabarlari 4096 belgidan oshsa "message is too long"
    xatosi bilan butunlay yuborilmay qoladi)."""
    text = str(e)
    if len(text) > limit:
        text = text[:limit] + "…"
    return html.escape(text)


router = Router()
dp = Dispatcher()


# ============================================================
#  FSM (STATE) HOLATLARI
# ============================================================

class SettingsStates(StatesGroup):
    """Admin sozlamalarni matn orqali o'zgartirishi uchun."""
    ref_reward = State()      # bitta referal uchun yulduz
    min_referals = State()    # xarid uchun minimal referallar
    min_withdraw = State()    # yulduz yechish uchun minimal
    pay_card = State()        # to'lov karta raqami
    reviews_channel = State() # otziv kanali
    nft_group = State()       # NFT sotiladigan guruh linki
    gift_caption = State()    # avtomatik gift bilan boradigan matn


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


class TgGiftStates(StatesGroup):
    """Do'kon mahsulotini haqiqiy Telegram Gift ID'siga bog'lash — shu orqali
    foydalanuvchi giftni yechib olganda avtomatik yuboriladi."""
    gift_id = State()


class BoxStates(StatesGroup):
    """Admin box sozlamalarini o'zgartirishi."""
    input = State()


class PromoRedeemStates(StatesGroup):
    """Foydalanuvchi promokod kiritayotgan holat."""
    code = State()


class PromoAdminStates(StatesGroup):
    """Admin promokod yaratishi (kod) va uning box-sozlamalarini
    tahrirlashi (bitta maydonni kiritish — oddiy box tahrirlash bilan bir xil)."""
    new_code = State()
    field_input = State()


class TopupStates(StatesGroup):
    """Botning haqiqiy Telegram Stars balansini admin o'zi to'ldirishi (invoys orqali,
    hech qanday komissiyasiz — to'liq summasi botning real balansiga tushadi)."""
    amount = State()


class AdminUserStates(StatesGroup):
    """Admin foydalanuvchi profilini qidirishi va uning (ichki, virtual)
    ⭐ balansini boshqarishi — masalan xohlagan payt 0 ga tushirish."""
    search = State()
    set_balance = State()


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
                last_daily_box TEXT DEFAULT '',
                joined_at TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                ref_reward_stars INTEGER NOT NULL DEFAULT 5,
                min_referals_required INTEGER NOT NULL DEFAULT 3,
                min_withdraw_stars INTEGER NOT NULL DEFAULT 100,
                pay_card TEXT NOT NULL DEFAULT '9860180104681937',
                reviews_channel TEXT DEFAULT '',
                nft_group TEXT DEFAULT ''
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
        await db.execute("""
            CREATE TABLE IF NOT EXISTS withdrawals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                user_name TEXT DEFAULT '',
                username TEXT DEFAULT '',
                kind TEXT NOT NULL,
                amount_stars INTEGER NOT NULL DEFAULT 0,
                item_name TEXT DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                resolved_at TEXT DEFAULT ''
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS gift_claims (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                item_id INTEGER NOT NULL,
                item_name TEXT NOT NULL,
                price_stars INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'pending',
                source TEXT NOT NULL DEFAULT 'box',
                created_at TEXT NOT NULL,
                resolved_at TEXT DEFAULT ''
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS gift_variants (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_id INTEGER NOT NULL,
                tg_gift_id TEXT NOT NULL,
                label TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS boxes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                box_id TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                cost INTEGER NOT NULL DEFAULT 0,
                cost_tgstars INTEGER NOT NULL DEFAULT 0,
                star_min INTEGER NOT NULL DEFAULT 0,
                star_max INTEGER NOT NULL DEFAULT 0,
                gift_drop_prob REAL NOT NULL DEFAULT 0.5,
                gift_pool_size INTEGER NOT NULL DEFAULT 1,
                gift_category TEXT NOT NULL DEFAULT 'gift',
                once_per_day INTEGER NOT NULL DEFAULT 0,
                desc_text TEXT NOT NULL DEFAULT '',
                tgstars_bonus_percent REAL NOT NULL DEFAULT 0
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS promo_codes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL DEFAULT '',
                star_min INTEGER NOT NULL DEFAULT 0,
                star_max INTEGER NOT NULL DEFAULT 10,
                gift_drop_prob REAL NOT NULL DEFAULT 0.0,
                gift_pool_size INTEGER NOT NULL DEFAULT 1,
                gift_category TEXT NOT NULL DEFAULT 'gift',
                once_per_day INTEGER NOT NULL DEFAULT 0,
                desc_text TEXT NOT NULL DEFAULT '',
                expires_at TEXT NOT NULL DEFAULT '',
                is_active INTEGER NOT NULL DEFAULT 1,
                used_count INTEGER NOT NULL DEFAULT 0,
                shop_price_stars INTEGER NOT NULL DEFAULT 0,
                shop_name TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS promo_redemptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                promo_id INTEGER NOT NULL,
                telegram_id INTEGER NOT NULL,
                redeem_count INTEGER NOT NULL DEFAULT 0,
                last_redeemed_at TEXT NOT NULL DEFAULT '',
                UNIQUE(promo_id, telegram_id)
            )
        """)

        # Eski DB bo'lsa, yangi ustunlarni qo'shamiz (migratsiya)
        for alter_sql in (
            "ALTER TABLE users ADD COLUMN last_daily_box TEXT DEFAULT ''",
            "ALTER TABLE users ADD COLUMN last_free_ticket TEXT DEFAULT ''",
            "ALTER TABLE settings ADD COLUMN min_withdraw_stars INTEGER NOT NULL DEFAULT 100",
            "ALTER TABLE shop_items ADD COLUMN price_uzs INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE settings ADD COLUMN pay_card TEXT NOT NULL DEFAULT '9860180104681937'",
            "ALTER TABLE shop_items ADD COLUMN deliver_stars INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE settings ADD COLUMN reviews_channel TEXT DEFAULT ''",
            "ALTER TABLE settings ADD COLUMN nft_group TEXT DEFAULT ''",
            "ALTER TABLE boxes ADD COLUMN cost_tgstars INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE boxes ADD COLUMN tgstars_bonus_percent REAL NOT NULL DEFAULT 0",
            "ALTER TABLE shop_items ADD COLUMN tg_gift_id TEXT DEFAULT ''",
            "ALTER TABLE settings ADD COLUMN gift_caption TEXT DEFAULT ''",
            "ALTER TABLE promo_codes ADD COLUMN name TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE promo_codes ADD COLUMN star_min INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE promo_codes ADD COLUMN star_max INTEGER NOT NULL DEFAULT 10",
            "ALTER TABLE promo_codes ADD COLUMN gift_drop_prob REAL NOT NULL DEFAULT 0.0",
            "ALTER TABLE promo_codes ADD COLUMN gift_pool_size INTEGER NOT NULL DEFAULT 1",
            "ALTER TABLE promo_codes ADD COLUMN gift_category TEXT NOT NULL DEFAULT 'gift'",
            "ALTER TABLE promo_codes ADD COLUMN once_per_day INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE promo_codes ADD COLUMN desc_text TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE promo_codes ADD COLUMN shop_price_stars INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE promo_codes ADD COLUMN shop_name TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE promo_redemptions ADD COLUMN redeem_count INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE promo_redemptions ADD COLUMN last_redeemed_at TEXT NOT NULL DEFAULT ''",
        ):
            try:
                await db.execute(alter_sql)
            except aiosqlite.OperationalError:
                pass  # ustun allaqachon mavjud

        # Default sozlamalar (faqat birinchi marta)
        await db.execute("""
            INSERT OR IGNORE INTO settings (id, ref_reward_stars, min_referals_required,
                                            min_withdraw_stars, pay_card)
            VALUES (1, 5, 3, 100, '9860180104681937')
        """)

        # Default boxlar (faqat birinchi marta)
        await db.execute("""
            INSERT OR IGNORE INTO boxes (box_id, name, cost, star_min, star_max,
                                         gift_drop_prob, gift_pool_size, gift_category,
                                         once_per_day, desc_text)
            VALUES
                ('daily', '📦 Kunlik box', 1, 0, 10, 0.0, 1, 'gift', 1, 'Mukofot: 0–10 ⭐'),
                ('gift', '🎁 Gift box', 50, 10, 60, 0.5, 2, 'gift', 0, 'Mukofot: 10–60 ⭐ yoki arzonroq 2 giftdan biri'),
                ('nft', '🖼 NFT box', 100, 60, 120, 0.5, 4, 'gift', 0, 'Mukofot: 60–120 ⭐ yoki arzonroq 4 giftdan biri'),
                ('mega', '💎 Mega box', 200, 120, 250, 0.5, 4, 'premium', 0, 'Mukofot: 120–250 ⭐ yoki premium')
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
            "SELECT ref_reward_stars, min_referals_required, min_withdraw_stars, pay_card, reviews_channel, nft_group, gift_caption FROM settings WHERE id = 1"
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


async def set_user_balance(telegram_id: int, amount: int) -> None:
    """Foydalanuvchining ichki (virtual) ⭐ balansini aniq qiymatga o'rnatadi —
    admin nazorati uchun (masalan xohlagan payt 0 ga tushirish)."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET balance_stars = ? WHERE telegram_id = ?", (amount, telegram_id))
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


async def update_shop_item(item_id: int, **kwargs) -> None:
    """Mahsulotning istalgan ustunini yangilaydi (masalan tg_gift_id)."""
    keys = ", ".join(f"{k} = ?" for k in kwargs)
    vals = list(kwargs.values()) + [item_id]
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(f"UPDATE shop_items SET {keys} WHERE id = ?", vals)
        await db.commit()


# ---------- Boxlar (Jekpot) ----------

async def get_all_boxes() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM boxes ORDER BY id")
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def get_box(box_id: str) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM boxes WHERE box_id = ?", (box_id,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def update_box(box_id: str, **kwargs) -> None:
    keys = ", ".join(f"{k} = ?" for k in kwargs)
    vals = list(kwargs.values())
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(f"UPDATE boxes SET {keys} WHERE box_id = ?", vals + [box_id])
        await db.commit()


async def set_daily_box_used(telegram_id: int, date_str: str) -> None:
    """Foydalanuvchining kunlik box ochgan sanasini saqlaydi."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET last_daily_box = ? WHERE telegram_id = ?", (date_str, telegram_id))
        await db.commit()


async def pick_shop_gifts(category: str, pool_size: int) -> list[dict]:
    """Do'kondan eng arzon `pool_size` tasidan bittasini tanlaydi."""
    items = sorted(
        (i for i in await get_all_shop_items() if i["category"] == category and i["price_stars"] > 0),
        key=lambda x: x["price_stars"],
    )
    if not items:
        return []
    if pool_size < 1:
        pool_size = 1
    pool = items[:pool_size]  # eng arzon N tasi
    return [random.choice(pool)]


# ---------- Promokodlar ----------
# Har bir promokod — aslida ALOHIDA "box" (o'ziga xos star diapazoni, gift
# foizi, gift toifasi va h.k. bilan), faqat pul/⭐ evaziga emas, admin bergan
# kod evaziga ochiladi. Shu sababli sozlamalari ham oddiy boxlarniki bilan
# bir xil (pastdagi admin bo'limiga qarang).

async def get_all_promo_codes() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM promo_codes ORDER BY id DESC")
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def get_promo_by_code(code: str) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM promo_codes WHERE code = ?", (code,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def get_promo_by_id(promo_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM promo_codes WHERE id = ?", (promo_id,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def get_purchasable_promo_codes() -> list[dict]:
    """Do'konda sotiladigan promokodlar — faqat admin ATAYLAB narx VA do'kon
    nomini qo'ygan, faol va muddati o'tmagan promokodlar chiqadi. Haqiqiy
    `code` maydoni bu ro'yxatda HECH QACHON ishlatilmasligi kerak — aks holda
    foydalanuvchi pullik promoni bepul kod sifatida kiritib yuborishi mumkin."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM promo_codes WHERE shop_price_stars > 0 AND shop_name != '' AND is_active = 1 ORDER BY id DESC",
        )
        rows = [dict(r) for r in await cur.fetchall()]

    now = datetime.now()
    result = []
    for p in rows:
        if p["expires_at"]:
            try:
                expires = datetime.strptime(p["expires_at"], "%Y-%m-%d %H:%M:%S")
            except ValueError:
                expires = None
            if expires and now > expires:
                continue
        result.append(p)
    return result


async def create_promo_code(code: str, name: str) -> int:
    """Yangi promokodni standart (oddiy box'dagidek) sozlamalar bilan
    yaratadi — keyin admin uni xuddi box kabi (star diapazoni, gift foizi,
    muddati va h.k.) moslashtiradi."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO promo_codes (code, name, created_at) VALUES (?, ?, ?)",
            (code, name, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        )
        await db.commit()
        return cur.lastrowid


async def update_promo_code(promo_id: int, **kwargs) -> None:
    keys = ", ".join(f"{k} = ?" for k in kwargs)
    vals = list(kwargs.values())
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(f"UPDATE promo_codes SET {keys} WHERE id = ?", vals + [promo_id])
        await db.commit()


async def set_promo_active(promo_id: int, active: bool) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE promo_codes SET is_active = ? WHERE id = ?", (1 if active else 0, promo_id))
        await db.commit()


async def delete_promo_code(promo_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM promo_codes WHERE id = ?", (promo_id,))
        await db.execute("DELETE FROM promo_redemptions WHERE promo_id = ?", (promo_id,))
        await db.commit()


async def get_promo_redemption(promo_id: int, telegram_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM promo_redemptions WHERE promo_id = ? AND telegram_id = ?",
            (promo_id, telegram_id),
        )
        row = await cur.fetchone()
        return dict(row) if row else None


async def upsert_promo_redemption(promo_id: int, telegram_id: int, when: str) -> None:
    """Foydalanuvchi shu promokodni ishlatganini belgilaydi (yoki, kunlik
    qayta ishlatishga ruxsat berilgan bo'lsa, sanasini yangilaydi)."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO promo_redemptions (promo_id, telegram_id, redeem_count, last_redeemed_at) "
            "VALUES (?, ?, 1, ?) "
            "ON CONFLICT(promo_id, telegram_id) DO UPDATE SET "
            "redeem_count = redeem_count + 1, last_redeemed_at = excluded.last_redeemed_at",
            (promo_id, telegram_id, when),
        )
        await db.execute("UPDATE promo_codes SET used_count = used_count + 1 WHERE id = ?", (promo_id,))
        await db.commit()


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


# ---------- Yulduz/gift yechish so'rovlari (withdrawals) ----------
# Bular real pul/gift chiqimi bo'lgani uchun holati kuzatiladi — shunda admin
# "to'ladimmi yoki yo'qmi" deb adashib, bir so'rovni ikki marta to'lab
# yubormaydi (status: pending -> paid / rejected).

async def add_withdrawal(telegram_id: int, user_name: str, username: str, kind: str,
                          amount_stars: int, item_name: str = "") -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO withdrawals (telegram_id, user_name, username, kind, amount_stars, item_name, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (telegram_id, user_name, username, kind, amount_stars, item_name,
             datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        )
        await db.commit()
        return cur.lastrowid


async def get_withdrawal(withdrawal_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM withdrawals WHERE id = ?", (withdrawal_id,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def get_pending_withdrawals() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM withdrawals WHERE status = 'pending' ORDER BY id")
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def update_withdrawal_status(withdrawal_id: int, status: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE withdrawals SET status = ?, resolved_at = ? WHERE id = ?",
            (status, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), withdrawal_id),
        )
        await db.commit()


# ---------- Box'dan yutilgan giftlar — "saqlab qo'yish" (tanlov keyinroq) ----------
# Box'dan gift chiqqanda darhol yuborilmaydi — foydalanuvchi tanlaguncha
# "pending" holatida saqlanadi: keyinroq "🎁 Giftni olish" (haqiqiy sovg'a,
# botning haqiqiy Stars balansidan) yoki "⭐ Starsga aylantirish" (ichki
# bot valyutasiga aylantirish) tugmalaridan birini bosishi mumkin.

async def add_gift_claim(telegram_id: int, item_id: int, item_name: str, price_stars: int, source: str = "box") -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO gift_claims (telegram_id, item_id, item_name, price_stars, source, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (telegram_id, item_id, item_name, price_stars, source, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        )
        await db.commit()
        return cur.lastrowid


async def get_gift_claim(claim_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM gift_claims WHERE id = ?", (claim_id,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def update_gift_claim_status(claim_id: int, status: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE gift_claims SET status = ?, resolved_at = ? WHERE id = ?",
            (status, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), claim_id),
        )
        await db.commit()


# ---------- Bitta mahsulotga bog'langan bir nechta haqiqiy gift turi ----------
# (masalan "🐻 Ayiqcha / 🧸 Panda" — foydalanuvchi sotib olganda yoki yutib
# olganda aynan qaysi birini xohlashini o'zi tanlaydi.)

async def add_gift_variant(item_id: int, tg_gift_id: str, label: str = "") -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO gift_variants (item_id, tg_gift_id, label, created_at) VALUES (?, ?, ?, ?)",
            (item_id, tg_gift_id, label, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        )
        await db.commit()
        return cur.lastrowid


async def get_gift_variants(item_id: int) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM gift_variants WHERE item_id = ? ORDER BY id", (item_id,))
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def get_gift_variant(variant_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM gift_variants WHERE id = ?", (variant_id,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def clear_gift_variants(item_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM gift_variants WHERE item_id = ?", (item_id,))
        await db.commit()


# ============================================================
#  YORDAMCHI FUNKSIYALAR
# ============================================================

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def normalize_channel_id(raw: str) -> str:
    """Admin kiritgan kanal ID/username ni Telegram Bot API tushunadigan
    formatga keltiradi. Quyidagilarni qo'llab-quvvatlaydi:
      -1001234567890            -> -1001234567890 (o'zgarmaydi)
      @mychannel                -> @mychannel (o'zgarmaydi)
      mychannel                 -> @mychannel ("@" qo'shiladi)
      https://t.me/mychannel    -> @mychannel
      t.me/mychannel            -> @mychannel
    """
    value = (raw or "").strip()
    for prefix in ("https://t.me/", "http://t.me/", "t.me/", "@"):
        if value.lower().startswith(prefix):
            value = value[len(prefix):]
            break
    value = value.strip().strip("/")
    if not value:
        return raw
    if value.lstrip("-").isdigit():
        # Raqamli chat ID (masalan -1001234567890)
        return value
    # Ochiq kanal username'i — "@" bilan boshlanishi shart
    return f"@{value}"


# Konfiguratsiya xatosi haqida adminlarga faqat bir marta xabar berish uchun
_notified_bad_channels: set[str] = set()


async def check_subscriptions(bot: Bot, telegram_id: int, channels: list[dict]) -> list[dict]:
    """Foydalanuvchi majburiy kanallarga a'zo ekanligini tekshiradi.
    A'zo bo'lmagan kanallar ro'yxatini qaytaradi.

    Adminlar bu tekshiruvdan mustasno — ular botni sinash/boshqarish uchun
    kanallarga a'zo bo'lishlari shart emas."""
    if is_admin(telegram_id):
        return []
    not_subscribed = []
    for ch in channels:
        chat_id = normalize_channel_id(ch["channel_id"])
        try:
            member = await bot.get_chat_member(chat_id, telegram_id)
            if member.status not in (
                ChatMemberStatus.MEMBER,
                ChatMemberStatus.ADMINISTRATOR,
                ChatMemberStatus.CREATOR,
                ChatMemberStatus.RESTRICTED,
            ):
                not_subscribed.append(ch)
        except (TelegramBadRequest, TelegramForbiddenError) as e:
            # Odatda bu xato ikki sababdan bo'ladi:
            #  1) Bot kanalda admin emas (getChatMember chaqira olmaydi)
            #  2) channel_id noto'g'ri kiritilgan (masalan "@" yo'q yoki xato ID)
            # Bunday holatda foydalanuvchini "obuna emas" deb belgilashning o'zi
            # xato bo'lishi mumkin — shuning uchun bu holatni aniq logga yozamiz
            # va adminlarga bir martalik ogohlantirish yuboramiz, lekin baribir
            # xavfsizlik uchun kanalni "obuna bo'linmagan" deb hisoblaymiz.
            logger.error(
                "Kanal tekshiruvi muvaffaqiyatsiz (channel_id=%r -> %r), telegram_id=%s: %s. "
                "Ehtimol bot kanalda admin emas yoki channel_id noto'g'ri.",
                ch["channel_id"], chat_id, telegram_id, e,
            )
            if chat_id not in _notified_bad_channels:
                _notified_bad_channels.add(chat_id)
                warning_text = (
                    f"⚠️ <b>Majburiy kanal tekshiruvida xatolik!</b>\n\n"
                    f"Kanal: <code>{html.escape(str(ch['channel_id']))}</code>\n"
                    f"Xato: <code>{safe_error_text(e)}</code>\n\n"
                    f"Sabablari:\n"
                    f"• Bot shu kanalda <b>admin</b> emas\n"
                    f"• Kanal ID/username noto'g'ri kiritilgan\n\n"
                    f"Botni kanalga admin qilib qo'shing yoki kanal ID sini "
                    f"admin panelda qaytadan tekshiring."
                )
                for admin_id in ADMIN_IDS:
                    try:
                        await bot.send_message(admin_id, warning_text)
                    except Exception:
                        pass
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
    rows = [
        [KeyboardButton(text="👤 Profil"), KeyboardButton(text="🛍️ Do'kon")],
        [KeyboardButton(text="🎰 Jekpot"), KeyboardButton(text="📞 Aloqa")],
        [KeyboardButton(text="⭐ Otziv"), KeyboardButton(text="💸 Yulduz yechish")],
        [KeyboardButton(text="🔗 Referal"), KeyboardButton(text="ℹ️ Bot haqida")],
    ]
    # Mini App tugmasi faqat PUBLIC_BASE_URL (ochiq HTTPS manzil) sozlangan
    # bo'lsa ko'rsatiladi — Telegram web_app tugmasi HTTPS talab qiladi.
    if PUBLIC_BASE_URL.startswith("https://"):
        rows.append([KeyboardButton(text="✨ Mini-App do'kon", web_app=WebAppInfo(url=f"{PUBLIC_BASE_URL}/webapp"))])
    kb = ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)
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
    """Mahsulot uchun to'lov tugmalari (toifaga qarab ⭐ bot balansi, ✨ haqiqiy
    Telegram Stars va/yo UZS)."""
    kb = InlineKeyboardBuilder()
    if item["category"] == "star":
        # Yulduzlar faqat UZS bilan sotib olinadi
        if item["price_uzs"] > 0:
            kb.button(text=f"💳 UZSda sotib olish ({item['price_uzs']:,} so'm)", callback_data=f"buy_uzs:{item['id']}")
    else:
        # Gift va Premium: bot balansi (⭐), haqiqiy Telegram Stars (✨) va UZS ishlaydi
        if item["price_stars"] > 0:
            kb.button(text=f"⭐ Bot balansidan ({item['price_stars']} ⭐)", callback_data=f"buy:{item['id']}")
            kb.button(text=f"✨ Telegram Stars bilan ({item['price_stars']} ⭐)", callback_data=f"buy_tgstars:{item['id']}")
        if item["price_uzs"] > 0:
            kb.button(text=f"💳 UZSda ({item['price_uzs']:,} so'm)", callback_data=f"buy_uzs:{item['id']}")
    kb.button(text="🔙 Ortga", callback_data="shop")
    kb.adjust(1)
    return kb.as_markup()


def shop_categories_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for key, label in CATEGORIES.items():
        kb.button(text=label, callback_data=f"shop:{key}")
    kb.button(text="🔑 Promokodni kiritish", callback_data="promo_redeem_start")
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
        "👋 <b>Xush kelibsiz!</b>\n\n"
        "Bu yerda yulduzlar (⭐) yig'ib, do'kondan sovg'alar olasiz va jekpotda qatnashasiz!\n"
        "Taklif qilgan har bir do'stingiz uchun bonus oling. 🎁",
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
    """Yulduzlar yechiladi: balansdan ayriladi, admin uchun Tasdiqlash/Rad etish
    tugmali so'rov yaratiladi (holati kuzatiladi — ikki marta to'lanib ketmasin)."""
    user = await get_user(call.from_user.id)
    if not user:
        await call.answer("❌ Xatolik", show_alert=True)
        return

    if user["balance_stars"] < amount:
        await call.answer("❌ Balans yetarli emas!", show_alert=True)
        return

    await deduct_stars(call.from_user.id, amount)
    w_id = await add_withdrawal(
        telegram_id=call.from_user.id,
        user_name=call.from_user.first_name or "",
        username=call.from_user.username or "",
        kind="stars",
        amount_stars=amount,
    )

    kb = InlineKeyboardBuilder()
    kb.button(text="✅ To'lov qildim", callback_data=f"wd_approve:{w_id}")
    kb.button(text="❌ Bekor qilish (qaytarish)", callback_data=f"wd_reject:{w_id}")
    kb.adjust(1)

    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(
                admin_id,
                f"💸 <b>YULDUZ YECHISH SO'ROVI #{w_id}</b>\n\n"
                f"👤 Foydalanuvchi: {call.from_user.first_name} (@{call.from_user.username or '—'})\n"
                f"🆔 ID: <code>{call.from_user.id}</code>\n"
                f"💰 Miqdor: <b>{amount} ⭐</b>\n"
                f"📅 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                f"⚠️ Foydalanuvchiga real to'lovni o'tkazgach, <b>\"✅ To'lov qildim\"</b> tugmasini bosing — "
                f"shunda bu so'rov \"to'langan\" deb belgilanadi va qayta-qayta to'lab yubormaysiz.",
                reply_markup=kb.as_markup(),
            )
        except TelegramForbiddenError:
            pass

    try:
        await call.message.edit_text(
            f"✅ <b>So'rovingiz qabul qilindi!</b>\n\n"
            f"💰 Miqdor: <b>{amount} ⭐</b>\n"
            f"🧾 So'rov: #{w_id}\n\n"
            f"📤 Yulduzlar bot egasi tomonidan hisobingizga o'tkaziladi.\n"
            f"👑 Ega: <b>@Kottabolladan</b>",
        )
    except TelegramBadRequest:
        await call.message.answer(
            f"✅ <b>So'rovingiz qabul qilindi!</b>\n\n"
            f"💰 Miqdor: <b>{amount} ⭐</b>\n"
            f"🧾 So'rov: #{w_id}\n\n"
            f"📤 Yulduzlar bot egasi tomonidan hisobingizga o'tkaziladi.\n"
            f"👑 Ega: <b>@Kottabolladan</b>",
        )
    await call.answer("✅ Yuborildi!", show_alert=False)


@router.callback_query(F.data.startswith("wd_approve:"))
async def withdrawal_approve_callback(call: CallbackQuery, bot: Bot) -> None:
    """Admin real to'lovni amalga oshirgach shu tugmani bosadi — so'rov 'to'langan'
    deb belgilanadi, shu bilan qayta-qayta to'lab yuborish oldi olinadi."""
    if not is_admin(call.from_user.id):
        await call.answer("❌ Siz admin emassiz!", show_alert=True)
        return

    w_id = int(call.data.split(":")[1])
    w = await get_withdrawal(w_id)
    if not w:
        await call.answer("❌ So'rov topilmadi", show_alert=True)
        return
    if w["status"] != "pending":
        await call.answer(f"⚠️ Bu so'rov allaqachon '{w['status']}' deb belgilangan!", show_alert=True)
        return

    await update_withdrawal_status(w_id, "paid")

    try:
        await bot.send_message(
            w["telegram_id"],
            f"✅ <b>To'lovingiz amalga oshirildi!</b>\n\n"
            f"🧾 So'rov: #{w_id}\n"
            f"💰 Miqdor: <b>{w['amount_stars']} ⭐</b>",
        )
    except TelegramForbiddenError:
        pass

    try:
        await call.message.edit_text(
            f"{call.message.text}\n\n✅ <b>TO'LANDI</b> — {call.from_user.first_name} tomonidan",
        )
    except TelegramBadRequest:
        pass
    await call.answer("✅ To'langan deb belgilandi!", show_alert=False)


@router.callback_query(F.data.startswith("wd_reject:"))
async def withdrawal_reject_callback(call: CallbackQuery, bot: Bot) -> None:
    """Admin to'lovni bekor qilsa — yulduzlar foydalanuvchi balansiga qaytariladi."""
    if not is_admin(call.from_user.id):
        await call.answer("❌ Siz admin emassiz!", show_alert=True)
        return

    w_id = int(call.data.split(":")[1])
    w = await get_withdrawal(w_id)
    if not w:
        await call.answer("❌ So'rov topilmadi", show_alert=True)
        return
    if w["status"] != "pending":
        await call.answer(f"⚠️ Bu so'rov allaqachon '{w['status']}' deb belgilangan!", show_alert=True)
        return

    await update_withdrawal_status(w_id, "rejected")
    # "stars" va "gift" — ikkalasida ham so'rov paytida balansdan yulduz
    # ayrilgan edi (gift'da item narxi hisobida) — shuning uchun ikkalasida
    # ham qaytaramiz.
    await add_stars(w["telegram_id"], w["amount_stars"])

    try:
        await bot.send_message(
            w["telegram_id"],
            f"❌ <b>Yechish so'rovingiz bekor qilindi!</b>\n\n"
            f"🧾 So'rov: #{w_id}\n"
            f"💰 <b>{w['amount_stars']} ⭐</b> balansingizga qaytarildi.\n\n"
            f"Muammo bo'lsa, bot egasi bilan bog'laning: @Kottabolladan",
        )
    except TelegramForbiddenError:
        pass

    try:
        await call.message.edit_text(
            f"{call.message.text}\n\n❌ <b>BEKOR QILINDI (qaytarildi)</b> — {call.from_user.first_name} tomonidan",
        )
    except TelegramBadRequest:
        pass
    await call.answer("❌ Bekor qilindi, balans qaytarildi!", show_alert=False)


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
    """Gift yechib olish. Agar mahsulotga haqiqiy Telegram gift_id bog'langan
    bo'lsa (admin panel → 🎁 TG Gift avto-yuborish), bot uni o'zining haqiqiy
    Stars balansidan DARHOL avtomatik yuboradi — admin qo'lda bosishi shart
    emas. Bog'lanmagan yoki avto-yuborish muvaffaqiyatsiz bo'lsa, eski
    qo'lda-tasdiqlash yo'liga qaytiladi (yulduz hech qachon yo'qolmaydi)."""
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
    w_id = await add_withdrawal(
        telegram_id=call.from_user.id,
        user_name=call.from_user.first_name or "",
        username=call.from_user.username or "",
        kind="gift",
        amount_stars=item["price_stars"],
        item_name=item["name"],
    )

    # ---- Avtomatik yuborishga urinish (agar tg_gift_id bog'langan bo'lsa) ----
    auto_sent = False
    auto_error = None
    if item["tg_gift_id"]:
        try:
            await bot.send_gift(
                user_id=call.from_user.id,
                gift_id=item["tg_gift_id"],
                text=f"🎁 {item['name']} — Stars Bot'dan sovg'a!",
            )
            auto_sent = True
        except Exception as e:
            auto_error = str(e)
            logger.error("send_gift avtomatik yuborilmadi (item=%s, w_id=%s): %s", item["name"], w_id, e)

    if auto_sent:
        await update_withdrawal_status(w_id, "paid")
        for admin_id in ADMIN_IDS:
            try:
                await bot.send_message(
                    admin_id,
                    f"🎁 <b>GIFT AVTOMATIK YUBORILDI!</b> (so'rov #{w_id})\n\n"
                    f"👤 Foydalanuvchi: {call.from_user.first_name} (@{call.from_user.username or '—'})\n"
                    f"🆔 ID: <code>{call.from_user.id}</code>\n"
                    f"🎁 Gift: <b>{item['name']}</b>\n"
                    f"💰 Narxi: <b>{item['price_stars']} ⭐</b> (bot balansidan)\n"
                    f"📅 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                    f"✅ Hech narsa qilish shart emas — allaqachon yuborilgan.",
                )
            except TelegramForbiddenError:
                pass

        result_text = (
            f"🎉 <b>Gift avtomatik yuborildi!</b>\n\n"
            f"🎁 <b>{item['name']}</b>\n"
            f"💰 {item['price_stars']} ⭐ ayirildi.\n"
            f"🧾 So'rov: #{w_id}\n\n"
            f"Telegram'dagi \"Sovg'alar\" bo'limingizni tekshiring! ✨"
        )
    else:
        # Qo'lda tasdiqlash yo'li (tg_gift_id yo'q yoki avto-yuborish xato berdi)
        kb = InlineKeyboardBuilder()
        kb.button(text="✅ Gift yubordim", callback_data=f"wd_approve:{w_id}")
        kb.button(text="❌ Bekor qilish (qaytarish)", callback_data=f"wd_reject:{w_id}")
        kb.adjust(1)

        warn = (
            f"⚠️ Avtomatik yuborish muvaffaqiyatsiz bo'ldi ({auto_error}) — qo'lda yuboring!\n\n"
            if auto_error else ""
        )
        for admin_id in ADMIN_IDS:
            try:
                await bot.send_message(
                    admin_id,
                    f"🎁 <b>GIFT YECHISH SO'ROVI #{w_id}</b>\n\n"
                    f"{warn}"
                    f"👤 Foydalanuvchi: {call.from_user.first_name} (@{call.from_user.username or '—'})\n"
                    f"🆔 ID: <code>{call.from_user.id}</code>\n"
                    f"🎁 Gift: <b>{item['name']}</b>\n"
                    f"💰 Narxi: <b>{item['price_stars']} ⭐</b>\n"
                    f"📅 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                    f"⚠️ Giftni foydalanuvchiga Telegram'da yuborgach, <b>\"✅ Gift yubordim\"</b> tugmasini bosing — "
                    f"shunda bu so'rov \"bajarildi\" deb belgilanadi va qayta-qayta yuborib yubormaysiz.",
                    reply_markup=kb.as_markup(),
                )
            except TelegramForbiddenError:
                pass

        result_text = (
            f"✅ <b>So'rovingiz qabul qilindi!</b>\n\n"
            f"🎁 <b>{item['name']}</b>\n"
            f"💰 {item['price_stars']} ⭐ ayirildi.\n"
            f"🧾 So'rov: #{w_id}\n\n"
            f"Gift sizga Telegram'da yuboriladi. Egasi: @Kottabolladan"
        )

    try:
        await call.message.edit_text(result_text)
    except TelegramBadRequest:
        await call.message.answer(result_text)
    await call.answer("✅ Yuborildi!", show_alert=False)


@router.message(F.text == "🛍️ Do'kon")
async def shop_handler(message: Message) -> None:
    await message.answer(
        "🛍️ <b>Do'kon</b>\n\nKategoriyani tanlang:",
        reply_markup=shop_categories_keyboard(),
    )


# ============================================================
#  BOXLAR (JEKPOT) — lootbox tizimi
# ============================================================
# Boxlar sozlamalari DB'da (boxes jadvali) saqlanadi, admin o'zgartiradi.
# Har bir box uchun: narx, star diapazoni, gift tushish foizi,
# eng arzon N ta giftdan biri, gift toifasi, kunlik cheklov, tavsif.


async def roll_box(box: dict, via_tgstars: bool = False) -> dict:
    """Box ochish natijasini hisoblaydi. {'kind': 'stars'|'gifts', 'amount', 'gifts'}

    via_tgstars=True bo'lsa (box haqiqiy Telegram Stars bilan to'langan) —
    gift tushish ehtimoli box['tgstars_bonus_percent'] foiz punktiga
    oshiriladi. Shu orqali foydalanuvchilar ichki balans o'rniga haqiqiy
    Stars bilan to'lashga rag'batlantiriladi (yutish imkoniyati kattaroq)."""
    gift_drop_prob = box["gift_drop_prob"]
    if via_tgstars:
        bonus = box.get("tgstars_bonus_percent") or 0
        gift_drop_prob = min(1.0, gift_drop_prob + bonus / 100.0)

    if gift_drop_prob > 0 and random.random() < gift_drop_prob:
        gifts = await pick_shop_gifts(box["gift_category"], box["gift_pool_size"])
        if gifts:
            return {"kind": "gifts", "amount": 0, "gifts": gifts}

    lo, hi = box["star_min"], box["star_max"]
    if hi < lo:
        hi = lo
    return {"kind": "stars", "amount": random.randint(lo, hi), "gifts": []}


@router.message(F.text == "🎰 Jekpot")
async def jackpot_handler(message: Message) -> None:
    await show_boxes(message.answer, message.from_user.id)


async def show_boxes(answer_func, telegram_id: int, result_text: str | None = None) -> None:
    """Boxlar menyusini ko'rsatadi."""
    user = await get_user(telegram_id)
    today = datetime.now().strftime("%Y-%m-%d")
    boxes = await get_all_boxes()

    kb = InlineKeyboardBuilder()
    for b in boxes:
        kb.button(text=f"{b['name']} — {b['cost']} ⭐ (balans)", callback_data=f"box_open:{b['box_id']}")
        if b["cost_tgstars"] > 0:
            kb.button(text=f"{b['name']} — {b['cost_tgstars']} 💫 (Telegram Stars)", callback_data=f"box_open_tgstars:{b['box_id']}")
    kb.button(text="🎟️ Promokod box", callback_data="promo_redeem_start")
    kb.button(text="🔙 Bosh menyu", callback_data="main_menu")
    kb.adjust(1)

    text = "🎰 <b>BOXLAR</b>\n\nQaysi boxni ochasiz?\n\n"
    for b in boxes:
        price_line = f"{b['cost']} ⭐ (balans)"
        if b["cost_tgstars"] > 0:
            price_line += f" / {b['cost_tgstars']} 💫 (Telegram Stars)"
        line = f"{b['name']} — <b>{price_line}</b>\n{b['desc_text']}"
        if b["once_per_day"]:
            if user and user["last_daily_box"] == today:
                line = f"✅ {line}\n(Bugun ishlatilgan — ertaga qayta ochiladi)"
            else:
                line = f"{line}\n(Kuniga 1 marta)"
        text += f"{line}\n\n"

    text += "🎟️ <b>Promokod box</b> — admin bergan promokodni kiriting, u ham oddiy box kabi ⭐ yoki gift beradi, lekin bepul!\n\n"

    if user:
        text += f"💰 Balansingiz: <b>{user['balance_stars']} ⭐</b>"

    if result_text:
        text = f"{result_text}\n\n────────────\n\n{text}"

    await answer_func(text, reply_markup=kb.as_markup())


@router.callback_query(F.data == "promo_redeem_start")
async def promo_redeem_start(call: CallbackQuery, state: FSMContext) -> None:
    """Foydalanuvchi 'Promokod box' tugmasini bosganda kodni so'raymiz."""
    user = await get_user(call.from_user.id)
    if not user:
        await call.answer("❌ Avval /start ni bosing!", show_alert=True)
        return
    await state.set_state(PromoRedeemStates.code)
    kb = InlineKeyboardBuilder()
    kb.button(text="🔙 Bekor qilish", callback_data="promo_redeem_cancel")
    kb.adjust(1)
    await call.message.edit_text(
        "🎟️ <b>Promokod box</b>\n\nAdmin bergan promokodni yozib yuboring:",
        reply_markup=kb.as_markup(),
    )
    await call.answer()


@router.callback_query(F.data == "promo_redeem_cancel")
async def promo_redeem_cancel(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.answer()
    try:
        await call.message.edit_text("❌ Bekor qilindi.")
    except TelegramBadRequest:
        pass


@router.message(PromoRedeemStates.code)
async def promo_redeem_input(message: Message, state: FSMContext, bot: Bot) -> None:
    await state.clear()
    result = await redeem_promo_code(
        bot, message.from_user.id, message.from_user.first_name, message.from_user.username, message.text or "",
    )
    if not result["ok"]:
        await message.answer(result["error"])
        return
    if result["claim_id"]:
        await message.answer(result["text"], reply_markup=gift_claim_keyboard(result["claim_id"], result["gift_price_stars"]))
    else:
        await message.answer(result["text"])


async def try_auto_deliver_gift(
    bot: Bot,
    telegram_id: int,
    item: dict | None,
    gift_id_override: str | None = None,
) -> tuple[bool, str]:
    """Do'kondan sotib olingan yoki box'dan yutilgan "gift" mahsulotini
    FOYDALANUVCHIGA avtomatik yetkazishga urinadi — ega/admin qo'lda hech
    narsa qilmaydi.

    Faqat "gift" toifasidagi va admin panelda haqiqiy Telegram gift ID'siga
    bog'langan (tg_gift_id) mahsulotlar uchun ishlaydi — bot o'zining
    haqiqiy Telegram Stars balansidan bot.send_gift() orqali yuboradi
    (Premium yoki bog'lanmagan/mahsus giftlar avtomatlashtirilmaydi, chunki
    Telegram Bot API buni qo'llab-quvvatlamaydi yoki qaysi real narsa
    ekanligi noma'lum).

    gift_id_override — mahsulotga BIR NECHTA gift turi bog'langanda
    (gift_variants), foydalanuvchi allaqachon aynan qaysi birini
    tanlaganidan keyin, o'sha aniq gift_id bilan yuborish uchun (deliver_gift
    va giftvariant: callback'lari shu orqali chaqiradi).

    Qaytaradi: (delivered, error) — delivered=True bo'lsa muvaffaqiyatli
    yuborilgan; delivered=False va error bo'sh bo'lsa avto-yuborish umuman
    urinilmagan (masalan Premium yoki gift_id bog'lanmagan); error to'la
    bo'lsa urinish xato bilan tugagan (masalan bot balansida Stars yetmadi)."""
    if not item or item.get("category") != "gift":
        return False, ""
    target_gift_id = gift_id_override or item.get("tg_gift_id")
    if not target_gift_id:
        return False, ""
    try:
        settings = await get_settings()
        caption_template = settings.get("gift_caption") or DEFAULT_GIFT_CAPTION
        caption = caption_template.replace("{item}", item["name"])[:255]
        await bot.send_gift(
            user_id=telegram_id,
            gift_id=target_gift_id,
            text=caption,
        )
        return True, ""
    except Exception as e:
        logger.error("Avtomatik gift yuborilmadi (item=%s, user=%s): %s", item.get("name"), telegram_id, e)
        # HTML-escape qilingan holda qaytariladi — chunki bu matn keyinchalik
        # to'g'ridan-to'g'ri Telegram HTML xabarlariga qo'shiladi (masalan
        # "<code>{error}</code>"). Escape qilinmasa, xato matni ichida "<...>"
        # bo'lsa (masalan ba'zi Python xatolarining ichki repr'i), Telegram
        # buni noto'g'ri HTML teg deb hisoblab, BUTUN xabarni rad etadi va
        # keyingi urinishlar ham xuddi shu tarzda "hech narsa chiqmasdan"
        # muvaffaqiyatsiz tugaydi.
        return False, safe_error_text(e)


def gift_variant_choice_keyboard(claim_id: int, variants: list[dict]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for v in variants:
        kb.button(text=v["label"] or "🎁", callback_data=f"giftvariant:{claim_id}:{v['id']}")
    kb.adjust(1)
    return kb.as_markup()


async def deliver_gift(
    bot: Bot,
    telegram_id: int,
    item: dict | None,
    *,
    price_stars: int | None = None,
    source: str = "purchase",
) -> dict:
    """Gift mahsulotni yetkazishning YAGONA kirish nuqtasi — do'kondan sotib
    olinganda HAM, box'dan yutilganda HAM shu orqali chaqiriladi.

    - Mahsulotga hech qanday haqiqiy gift bog'lanmagan bo'lsa: {"mode": "none"}
      (avto-yetkazish umuman qo'llanilmaydi, eski qo'lda tasdiqlash yo'li davom etadi)
    - Bitta gift turi bog'langan bo'lsa: darhol yuboriladi —
      {"mode": "delivered"} yoki {"mode": "failed", "error": ...}
    - IKKI YOKI KO'PROQ gift turi (gift_variants) bog'langan bo'lsa: darhol
      yubormaydi — foydalanuvchiga "qaysi birini xohlaysiz?" tanlov tugmalarini
      yuboradi, tanlov qilingach giftvariant: callback orqali yakunlanadi —
      {"mode": "choice", "claim_id": ...}
    """
    if not item or item.get("category") != "gift":
        return {"mode": "none"}

    variants = await get_gift_variants(item["id"])
    if len(variants) >= 2:
        claim_id = await add_gift_claim(
            telegram_id, item["id"], item["name"], price_stars or item.get("price_stars", 0), source=source,
        )
        try:
            await bot.send_message(
                telegram_id,
                f"🎁 <b>{item['name']}</b>\n\n"
                f"Bu mahsulotning bir nechta turi bor — aynan qaysi birini xohlaysiz?",
                reply_markup=gift_variant_choice_keyboard(claim_id, variants),
            )
        except TelegramForbiddenError:
            pass
        return {"mode": "choice", "claim_id": claim_id}

    if len(variants) == 1:
        ok, err = await try_auto_deliver_gift(bot, telegram_id, item, gift_id_override=variants[0]["tg_gift_id"])
    elif item.get("tg_gift_id"):
        ok, err = await try_auto_deliver_gift(bot, telegram_id, item)
    else:
        return {"mode": "none"}

    return {"mode": "delivered"} if ok else {"mode": "failed", "error": err}


def gift_delivery_texts(result: dict) -> tuple[str, str]:
    """deliver_gift() natijasidan admin xabariga qo'shiladigan qism va
    foydalanuvchiga ko'rsatiladigan qatorni tayyorlaydi — barcha xarid
    yo'llarida (balans/Stars/UZS) bir xil matn mantig'i ishlatiladi."""
    mode = result.get("mode")
    if mode == "delivered":
        return (
            "✅ Gift avtomatik yuborildi — hech narsa qilish shart emas.\n\n",
            "✅ Gift avtomatik yuborildi — Telegram'dagi \"Sovg'alar\" bo'limingizni tekshiring! ✨",
        )
    if mode == "choice":
        return (
            "🎯 Foydalanuvchi hozir qaysi gift turini xohlashini tanlamoqda — tanlagach avtomatik yuboriladi.\n\n",
            "🎁 Sizga gift turini tanlash uchun alohida xabar yubordik — shu yerdan tanlang!",
        )
    if mode == "failed":
        err = result.get("error", "")
        return (
            f"⚠️ Avtomatik yuborish muvaffaqiyatsiz bo'ldi ({err}) — qo'lda yuboring!\n\n",
            "Buyurtma adminga yuborildi, tez orada siz bilan bog'lanamiz. 🎁",
        )
    return "", "Buyurtma adminga yuborildi, tez orada siz bilan bog'lanamiz. 🎁"


async def open_box_and_award(
    bot: Bot, box: dict, telegram_id: int, first_name: str, username: str, via_tgstars: bool = False,
) -> dict:
    """Boxni yechadi (roll_box) va mukofotni beradi — yulduz bo'lsa balansga
    qo'shiladi, gift bo'lsa adminlarga xabar boradi. Bot balansi ORQALI ham,
    haqiqiy Telegram Stars ORQALI ham ochilgan boxlar uchun bir xil
    ishlatiladi.

    via_tgstars=True — box haqiqiy Telegram Stars bilan to'langan
    (box_open_tgstars_callback -> _handle_box_stars_payment): roll_box'ga
    uzatiladi, u yerda gift tushish ehtimoli box'ning tgstars_bonus_percent
    qiymati qadar oshiriladi.

    Natija sifatida dict qaytaradi:
      - text: Telegram HTML formatidagi natija matni
      - kind: "stars" yoki "gifts"
      - amount: yutilgan yulduzlar soni (gifts bo'lsa 0)
      - ratio: 0..1 oralig'ida "qanchalik katta yutuq" ko'rsatkichi — Mini
        App'dagi raketa animatsiyasi qay balandlikka uchishini shu belgilaydi
        (gift har doim eng baland/portlash, yulduz esa box'ning star_min..
        star_max oralig'idagi o'rniga qarab hisoblanadi)."""
    prize = await roll_box(box, via_tgstars=via_tgstars)

    if prize["kind"] == "stars":
        await add_stars(telegram_id, prize["amount"])
        result_text = (
            f"🎉 <b>{box['name']}</b> ochildi!\n\n"
            f"⭐ Mukofot: <b>+{prize['amount']} ⭐</b>\n\n"
            f"Yulduzlar hisobingizga qo'shildi!"
        )
        lo, hi = box["star_min"], box["star_max"]
        if hi > lo:
            raw_ratio = (prize["amount"] - lo) / (hi - lo)
        else:
            raw_ratio = 1.0
        raw_ratio = max(0.0, min(1.0, raw_ratio))
        # Yulduz mukofotlari doim gift'dan pastroq "balandlik"da qoladi (0.05..0.82)
        ratio = 0.05 + raw_ratio * 0.77
        amount = prize["amount"]
        claim_id = None
    else:
        gift = prize["gifts"][0]
        ratio = 0.97  # gift — eng katta yutuq, raketa deyarli tepaga uchadi
        amount = 0

        # Darhol yuborilmaydi — foydalanuvchi keyinroq tanlaydi: haqiqiy gift
        # sifatida olish yoki ⭐ (ichki valyuta) ga aylantirish. Shu tanlovga
        # qadar hech narsa sodir bo'lmaydi ("saqlab qo'yilgan" holat).
        claim_id = await add_gift_claim(telegram_id, gift["id"], gift["name"], gift["price_stars"], source="box")

        result_text = (
            f"🎉 <b>{box['name']}</b> ochildi!\n\n"
            f"🎁 Yutgan giftingiz: <b>{gift['name']}</b> ({gift['price_stars']} ⭐)\n\n"
            f"Pastdagi tugmalardan birini tanlang 👇"
        )

    return {
        "text": result_text,
        "kind": prize["kind"],
        "amount": amount,
        "ratio": round(ratio, 3),
        "claim_id": claim_id,
        "gift_name": prize["gifts"][0]["name"] if prize["kind"] == "gifts" else None,
        "gift_price_stars": prize["gifts"][0]["price_stars"] if prize["kind"] == "gifts" else None,
    }


async def _promo_eligibility_error(promo: dict, telegram_id: int) -> str | None:
    """Promokodni HOZIR ishlatish/sotib olish mumkinmi tekshiradi (faol,
    muddati, va bu foydalanuvchi allaqachon ishlatganmi) — mos kelmasa xato
    matnini, aks holda None qaytaradi. Kod bepul kiritilganda ham, do'kondan
    sotib olinganda ham bir xil ishlatiladi."""
    if not promo["is_active"]:
        return "⛔ Bu promokod faolsizlantirilgan!"
    if promo["expires_at"]:
        try:
            expires = datetime.strptime(promo["expires_at"], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            expires = None
        if expires and datetime.now() > expires:
            return "⏰ Bu promokodning muddati tugagan!"

    redemption = await get_promo_redemption(promo["id"], telegram_id)
    if redemption:
        if promo["once_per_day"]:
            today = datetime.now().strftime("%Y-%m-%d")
            if redemption["last_redeemed_at"][:10] == today:
                return "❌ Bu promokodni bugun ishlatgansiz! Ertaga qayta urinib ko'ring."
        else:
            return "⚠️ Siz bu promokodni allaqachon ishlatgansiz!"
    return None


async def _award_promo(bot: Bot, promo: dict, telegram_id: int, first_name: str, username: str) -> dict:
    """Promokodni ODDIY BOX sifatida ochadi (open_box_and_award orqali, xuddi
    box_open_callback'dagi kabi): yulduz yoki gift chiqishi mumkin. Chaqiruvchi
    tomonidan eligibility (_promo_eligibility_error) allaqachon tekshirilgan
    deb hisoblanadi."""
    await upsert_promo_redemption(promo["id"], telegram_id, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    box_like = {
        "name": promo["name"] or f"🎟️ {promo['code']}",
        "star_min": promo["star_min"],
        "star_max": promo["star_max"],
        "gift_drop_prob": promo["gift_drop_prob"],
        "gift_pool_size": promo["gift_pool_size"],
        "gift_category": promo["gift_category"],
    }
    result = await open_box_and_award(bot, box_like, telegram_id, first_name, username)
    return {"ok": True, **result}


async def redeem_promo_code(
    bot: Bot, telegram_id: int, first_name: str, username: str, raw_code: str,
) -> dict:
    """Foydalanuvchi qo'lda kiritgan (bepul) promokodni tekshiradi va amal
    qilsa ochadi. Qaytaradi: {"ok": False, "error": "..."} yoki
    {"ok": True, **open_box_and_award() natijasi}."""
    code = (raw_code or "").strip().upper()
    if not code:
        return {"ok": False, "error": "❌ Promokodni kiriting!"}

    promo = await get_promo_by_code(code)
    if not promo:
        return {"ok": False, "error": "❌ Bunday promokod topilmadi!"}

    error = await _promo_eligibility_error(promo, telegram_id)
    if error:
        return {"ok": False, "error": error}

    return await _award_promo(bot, promo, telegram_id, first_name, username)


async def buy_promo_from_shop(
    bot: Bot, telegram_id: int, first_name: str, username: str, promo_id: int,
) -> dict:
    """Do'kondan ⭐ balans evaziga promokod sotib olish — narx ayiriladi,
    keyin xuddi bepul kod kiritilgandagidek ochiladi (bir xil eligibility
    va bir xil mukofot mantig'i, faqat kod o'rniga to'lov orqali kirish)."""
    promo = await get_promo_by_id(promo_id)
    if not promo:
        return {"ok": False, "error": "❌ Bunday mahsulot topilmadi!"}
    if promo["shop_price_stars"] <= 0 or not promo["shop_name"]:
        return {"ok": False, "error": "❌ Bu promokod do'konda sotilmaydi!"}

    error = await _promo_eligibility_error(promo, telegram_id)
    if error:
        return {"ok": False, "error": error}

    user = await get_user(telegram_id)
    if not user or user["balance_stars"] < promo["shop_price_stars"]:
        return {"ok": False, "error": f"❌ Balans yetarli emas! Kerak: {promo['shop_price_stars']} ⭐"}

    await deduct_stars(telegram_id, promo["shop_price_stars"])
    return await _award_promo(bot, promo, telegram_id, first_name, username)


def gift_claim_keyboard(claim_id: int, price_stars: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🎁 Giftni olish", callback_data=f"giftclaim:real:{claim_id}")
    kb.button(text=f"⭐ {price_stars} ⭐ ga aylantirish", callback_data=f"giftclaim:stars:{claim_id}")
    kb.adjust(1)
    return kb.as_markup()


@router.callback_query(F.data.startswith("giftclaim:"))
async def gift_claim_callback(call: CallbackQuery, bot: Bot) -> None:
    """Box'dan yutilgan gift bo'yicha foydalanuvchining tanlovi: haqiqiy
    sovg'a sifatida olish yoki ichki ⭐ valyutaga aylantirish. Tanlov
    qilinmaguncha gift "saqlanadi" — tugmalar istalgan vaqt bosilishi mumkin."""
    parts = call.data.split(":")
    action, claim_id = parts[1], int(parts[2])

    claim = await get_gift_claim(claim_id)
    if not claim:
        await call.answer("❌ Topilmadi", show_alert=True)
        return
    if claim["telegram_id"] != call.from_user.id:
        await call.answer("❌ Bu sizga tegishli emas!", show_alert=True)
        return
    if claim["status"] != "pending":
        await call.answer("⚠️ Bu gift bo'yicha allaqachon tanlov qilingan!", show_alert=True)
        return

    if action == "stars":
        await update_gift_claim_status(claim_id, "claimed_stars")
        await add_stars(claim["telegram_id"], claim["price_stars"])
        text = (
            f"⭐ <b>{claim['item_name']}</b> — <b>{claim['price_stars']} ⭐</b> ga aylantirildi "
            f"va balansingizga qo'shildi!"
        )
        try:
            await call.message.edit_text(text)
        except TelegramBadRequest:
            await call.message.answer(text)
        await call.answer("✅ Starsga aylantirildi!", show_alert=False)
        return

    # action == "real" — haqiqiy gift sifatida olish
    item = await get_shop_item(claim["item_id"])

    variants = await get_gift_variants(claim["item_id"]) if item else []
    if len(variants) >= 2:
        # Mahsulotga bir nechta gift turi bog'langan — avval foydalanuvchi
        # aynan qaysi birini xohlashini tanlashi kerak (claim hali "pending"
        # holatida qoladi, giftvariant: callback uni yakunlaydi).
        text = f"🎁 <b>{claim['item_name']}</b>\n\nQaysi turini xohlaysiz?"
        try:
            await call.message.edit_text(text, reply_markup=gift_variant_choice_keyboard(claim_id, variants))
        except TelegramBadRequest:
            await call.message.answer(text, reply_markup=gift_variant_choice_keyboard(claim_id, variants))
        await call.answer()
        return

    delivered, error = await try_auto_deliver_gift(bot, claim["telegram_id"], item)

    if delivered:
        await update_gift_claim_status(claim_id, "claimed_gift")
        text = (
            f"✅ <b>{claim['item_name']}</b> avtomatik yuborildi — "
            f"Telegram'dagi \"Sovg'alar\" bo'limingizni tekshiring! ✨"
        )
        for admin_id in ADMIN_IDS:
            try:
                await bot.send_message(
                    admin_id,
                    f"🎁 <b>BOX'DAN GIFT TANLANDI VA AVTOMATIK YUBORILDI!</b>\n\n"
                    f"👤 Foydalanuvchi: {call.from_user.first_name} (@{call.from_user.username or '—'})\n"
                    f"🆔 ID: <code>{call.from_user.id}</code>\n"
                    f"🎁 Gift: <b>{claim['item_name']}</b> ({claim['price_stars']} ⭐)\n\n"
                    f"✅ Hech narsa qilish shart emas — allaqachon yuborilgan.",
                )
            except TelegramForbiddenError:
                pass
    else:
        # Qo'lda tasdiqlash yo'liga o'tamiz — mavjud withdrawals infratuzilmasi
        # (wd_approve/wd_reject) qayta ishlatiladi, shu bilan yagona joyda
        # kuzatiladi va ikki marta yuborib yuborilmaydi.
        w_id = await add_withdrawal(
            telegram_id=claim["telegram_id"],
            user_name=call.from_user.first_name or "",
            username=call.from_user.username or "",
            kind="gift",
            amount_stars=claim["price_stars"],
            item_name=claim["item_name"],
        )
        await update_gift_claim_status(claim_id, "claimed_gift")

        kb = InlineKeyboardBuilder()
        kb.button(text="✅ Gift yubordim", callback_data=f"wd_approve:{w_id}")
        kb.adjust(1)
        warn = f"⚠️ Avtomatik yuborish muvaffaqiyatsiz bo'ldi ({error}) — qo'lda yuboring!\n\n" if error else ""
        for admin_id in ADMIN_IDS:
            try:
                await bot.send_message(
                    admin_id,
                    f"🎁 <b>BOX'DAN GIFT TANLANDI — QO'LDA YUBORISH KERAK!</b>\n\n"
                    f"{warn}"
                    f"👤 Foydalanuvchi: {call.from_user.first_name} (@{call.from_user.username or '—'})\n"
                    f"🆔 ID: <code>{call.from_user.id}</code>\n"
                    f"🎁 Gift: <b>{claim['item_name']}</b> ({claim['price_stars']} ⭐)\n\n"
                    f"⚠️ Giftni foydalanuvchiga Telegram'da yuborgach, tugmani bosing.",
                    reply_markup=kb.as_markup(),
                )
            except TelegramForbiddenError:
                pass

        text = "✅ So'rovingiz qabul qilindi — gift tez orada admin tomonidan yuboriladi."

    try:
        await call.message.edit_text(text)
    except TelegramBadRequest:
        await call.message.answer(text)
    await call.answer("✅ Tanlandi!", show_alert=False)


@router.callback_query(F.data.startswith("giftvariant:"))
async def gift_variant_choice_callback(call: CallbackQuery, bot: Bot) -> None:
    """Mahsulotga bir nechta haqiqiy gift turi (gift_variants) bog'langanda,
    foydalanuvchi aynan qaysi birini xohlashini shu yerda yakuniy tanlaydi —
    sotib olganda HAM, box'dan yutib "Giftni olish"ni tanlagandan keyin HAM
    shu bitta callback orqali ishlaydi (deliver_gift/gift_claim_callback
    ikkalasi ham shu claim_id'ga havola qiladi)."""
    parts = call.data.split(":")
    claim_id, variant_id = int(parts[1]), int(parts[2])

    claim = await get_gift_claim(claim_id)
    if not claim:
        await call.answer("❌ Topilmadi", show_alert=True)
        return
    if claim["telegram_id"] != call.from_user.id:
        await call.answer("❌ Bu sizga tegishli emas!", show_alert=True)
        return
    if claim["status"] != "pending":
        await call.answer("⚠️ Bu gift bo'yicha allaqachon tanlov qilingan!", show_alert=True)
        return

    variant = await get_gift_variant(variant_id)
    if not variant or variant["item_id"] != claim["item_id"]:
        await call.answer("❌ Noto'g'ri tanlov", show_alert=True)
        return

    item = await get_shop_item(claim["item_id"])
    delivered, error = await try_auto_deliver_gift(
        bot, claim["telegram_id"], item, gift_id_override=variant["tg_gift_id"],
    )
    variant_label = variant["label"] or claim["item_name"]

    if delivered:
        await update_gift_claim_status(claim_id, "claimed_gift")
        text = (
            f"✅ <b>{variant_label}</b> avtomatik yuborildi — "
            f"Telegram'dagi \"Sovg'alar\" bo'limingizni tekshiring! ✨"
        )
        for admin_id in ADMIN_IDS:
            try:
                await bot.send_message(
                    admin_id,
                    f"🎁 <b>GIFT TURI TANLANDI VA AVTOMATIK YUBORILDI!</b>\n\n"
                    f"👤 Foydalanuvchi: {call.from_user.first_name} (@{call.from_user.username or '—'})\n"
                    f"🆔 ID: <code>{call.from_user.id}</code>\n"
                    f"🎁 Gift: <b>{variant_label}</b>\n\n"
                    f"✅ Hech narsa qilish shart emas — allaqachon yuborilgan.",
                )
            except TelegramForbiddenError:
                pass
    else:
        # Qo'lda tasdiqlash yo'liga o'tamiz — mavjud withdrawals infratuzilmasi
        # qayta ishlatiladi, shu bilan yagona joyda kuzatiladi va ikki marta
        # yuborib yuborilmaydi.
        w_id = await add_withdrawal(
            telegram_id=claim["telegram_id"],
            user_name=call.from_user.first_name or "",
            username=call.from_user.username or "",
            kind="gift",
            amount_stars=claim["price_stars"],
            item_name=variant_label,
        )
        await update_gift_claim_status(claim_id, "claimed_gift")

        kb = InlineKeyboardBuilder()
        kb.button(text="✅ Gift yubordim", callback_data=f"wd_approve:{w_id}")
        kb.adjust(1)
        warn = f"⚠️ Avtomatik yuborish muvaffaqiyatsiz bo'ldi ({error}) — qo'lda yuboring!\n\n" if error else ""
        for admin_id in ADMIN_IDS:
            try:
                await bot.send_message(
                    admin_id,
                    f"🎁 <b>GIFT TURI TANLANDI — QO'LDA YUBORISH KERAK!</b>\n\n"
                    f"{warn}"
                    f"👤 Foydalanuvchi: {call.from_user.first_name} (@{call.from_user.username or '—'})\n"
                    f"🆔 ID: <code>{call.from_user.id}</code>\n"
                    f"🎁 Gift: <b>{variant_label}</b>\n\n"
                    f"⚠️ Giftni foydalanuvchiga Telegram'da yuborgach, tugmani bosing.",
                    reply_markup=kb.as_markup(),
                )
            except TelegramForbiddenError:
                pass

        text = "✅ Tanlovingiz qabul qilindi — gift tez orada admin tomonidan yuboriladi."

    try:
        await call.message.edit_text(text)
    except TelegramBadRequest:
        await call.message.answer(text)
    await call.answer("✅ Tanlandi!", show_alert=False)


@router.callback_query(F.data.startswith("box_open:"))
async def box_open_callback(call: CallbackQuery, bot: Bot) -> None:
    """Boxni bot balansidagi (ichki) ⭐ bilan ochish."""
    box_id = call.data.split(":")[1]
    box = await get_box(box_id)
    if not box:
        await call.answer("❌ Box topilmadi!", show_alert=True)
        return

    user = await get_user(call.from_user.id)
    if not user:
        await call.answer("❌ Avval /start ni bosing!", show_alert=True)
        return

    today = datetime.now().strftime("%Y-%m-%d")
    if box["once_per_day"]:
        if user["last_daily_box"] == today:
            await call.answer("❌ Kunlik boxni bugun ishlatgansiz! Ertaga qayta oching.", show_alert=True)
            return

    if user["balance_stars"] < box["cost"]:
        await call.answer(f"❌ Balans yetarli emas! Kerak: {box['cost']} ⭐", show_alert=True)
        return

    # Box narxini ayiramiz
    await deduct_stars(call.from_user.id, box["cost"])
    if box["once_per_day"]:
        await set_daily_box_used(call.from_user.id, today)

    result = await open_box_and_award(
        bot, box, call.from_user.id, call.from_user.first_name, call.from_user.username,
    )

    await call.answer("🎉 Box ochildi!", show_alert=False)
    try:
        await call.message.delete()
    except TelegramBadRequest:
        pass
    if result["claim_id"]:
        await call.message.answer(
            result["text"],
            reply_markup=gift_claim_keyboard(result["claim_id"], result["gift_price_stars"]),
        )
        await show_boxes(call.message.answer, call.from_user.id)
    else:
        await show_boxes(call.message.answer, call.from_user.id, result["text"])


@router.callback_query(F.data.startswith("box_open_tgstars:"))
async def box_open_tgstars_callback(call: CallbackQuery, bot: Bot) -> None:
    """Boxni haqiqiy Telegram Stars bilan ochish uchun invoys yuboradi —
    to'lov muvaffaqiyatli o'tgach, box successful_payment_handler'da ochiladi."""
    box_id = call.data.split(":")[1]
    box = await get_box(box_id)
    if not box:
        await call.answer("❌ Box topilmadi!", show_alert=True)
        return
    if box["cost_tgstars"] <= 0:
        await call.answer("❌ Bu box uchun Stars narxi belgilanmagan!", show_alert=True)
        return

    user = await get_user(call.from_user.id)
    if not user:
        await call.answer("❌ Avval /start ni bosing!", show_alert=True)
        return

    today = datetime.now().strftime("%Y-%m-%d")
    if box["once_per_day"] and user["last_daily_box"] == today:
        await call.answer("❌ Kunlik boxni bugun ishlatgansiz! Ertaga qayta oching.", show_alert=True)
        return

    try:
        await bot.send_invoice(
            chat_id=call.from_user.id,
            title=f"📦 {box['name']}",
            description=box["desc_text"] or f"{box['name']} — {box['cost_tgstars']} Telegram Stars",
            payload=f"box:{box['box_id']}",
            currency="XTR",
            prices=[LabeledPrice(label=box["name"], amount=box["cost_tgstars"])],
            provider_token="",
        )
    except TelegramBadRequest as e:
        logger.error("Box uchun Stars invoys yuborilmadi (box_id=%s): %s", box_id, e)
        await call.answer("❌ Invoys yuborib bo'lmadi, keyinroq urinib ko'ring.", show_alert=True)
        return

    await call.answer()


@router.message(F.text == "ℹ️ Bot haqida")
async def about_bot_handler(message: Message) -> None:
    """Bot va Mini App qanday ishlashi haqida qisqa qo'llanma."""
    settings = await get_settings()
    text = (
        "ℹ️ <b>Bot haqida — qanday ishlaydi?</b>\n\n"
        "⭐ <b>Yulduz qanday topiladi?</b>\n"
        f"• Do'stlaringizni <b>🔗 Referal</b> havolangiz orqali taklif qiling — "
        f"har biri uchun <b>{settings['ref_reward_stars']} ⭐</b> olasiz "
        f"(do'stingiz majburiy kanallarga a'zo bo'lishi shart).\n"
        "• <b>🎰 Jekpot</b> boxlarini oching — tasodifiy miqdorda ⭐ yoki gift yutib olasiz.\n\n"
        "🛍️ <b>Do'kon</b>\n"
        "Gift, Premium va boshqa mahsulotlarni 3 xil usulda sotib olish mumkin: "
        "ichki ⭐ balansingizdan, haqiqiy Telegram Stars'dan, yoki karta (UZS) orqali.\n\n"
        "🎰 <b>Jekpot (boxlar)</b>\n"
        "Box ochilganda raketa uchadi — qancha baland uchsa, mukofot shuncha katta. "
        "Gift yutib olsangiz, uni <b>saqlab qo'yasiz</b>: keyin xohlaganingizda "
        "\"🎁 Giftni olish\" (haqiqiy sovg'a) yoki \"⭐ ga aylantirish\" (ichki balansga "
        "qo'shish) tugmalaridan birini bosasiz — shoshilish shart emas.\n\n"
        "💸 <b>Yulduz yechish</b>\n"
        f"Balansingiz kamida <b>{settings['min_withdraw_stars']} ⭐</b> bo'lsa, ⭐ (real to'lov) "
        "yoki gift sifatida yechib olishingiz mumkin. Ba'zi giftlar avtomatik yuboriladi, "
        "qolganlari bot egasi tomonidan qo'lda.\n\n"
        "✨ <b>Mini-App do'kon</b>\n"
        "Bir xil do'kon va Jekpot — chiroyli veb-sahifa ko'rinishida, tepadagi 🌙/☀️ "
        "tugmasi bilan dark/light mavzuni almashtirishingiz mumkin.\n\n"
        "❓ Savol bo'lsa — <b>📞 Aloqa</b> bo'limidan yozing."
    )

    has_miniapp = PUBLIC_BASE_URL.startswith("https://")
    kb = InlineKeyboardBuilder()
    if has_miniapp:
        kb.button(text="✨ Mini-App'ni ochish", web_app=WebAppInfo(url=f"{PUBLIC_BASE_URL}/webapp"))
    await message.answer(text, reply_markup=kb.as_markup() if has_miniapp else None)


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
    """"Otziv" tugmasi botda yozib qoldirish uchun emas — foydalanuvchini
    to'g'ridan-to'g'ri otziv kanaliga yo'naltiradi (bitta tugma bosish)."""
    s = await get_settings()

    if not s["reviews_channel"]:
        # Kanal hali sozlanmagan — botda yozib qoldirishga o'rniga aniq xabar beramiz
        await message.answer(
            "⭐ <b>Otziv</b>\n\n"
            "❌ Otziv kanali hali sozlanmagan.\n"
            "Admin: <code>Sozlamalar → Otziv kanali</code> bo'limidan kanalni belgilang.",
        )
        return

    kb = InlineKeyboardBuilder()
    kb.button(text="⭐ Otziv kanaliga o'tish", url=channel_url(s["reviews_channel"]))
    kb.adjust(1)

    await message.answer(
        "⭐ <b>Otziv</b>\n\n"
        "Otzivlar shu yerda emas, <b>otziv kanalimizda</b> qoldiriladi.\n"
        "Pastdagi tugmani bosing va kanalga o'ting 👇",
        reply_markup=kb.as_markup(),
    )


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

    # NFT bo'limi boshqacha ishlaydi — bot ichida sotilmaydi, foydalanuvchi
    # NFT sotiladigan guruhga yo'naltiriladi.
    if category == "nft":
        s = await get_settings()
        kb = InlineKeyboardBuilder()
        if s["nft_group"]:
            kb.button(text="🖼 NFT guruhiga o'tish", url=channel_url(s["nft_group"]))
        kb.button(text="🔙 Ortga", callback_data="shop")
        kb.adjust(1)

        if s["nft_group"]:
            text = (
                f"{label} <b>bo'limi</b>\n\n"
                f"NFT'lar bot ichida emas, <b>maxsus guruhda</b> sotiladi.\n"
                f"Pastdagi tugmani bosing va guruhga o'ting 👇"
            )
        else:
            text = (
                f"{label} <b>bo'limi</b>\n\n"
                f"❌ NFT guruhi hali sozlanmagan. Admin bilan bog'laning."
            )
        await call.message.edit_text(text, reply_markup=kb.as_markup())
        await call.answer()
        return

    # Promokodlar bo'limi ham boshqacha — do'kondan sotib olinadigan
    # promokodlar shop_items jadvalida emas, promo_codes jadvalida saqlanadi
    # (haqiqiy kod matni hech qachon ko'rsatilmaydi — faqat admin qo'ygan
    # do'kon nomi, tavsifi va narxi).
    if category == "promo":
        promos = await get_purchasable_promo_codes()
        if not promos:
            await call.answer("❌ Hozircha sotuvda promokod yo'q", show_alert=True)
            return
        kb = InlineKeyboardBuilder()
        for p in promos:
            kb.button(text=f"{p['shop_name']} — {p['shop_price_stars']} ⭐", callback_data=f"shoppromo:{p['id']}")
        kb.button(text="🔙 Ortga", callback_data="shop")
        kb.adjust(1)
        await call.message.edit_text(f"{label} <b>bo'limi:</b>", reply_markup=kb.as_markup())
        await call.answer()
        return

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


@router.callback_query(F.data.startswith("shoppromo:"))
async def shop_promo_detail_callback(call: CallbackQuery) -> None:
    """Do'kondagi promokod tafsilotlari — ATAYLAB TO'LIQ EMAS: star
    diapazoni va gift foizi ko'rsatilmaydi, aks holda foydalanuvchi eng kam
    mukofotni ko'rib xarid qilishni xohlamasligi mumkin (marketing uchun
    zararli). Faqat admin yozgan do'kon nomi va tavsifi ko'rsatiladi."""
    promo_id = int(call.data.split(":")[1])
    p = await get_promo_by_id(promo_id)
    if not p or p["shop_price_stars"] <= 0 or not p["shop_name"]:
        await call.answer("❌ Bu mahsulot endi mavjud emas", show_alert=True)
        return

    text = f"🎟 <b>{p['shop_name']}</b>\n\n💰 Narxi: <b>{p['shop_price_stars']} ⭐</b> (bot balansidan)"
    if p["desc_text"]:
        text += f"\n\n{p['desc_text']}"

    kb = InlineKeyboardBuilder()
    kb.button(text=f"💳 Sotib olish ({p['shop_price_stars']} ⭐)", callback_data=f"shoppromobuy:{promo_id}")
    kb.button(text="🔙 Ortga", callback_data="shop:promo")
    kb.adjust(1)
    await call.message.edit_text(text, reply_markup=kb.as_markup())
    await call.answer()


@router.callback_query(F.data.startswith("shoppromobuy:"))
async def shop_promo_buy_callback(call: CallbackQuery, bot: Bot) -> None:
    """Do'kondan promokodni ⭐ balans evaziga sotib olish — narx ayiriladi
    va promokod xuddi bepul kod kiritilgandek darhol ochiladi."""
    promo_id = int(call.data.split(":")[1])
    user = await get_user(call.from_user.id)
    if not user:
        await call.answer("❌ Avval /start ni bosing!", show_alert=True)
        return

    result = await buy_promo_from_shop(
        bot, call.from_user.id, call.from_user.first_name, call.from_user.username, promo_id,
    )
    if not result["ok"]:
        await call.answer(result["error"], show_alert=True)
        return

    await call.answer("🎉 Sotib olindi!", show_alert=False)
    try:
        await call.message.delete()
    except TelegramBadRequest:
        pass
    if result["claim_id"]:
        await call.message.answer(
            result["text"],
            reply_markup=gift_claim_keyboard(result["claim_id"], result["gift_price_stars"]),
        )
    else:
        await call.message.answer(result["text"])


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

    result = await deliver_gift(bot, call.from_user.id, item, price_stars=item["price_stars"], source="purchase")
    status_line, user_note = gift_delivery_texts(result)

    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(
                admin_id,
                f"🛒 <b>YANGI BUYURTMA!</b>\n\n"
                f"{status_line}"
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
        f"{user_note}",
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


# ---------- Telegram Stars (haqiqiy ⭐, Bot API to'lovi) ----------
# Bularni bot ichidagi virtual ⭐ balansi bilan aralashtirmang: bu yerda
# foydalanuvchi o'zining Telegramdagi haqiqiy Stars balansidan to'laydi
# (Telegram Payments API, currency="XTR", provider_token shart emas).

@router.callback_query(F.data.startswith("buy_tgstars:"))
async def buy_item_tgstars_callback(call: CallbackQuery, bot: Bot) -> None:
    """Mahsulotni foydalanuvchining haqiqiy Telegram Stars balansidan to'lash uchun invoys yuboradi."""
    item_id = int(call.data.split(":")[1])
    item = await get_shop_item(item_id)

    if not item:
        await call.answer("❌ Mahsulot topilmadi", show_alert=True)
        return
    if item["price_stars"] <= 0:
        await call.answer("❌ Bu mahsulot uchun Stars narxi belgilanmagan!", show_alert=True)
        return

    label = CATEGORIES.get(item["category"], item["category"])
    try:
        await bot.send_invoice(
            chat_id=call.from_user.id,
            title=f"{label} — {item['name']}",
            description=item["description"] or f"{item['name']} ({item['price_stars']} ⭐ Telegram Stars)",
            payload=f"shop_item:{item['id']}",
            currency="XTR",  # Telegram Stars uchun maxsus valyuta kodi
            prices=[LabeledPrice(label=item["name"], amount=item["price_stars"])],
            provider_token="",  # Telegram Stars uchun bo'sh qoldiriladi
        )
    except TelegramBadRequest as e:
        logger.error("Stars invoys yuborilmadi (item_id=%s): %s", item_id, e)
        await call.answer("❌ Invoys yuborib bo'lmadi, keyinroq urinib ko'ring.", show_alert=True)
        return

    await call.answer()


@router.pre_checkout_query()
async def pre_checkout_handler(pre_checkout_query: PreCheckoutQuery, bot: Bot) -> None:
    """Telegram to'lovni tasdiqlashdan oldin so'raydi — mahsulot hali mavjudligini tekshiramiz."""
    payload = pre_checkout_query.invoice_payload
    ok = True
    error_message = None
    if payload.startswith("shop_item:"):
        item_id = int(payload.split(":")[1])
        item = await get_shop_item(item_id)
        if not item:
            ok = False
            error_message = "Bu mahsulot endi mavjud emas."
    elif payload.startswith("box:"):
        box_id = payload.split(":", 1)[1]
        box = await get_box(box_id)
        if not box:
            ok = False
            error_message = "Bu box endi mavjud emas."
    await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=ok, error_message=error_message)


async def _handle_box_stars_payment(message: Message, bot: Bot, payload: str, payment) -> None:
    """Box haqiqiy Telegram Stars bilan to'langach shu yerda ochiladi (rol
    o'ynatiladi) va mukofot beriladi — bot balansi yoki kunlik cheklovga
    umuman tegilmaydi, chunki bu boshqa (real pul) to'lov yo'li."""
    box_id = payload.split(":", 1)[1]
    box = await get_box(box_id)
    if not box:
        await message.answer("⚠️ To'lov qabul qilindi, lekin box topilmadi. Admin bilan bog'laning: @Kottabolladan")
        return

    if box["once_per_day"]:
        today = datetime.now().strftime("%Y-%m-%d")
        await set_daily_box_used(message.from_user.id, today)

    result = await open_box_and_award(
        bot, box, message.from_user.id, message.from_user.first_name or "", message.from_user.username or "",
        via_tgstars=True,
    )

    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(
                admin_id,
                f"💫 <b>BOX TELEGRAM STARS BILAN OCHILDI!</b>\n\n"
                f"👤 Foydalanuvchi: {message.from_user.first_name} (@{message.from_user.username or '—'})\n"
                f"🆔 ID: <code>{message.from_user.id}</code>\n"
                f"📦 Box: <b>{box['name']}</b>\n"
                f"💰 To'lov: <b>{payment.total_amount} ⭐ Telegram Stars</b>\n"
                f"📅 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            )
        except TelegramForbiddenError:
            pass

    if result["claim_id"]:
        await message.answer(
            result["text"],
            reply_markup=gift_claim_keyboard(result["claim_id"], result["gift_price_stars"]),
        )
        await show_boxes(message.answer, message.from_user.id)
    else:
        await show_boxes(message.answer, message.from_user.id, result["text"])


@router.message(F.successful_payment)
async def successful_payment_handler(message: Message, bot: Bot) -> None:
    """To'lov muvaffaqiyatli o'tgach — mahsulotni yetkazib beramiz va adminga xabar beramiz."""
    payment = message.successful_payment
    payload = payment.invoice_payload

    if payload.startswith("box:"):
        await _handle_box_stars_payment(message, bot, payload, payment)
        return

    if payload.startswith("topup:"):
        # Admin botning haqiqiy Stars balansini o'zi to'ldirdi — bu to'lov hech qanday
        # buyurtma/mahsulotga bog'liq emas, faqat Telegram'ning o'zi balansni oshiradi.
        # Komissiya olinmaydi: bot kodi to'lovning bir tiyinini ham ushlab qolmaydi —
        # to'liq summasi Telegram tomonidan botning real balansiga qo'shiladi.
        await message.answer(
            f"✅ <b>Balans to'ldirildi!</b>\n\n"
            f"Botning haqiqiy Stars balansiga <b>{payment.total_amount} ⭐</b> qo'shildi "
            f"(komissiyasiz, 100%). Bu balansdan endi foydalanuvchilarga haqiqiy "
            f"gift'lar avtomatik yuborilishi mumkin.",
        )
        return

    if not payload.startswith("shop_item:"):
        return

    item_id = int(payload.split(":")[1])
    item = await get_shop_item(item_id)
    if not item:
        await message.answer("⚠️ To'lov qabul qilindi, lekin mahsulot topilmadi. Admin bilan bog'laning.")
        return

    label = CATEGORIES.get(item["category"], item["category"])

    order_id = await add_order(
        telegram_id=message.from_user.id,
        user_name=message.from_user.first_name or "",
        username=message.from_user.username or "",
        item_id=item["id"],
        item_name=item["name"],
        category=item["category"],
        amount_uzs=0,
        proof_file_id="",
    )
    # Telegram o'zi to'lovni tasdiqlagan — buyurtma darhol tasdiqlangan deb belgilanadi
    await update_order_status(order_id, "approved")

    result = await deliver_gift(bot, message.from_user.id, item, price_stars=payment.total_amount, source="purchase")
    status_line, user_note = gift_delivery_texts(result)
    if result.get("mode") == "none":
        status_line = "⚠️ Mahsulotni foydalanuvchiga o'tkazing (Telegram'da yuborish mumkin)!\n\n"
        user_note = "Tez orada mahsulot sizga yetkaziladi. 🎁"

    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(
                admin_id,
                f"✨ <b>TELEGRAM STARS BILAN TO'LANDI!</b>\n\n"
                f"{status_line}"
                f"👤 Foydalanuvchi: {message.from_user.first_name} (@{message.from_user.username or '—'})\n"
                f"🆔 ID: <code>{message.from_user.id}</code>\n"
                f"{label} <b>{item['name']}</b>\n"
                f"💰 Narxi: <b>{payment.total_amount} ⭐ Telegram Stars</b>\n"
                f"🧾 Buyurtma: #{order_id}\n"
                f"📅 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            )
        except TelegramForbiddenError:
            pass

    await message.answer(
        f"✅ <b>To'lov qabul qilindi!</b>\n\n"
        f"{label} <b>{item['name']}</b> — {payment.total_amount} ⭐ Telegram Stars orqali sotib olindi.\n"
        f"🧾 Buyurtma: #{order_id}\n\n"
        f"{user_note}",
    )


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

    # Gift toifasida va Telegram gift ID bog'langan bo'lsa — avtomatik yuboramiz
    # (2+ gift turi bog'langan bo'lsa — foydalanuvchi tanlaydi, "choice" rejimi)
    result = await deliver_gift(bot, order["telegram_id"], item, price_stars=order["amount_uzs"], source="purchase")
    delivered = result.get("mode") == "delivered"
    is_choice = result.get("mode") == "choice"
    error = result.get("error", "")

    try:
        await bot.send_message(
            order["telegram_id"],
            f"✅ <b>To'lov tasdiqlandi!</b>\n\n"
            f"🛒 #{order_id} buyurtma: <b>{order['item_name']}</b> — {order['amount_uzs']:,} so'm\n\n"
            + (f"⭐ <b>{item['deliver_stars']} yulduz</b> hisobingizga qo'shildi!\n"
               if order["category"] == "star" and item and item["deliver_stars"] > 0 else "")
            + ("✅ Gift avtomatik yuborildi — Telegram'dagi \"Sovg'alar\" bo'limingizni tekshiring! ✨\n"
               if delivered else "")
            + ("🎯 Gift turini tanlash uchun alohida xabar yubordik — shu yerdan tanlang!\n"
               if is_choice else "")
            + (f"🎁 {order['item_name']} sizga yuboriladi (egasi: @Kottabolladan).\n"
               if order["category"] != "star" and not delivered and not is_choice else "")
            + "\nDo'kondan foydalanishda davom eting! 🛍️",
        )
    except TelegramForbiddenError:
        pass

    caption_extra = "\n\n✅ <b>TASDIQLANDI</b>"
    if delivered:
        caption_extra += " (gift avtomatik yuborildi)"
    elif is_choice:
        caption_extra += " (foydalanuvchi gift turini tanlamoqda)"
    elif error:
        caption_extra += f" — ⚠️ avto-yuborish xato berdi ({error}), qo'lda yuboring!"
    caption_extra += f" — {call.from_user.first_name}"
    try:
        await call.message.edit_caption(caption=f"{call.message.caption}{caption_extra}")
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
#  ADMIN PANEL
# ============================================================

def admin_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📊 Statistika", callback_data="admin:stats")
    kb.button(text="⚙️ Sozlamalar", callback_data="admin:settings")
    kb.button(text="🛒 Savdo boshqaruvi", callback_data="admin:shop")
    kb.button(text="💸 Kutilayotgan to'lovlar", callback_data="admin:withdrawals")
    kb.button(text="🎁 TG Gift avto-yuborish", callback_data="admin:tggifts")
    kb.button(text="📦 Boxlar boshqaruvi", callback_data="admin:boxes")
    kb.button(text="🎟 Promokodlar", callback_data="admin:promo")
    kb.button(text="📢 Rassilka", callback_data="admin:broadcast")
    kb.button(text="🔗 Kanallar", callback_data="admin:channels")
    kb.button(text="📞 Aloqa boshqaruvi", callback_data="admin:contacts")
    kb.button(text="🔋 Bot balansini to'ldirish", callback_data="admin:topup")
    kb.button(text="💰 Bot Stars balansi", callback_data="admin:starbalance")
    kb.button(text="👤 Foydalanuvchini boshqarish", callback_data="admin:usersearch")
    kb.adjust(2)
    return kb.as_markup()


def _user_manage_keyboard(telegram_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="0️⃣ Balansni 0 ga tushirish", callback_data=f"admin:userzero:{telegram_id}")
    kb.button(text="✏️ Aniq qiymat qo'yish", callback_data=f"admin:usersetbal:{telegram_id}")
    kb.button(text="🔍 Boshqa foydalanuvchi", callback_data="admin:usersearch")
    kb.button(text="🔙 Ortga", callback_data="admin")
    kb.adjust(1)
    return kb.as_markup()


async def _show_user_profile(target, telegram_id: int) -> None:
    user = await get_user(telegram_id)
    if not user:
        await target.answer(
            f"❌ <code>{telegram_id}</code> ID'li foydalanuvchi topilmadi. "
            f"Faqat botdan kamida bir marta /start bosgan foydalanuvchilar mavjud bo'ladi.",
            reply_markup=_user_manage_keyboard(telegram_id),
        )
        return
    await target.answer(
        f"👤 <b>Foydalanuvchi profili</b>\n\n"
        f"🆔 ID: <code>{user['telegram_id']}</code>\n"
        f"⭐ Ichki balans: <b>{user['balance_stars']}</b>\n"
        f"🔗 Referallar: <b>{user['referals_count']}</b>\n"
        f"👥 Taklif qilgan: <code>{user['referrer_id'] or '—'}</code>\n"
        f"📅 Qo'shilgan: {user['joined_at']}\n\n"
        f"Quyidagi tugmalar orqali balansni boshqarishingiz mumkin:",
        reply_markup=_user_manage_keyboard(telegram_id),
    )


@router.callback_query(F.data == "admin:usersearch")
async def admin_user_search_start(call: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(call.from_user.id):
        await call.answer("❌ Siz admin emassiz!", show_alert=True)
        return
    await state.set_state(AdminUserStates.search)
    await call.message.edit_text(
        "👤 <b>Foydalanuvchini boshqarish</b>\n\n"
        "Foydalanuvchining Telegram ID raqamini yuboring (masalan: <code>123456789</code>).\n\n"
        "💡 ID'ni foydalanuvchining profilidan yoki bot adminga yuborgan xabarlardagi "
        "<code>🆔 ID:</code> qatoridan olishingiz mumkin.",
    )
    await call.answer()


@router.message(AdminUserStates.search)
async def admin_user_search_input(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return
    raw = (message.text or "").strip()
    if not raw.lstrip("-").isdigit():
        await message.answer("❌ Iltimos, faqat Telegram ID raqamini yuboring (masalan: 123456789).")
        return
    telegram_id = int(raw)
    await state.clear()
    await _show_user_profile(message, telegram_id)


@router.callback_query(F.data.startswith("admin:userzero:"))
async def admin_user_zero(call: CallbackQuery) -> None:
    if not is_admin(call.from_user.id):
        await call.answer("❌ Siz admin emassiz!", show_alert=True)
        return
    telegram_id = int(call.data.split(":")[2])
    user = await get_user(telegram_id)
    if not user:
        await call.answer("❌ Foydalanuvchi topilmadi", show_alert=True)
        return
    await set_user_balance(telegram_id, 0)
    await call.answer("✅ Balans 0 ga tushirildi!", show_alert=True)
    await _show_user_profile(call.message, telegram_id)


@router.callback_query(F.data.startswith("admin:usersetbal:"))
async def admin_user_setbal_start(call: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(call.from_user.id):
        await call.answer("❌ Siz admin emassiz!", show_alert=True)
        return
    telegram_id = int(call.data.split(":")[2])
    await state.set_state(AdminUserStates.set_balance)
    await state.update_data(target_id=telegram_id)
    await call.message.edit_text(
        f"✏️ <b>Yangi balans qiymatini yuboring</b>\n\n"
        f"Foydalanuvchi: <code>{telegram_id}</code>\n"
        f"Butun son kiriting (masalan: <code>0</code> yoki <code>500</code>).",
    )
    await call.answer()


@router.message(AdminUserStates.set_balance)
async def admin_user_setbal_input(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return
    raw = (message.text or "").strip()
    if not raw.lstrip("-").isdigit():
        await message.answer("❌ Iltimos, faqat butun son yuboring (masalan: 0 yoki 500).")
        return
    data = await state.get_data()
    telegram_id = data.get("target_id")
    amount = int(raw)
    await state.clear()
    if telegram_id is None:
        await message.answer("❌ Xatolik: foydalanuvchi aniqlanmadi, qaytadan urinib ko'ring.")
        return
    user = await get_user(telegram_id)
    if not user:
        await message.answer("❌ Foydalanuvchi topilmadi.")
        return
    await set_user_balance(telegram_id, amount)
    await message.answer(f"✅ Balans <b>{amount}</b> ga o'rnatildi!")
    await _show_user_profile(message, telegram_id)


@router.callback_query(F.data == "admin:starbalance")
async def admin_star_balance(call: CallbackQuery, bot: Bot) -> None:
    """Botning haqiqiy Telegram Stars balansini hisoblab ko'rsatadi.

    Bot API'da balansni to'g'ridan-to'g'ri qaytaradigan alohida metod yo'q
    (masalan getMyStarBalance) — shuning uchun barcha tranzaksiyalar tarixini
    (get_star_transactions) o'qib, kirim (source bor — foydalanuvchi to'lov
    qilgan) va chiqim (receiver bor — gift yuborilgan/pul yechilgan)
    summalarini o'zimiz hisoblaymiz: joriy balans = kirimlar - chiqimlar.
    """
    if not is_admin(call.from_user.id):
        await call.answer("❌ Siz admin emassiz!", show_alert=True)
        return
    await call.answer("⏳ Hisoblanmoqda...")

    async def _compute() -> tuple[int, int, int]:
        income = 0
        outcome = 0
        count = 0
        offset = 0
        limit = 100
        while True:
            result = await bot.get_star_transactions(offset=offset, limit=limit, request_timeout=15)
            txns = result.transactions
            if not txns:
                break
            for t in txns:
                count += 1
                if t.source is not None:
                    income += t.amount
                elif t.receiver is not None:
                    outcome += t.amount
            if len(txns) < limit or count >= 2000:
                break
            offset += limit
        return income, outcome, count

    try:
        # Umumiy 30 soniyalik "zaxira" chegara — API sekin javob bersa yoki
        # bot uyg'onayotgan (Render "sleep") bo'lsa ham, funksiya cheksiz
        # osilib qolmasdan, albatta aniq xato bilan tugaydi.
        income, outcome, count = await asyncio.wait_for(_compute(), timeout=30)
    except TimeoutError:
        logger.error("Stars balansi hisoblanmadi: 30 soniyada javob kelmadi (timeout)")
        await call.message.answer(
            "⚠️ Balansni hisoblab bo'lmadi: Telegram/serverdan 30 soniyada javob kelmadi.\n\n"
            "Bir necha soniyadan keyin qayta urinib ko'ring — ba'zan Render xizmati "
            "uyg'onayotgan bo'ladi (bepul tarifda vaqtincha 'uxlab qoladi').\n\n"
            "Aniq balansni @BotFather → Bot Settings orqali ham tekshirishingiz mumkin.",
            reply_markup=admin_keyboard(),
        )
        return
    except Exception as e:
        logger.error("Stars balansi hisoblanmadi: %s", e)
        await call.message.answer(
            f"⚠️ Balansni hisoblab bo'lmadi: <code>{safe_error_text(e)}</code>\n\n"
            f"Aniq balansni @BotFather → Bot Settings orqali tekshiring.",
            reply_markup=admin_keyboard(),
        )
        return

    balance = income - outcome
    try:
        await call.message.answer(
            f"💰 <b>Botning haqiqiy Telegram Stars balansi</b>\n\n"
            f"📥 Jami kirim: <b>{income} ⭐</b>\n"
            f"📤 Jami chiqim (gift/refund): <b>{outcome} ⭐</b>\n"
            f"➖➖➖➖➖➖➖➖➖➖\n"
            f"💎 Joriy balans: <b>{balance} ⭐</b>\n\n"
            f"🧾 Tekshirilgan tranzaksiyalar: {count} ta\n\n"
            f"Aniqroq/rasmiy ma'lumot uchun: @BotFather → botingiz → Bot Settings → Payments.",
            reply_markup=admin_keyboard(),
        )
    except Exception as e:
        # Natija hisoblandi, lekin xabar yuborishning o'zi xato berdi (masalan
        # vaqtinchalik tarmoq muammosi) — hech bo'lmasa loglarda ko'rinsin,
        # aks holda admin hech qanday javob olmay qoladi.
        logger.error("Stars balansi hisoblandi (%s ⭐), lekin xabar yuborilmadi: %s", balance, e)


@router.callback_query(F.data == "admin:topup")
async def admin_topup_start(call: CallbackQuery, state: FSMContext) -> None:
    """Admin botning haqiqiy Stars balansini o'zi to'ldirishi uchun miqdor so'raydi."""
    if not is_admin(call.from_user.id):
        await call.answer("❌ Siz admin emassiz!", show_alert=True)
        return
    await state.set_state(TopupStates.amount)
    await call.message.edit_text(
        "🔋 <b>Bot balansini to'ldirish</b>\n\n"
        "Nechta ⭐ Stars bilan botning haqiqiy balansini to'ldirmoqchisiz? "
        "Raqamni yuboring (masalan: <code>500</code>).\n\n"
        "💡 To'lov to'g'ridan-to'g'ri Telegram orqali o'tadi, hech qanday komissiya "
        "olinmaydi — yuborgan summangizning 100% botning real Stars balansiga tushadi. "
        "Shu balansdan keyin foydalanuvchilarga haqiqiy gift'lar avtomatik yuboriladi.",
    )
    await call.answer()


@router.message(TopupStates.amount)
async def admin_topup_amount_input(message: Message, state: FSMContext, bot: Bot) -> None:
    if not is_admin(message.from_user.id):
        return
    raw = (message.text or "").strip()
    if not raw.isdigit() or int(raw) <= 0:
        await message.answer("❌ Iltimos, musbat butun son yuboring (masalan: 500).")
        return
    amount = int(raw)
    if amount > 100000:
        await message.answer("❌ Bir martada eng ko'pi bilan 100000 ⭐ yuborish mumkin.")
        return

    await state.clear()
    try:
        await bot.send_invoice(
            chat_id=message.from_user.id,
            title="Bot balansini to'ldirish",
            description=(
                f"Botning haqiqiy Stars balansiga {amount} ⭐ qo'shiladi. "
                "Komissiyasiz — 100% balansga tushadi."
            ),
            payload=f"topup:{amount}",
            currency="XTR",  # Telegram Stars uchun maxsus valyuta kodi
            prices=[LabeledPrice(label="Balansni to'ldirish", amount=amount)],
            provider_token="",  # Telegram Stars uchun bo'sh qoldiriladi
        )
    except TelegramBadRequest as e:
        logger.error("Balans to'ldirish invoysi yuborilmadi: %s", e)
        await message.answer("❌ Invoys yuborib bo'lmadi, keyinroq urinib ko'ring.")


@router.callback_query(F.data == "admin:withdrawals")
async def admin_pending_withdrawals(call: CallbackQuery) -> None:
    """Hali to'lanmagan/berilmagan barcha yechish so'rovlari ro'yxati — admin
    chatidagi eski xabarni yo'qotib qo'ysa ham, shu yerdan holatni tekshiradi."""
    if not is_admin(call.from_user.id):
        await call.answer("❌ Siz admin emassiz!", show_alert=True)
        return

    pending = await get_pending_withdrawals()
    if not pending:
        text = "💸 <b>Kutilayotgan to'lovlar</b>\n\n✅ Hozircha kutilayotgan so'rov yo'q."
    else:
        lines = ["💸 <b>Kutilayotgan to'lovlar</b>\n"]
        total_stars = 0
        for w in pending:
            if w["kind"] == "gift":
                lines.append(f"#{w['id']} — 🎁 {w['item_name']} ({w['amount_stars']} ⭐) — {w['user_name']} (@{w['username'] or '—'}, ID: {w['telegram_id']})")
            else:
                lines.append(f"#{w['id']} — ⭐ {w['amount_stars']} ⭐ — {w['user_name']} (@{w['username'] or '—'}, ID: {w['telegram_id']})")
            total_stars += w["amount_stars"]
        lines.append(f"\nJami: <b>{len(pending)}</b> ta so'rov, <b>{total_stars} ⭐</b> qiymatida.")
        lines.append("\nHar birini tasdiqlash/bekor qilish uchun o'sha so'rov yuborilgan admin xabaridagi tugmalardan foydalaning.")
        text = "\n".join(lines)

    await call.message.edit_text(text, reply_markup=back_to_admin_keyboard())
    await call.answer()


# ---------- Telegram Gift avtomatik yuborish ----------
# "Gift sifatida yechish" so'ralganda, agar shop_items'dagi gift'ga haqiqiy
# Telegram gift_id bog'langan bo'lsa, admin qo'lda bosishi shart bo'lmaydi —
# bot o'zining haqiqiy Stars balansidan (Bot API sendGift) avtomatik yuboradi.
# Bog'lanmagan giftlar uchun eski qo'lda tasdiqlash yo'li ishlayveradi.

@router.callback_query(F.data == "admin:tggifts")
async def admin_tggifts_menu(call: CallbackQuery) -> None:
    if not is_admin(call.from_user.id):
        await call.answer("❌ Siz admin emassiz!", show_alert=True)
        return

    gifts = await get_shop_items("gift")
    kb = InlineKeyboardBuilder()
    for g in gifts:
        variants = await get_gift_variants(g["id"])
        if len(variants) >= 2:
            mark = f"✅ ({len(variants)} tur)"
        elif g["tg_gift_id"]:
            mark = "✅"
        else:
            mark = "❌"
        kb.button(text=f"{mark} {g['name']}", callback_data=f"admin:tggift_set:{g['id']}")
    kb.button(text="📋 Mavjud Telegram gift'lar", callback_data="admin:tggift_catalog")
    kb.button(text="🔙 Ortga", callback_data="admin")
    kb.adjust(1)

    text = (
        "🎁 <b>TG Gift avto-yuborish</b>\n\n"
        "Do'kondagi \"Gift\" mahsulotlaridan qay birini foydalanuvchi "
        "\"Yulduz yechish → Gift sifatida\" orqali tanlasa — agar shu "
        "mahsulotga haqiqiy Telegram gift ID bog'langan bo'lsa, bot uni "
        "<b>o'zining haqiqiy Stars balansidan avtomatik</b> yuboradi (admin "
        "qo'lda bosishi shart emas). Bog'lanmagan mahsulotlar eskichasiga "
        "qo'lda tasdiqlanadi.\n\n"
        "💡 Bitta mahsulotga BIR NECHTA gift turini bog'lasangiz (masalan "
        "\"🐻/🧸\" — ikkalasi ham), foydalanuvchi sotib olganda yoki yutib "
        "olganda aynan qaysi birini xohlashini o'zi tanlaydi.\n\n"
        "✅ — gift ID bog'langan (bitta), ✅ (N tur) — bir nechta tur "
        "bog'langan (foydalanuvchi tanlaydi), ❌ — bog'lanmagan.\n"
        "Mahsulotni tanlang:"
    )
    await call.message.edit_text(text, reply_markup=kb.as_markup())
    await call.answer()


@router.callback_query(F.data == "admin:tggift_catalog")
async def admin_tggift_catalog(call: CallbackQuery, bot: Bot) -> None:
    if not is_admin(call.from_user.id):
        await call.answer("❌ Siz admin emassiz!", show_alert=True)
        return

    try:
        gifts = await bot.get_available_gifts()
    except Exception as e:
        logger.error("get_available_gifts xato: %s", e)
        await call.answer("❌ Telegram'dan gift ro'yxatini olib bo'lmadi.", show_alert=True)
        return

    if not gifts.gifts:
        text = "📋 <b>Mavjud Telegram gift'lar</b>\n\nHozircha ro'yxat bo'sh."
    else:
        lines = ["📋 <b>Mavjud Telegram gift'lar</b>\n(ID'ni nusxalab, mahsulotga bog'lang)\n"]
        for g in gifts.gifts[:40]:
            emoji = g.sticker.emoji if g.sticker and g.sticker.emoji else "🎁"
            limit = f" (qolgan {g.remaining_count}/{g.total_count})" if g.total_count else " (cheksiz)"
            lines.append(f"{emoji} <code>{g.id}</code> — {g.star_count} ⭐{limit}")
        text = "\n".join(lines)

    kb = InlineKeyboardBuilder()
    kb.button(text="🔙 Ortga", callback_data="admin:tggifts")
    await call.message.edit_text(text, reply_markup=kb.as_markup())
    await call.answer()


@router.callback_query(F.data.startswith("admin:tggift_set:"))
async def admin_tggift_set_start(call: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(call.from_user.id):
        await call.answer("❌ Siz admin emassiz!", show_alert=True)
        return

    item_id = int(call.data.split(":")[2])
    item = await get_shop_item(item_id)
    if not item:
        await call.answer("❌ Mahsulot topilmadi", show_alert=True)
        return

    await state.set_state(TgGiftStates.gift_id)
    await state.update_data(item_id=item_id)

    variants = await get_gift_variants(item_id)
    if len(variants) >= 2:
        current = "\n".join(f"• {v['label'] or '—'}: <code>{v['tg_gift_id']}</code>" for v in variants)
    else:
        current = item["tg_gift_id"] or "❌ bog'lanmagan"
    await call.message.edit_text(
        f"🎁 <b>{item['name']}</b>\n\n"
        f"Hozirgi holat:\n{current}\n\n"
        f"📋 \"Mavjud Telegram gift'lar\" bo'limidan ID'ni nusxalab shu yerga yuboring.\n\n"
        f"💡 Bitta gift ID — bitta qatorda yuboring (masalan: <code>abc123</code>).\n"
        f"💡 BIR NECHTA turni bog'lash uchun — har birini ALOHIDA qatorga, "
        f"xohlasangiz nomi bilan yozing:\n"
        f"<code>abc123 🐻 Ayiqcha\ndef456 🧸 Panda</code>\n"
        f"(shunda foydalanuvchi qaysi birini xohlashini o'zi tanlaydi)\n\n"
        f"O'chirish uchun <code>-</code> yozing.",
    )
    await call.answer()


@router.message(TgGiftStates.gift_id)
async def admin_tggift_id_input(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    item_id = data.get("item_id")
    raw = (message.text or "").strip()

    if raw == "-":
        await update_shop_item(item_id, tg_gift_id="")
        await clear_gift_variants(item_id)
        await state.clear()
        await message.answer("✅ Bog'lanish o'chirildi — bu gift endi qo'lda tasdiqlanadi.")
        return

    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]

    if len(lines) == 1:
        # Bitta gift ID — eski oddiy usul (variantlar bo'lsa, tozalanadi)
        gift_id = lines[0].split(maxsplit=1)[0]
        await update_shop_item(item_id, tg_gift_id=gift_id)
        await clear_gift_variants(item_id)
        await state.clear()
        await message.answer(f"✅ Bog'landi! Endi bu gift avtomatik yuboriladi.\nID: <code>{gift_id}</code>")
        return

    # Bir nechta qator — har biri alohida gift turi (variant) sifatida saqlanadi
    await clear_gift_variants(item_id)
    await update_shop_item(item_id, tg_gift_id="")
    saved = []
    for ln in lines:
        parts = ln.split(maxsplit=1)
        gift_id = parts[0]
        label = parts[1] if len(parts) > 1 else ""
        await add_gift_variant(item_id, gift_id, label)
        saved.append(f"• {label or '—'}: <code>{gift_id}</code>")
    await state.clear()
    await message.answer(
        f"✅ {len(saved)} ta gift turi bog'landi! Foydalanuvchi sotib olganda/yutib olganda "
        f"tanlaydi:\n\n" + "\n".join(saved),
    )


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

    text = (
        f"📊 <b>Statistika</b>\n\n"
        f"👥 Jami foydalanuvchilar: <b>{len(users)}</b>\n"
        f"⭐ Referal mukofoti: {settings['ref_reward_stars']}\n"
        f"👥 Min. referallar: {settings['min_referals_required']}\n"
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
    kb.button(text="💸 Yulduz yechish minimumi", callback_data="admin:set:min_withdraw")
    kb.button(text="💳 To'lov kartasi", callback_data="admin:set:pay_card")
    kb.button(text="⭐ Otziv kanali", callback_data="admin:set:reviews_channel")
    kb.button(text="🖼 NFT guruh linki", callback_data="admin:set:nft_group")
    kb.button(text="🎁 Gift yuborish matni", callback_data="admin:set:gift_caption")
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
    nft_display = s["nft_group"] or "❌ o'rnatilmagan"
    gift_caption_display = s["gift_caption"] or DEFAULT_GIFT_CAPTION
    text = (
        f"⚙️ <b>Sozlamalar</b>\n\n"
        f"⭐ Referal mukofoti: <b>{s['ref_reward_stars']} ⭐</b>\n"
        f"👥 Xarid uchun min. referallar: <b>{s['min_referals_required']}</b>\n"
        f"💸 Yulduz yechish minimumi: <b>{s['min_withdraw_stars']} ⭐</b>\n"
        f"💳 To'lov kartasi: <code>{s['pay_card']}</code>\n"
        f"⭐ Otziv: {reviews_display}\n"
        f"🖼 NFT guruhi: {nft_display}\n"
        f"🎁 Gift matni: <code>{gift_caption_display}</code>\n\n"
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


@router.callback_query(F.data == "admin:set:nft_group")
async def set_nft_group(call: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(call.from_user.id):
        await call.answer("❌ Siz admin emassiz!", show_alert=True)
        return
    await state.set_state(SettingsStates.nft_group)
    await call.message.edit_text(
        "🖼 <b>NFT guruh linkini yuboring:</b>\n"
        "(masalan: <code>@nftguruh</code> yoki <code>https://t.me/nftguruh</code>)\n\n"
        "Do'kondagi NFT bo'limi foydalanuvchini shu guruhga yo'naltiradi — "
        "NFT'lar bot ichida emas, o'sha guruhda sotiladi.",
    )
    await call.answer()


@router.message(SettingsStates.nft_group)
async def nft_group_input(message: Message, state: FSMContext) -> None:
    raw = message.text.strip()
    if len(raw) < 3:
        await message.answer("❌ Iltimos, to'g'ri havola yuboring!")
        return
    await update_settings(nft_group=raw)
    await state.clear()
    await message.answer(f"✅ NFT guruh linki saqlandi: <b>{raw}</b>", reply_markup=admin_keyboard())


@router.callback_query(F.data == "admin:set:gift_caption")
async def set_gift_caption(call: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(call.from_user.id):
        await call.answer("❌ Siz admin emassiz!", show_alert=True)
        return
    settings = await get_settings()
    current = settings.get("gift_caption") or DEFAULT_GIFT_CAPTION
    await state.set_state(SettingsStates.gift_caption)
    await call.message.edit_text(
        "🎁 <b>Gift yuborish matnini yozing:</b>\n\n"
        "Bu matn haqiqiy Telegram gift avtomatik yuborilganda unga qo'shiladi.\n"
        "<code>{item}</code> — gift nomi bilan avtomatik almashtiriladi.\n\n"
        f"Joriy: <code>{current}</code>\n\n"
        "Standart holatga qaytarish uchun <code>-</code> yozing.",
    )
    await call.answer()


@router.message(SettingsStates.gift_caption)
async def gift_caption_input(message: Message, state: FSMContext) -> None:
    raw = message.text.strip()
    new_value = "" if raw == "-" else raw[:255]
    await update_settings(gift_caption=new_value)
    await state.clear()
    shown = new_value or DEFAULT_GIFT_CAPTION
    await message.answer(f"✅ Gift yuborish matni saqlandi:\n<code>{shown}</code>", reply_markup=admin_keyboard())


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


# ---------- Boxlar boshqaruvi ----------

def box_detail_text(b: dict) -> str:
    prob = f"{b['gift_drop_prob'] * 100:.0f}%"
    daily = "✅ Ha" if b["once_per_day"] else "❌ Yo'q"
    tgstars_line = f"💫 Telegram Stars narxi: <b>{b['cost_tgstars']} ⭐</b>\n" if b["cost_tgstars"] > 0 else "💫 Telegram Stars narxi: <b>o'rnatilmagan</b>\n"
    bonus = b.get("tgstars_bonus_percent") or 0
    if bonus > 0:
        boosted = min(100.0, b["gift_drop_prob"] * 100 + bonus)
        bonus_line = f"🚀 TG Stars bonusi: <b>+{bonus:.0f}%</b> (real Stars bilan ochsa gift foizi ≈ <b>{boosted:.0f}%</b> bo'ladi)\n"
    else:
        bonus_line = "🚀 TG Stars bonusi: <b>yo'q</b>\n"
    return (
        f"📦 <b>{b['name']}</b>\n\n"
        f"💰 Narxi (bot balansi): <b>{b['cost']} ⭐</b>\n"
        f"{tgstars_line}"
        f"⭐ Star diapazoni: <b>{b['star_min']}–{b['star_max']}</b>\n"
        f"🎁 Gift tushish foizi: <b>{prob}</b>\n"
        f"{bonus_line}"
        f"🎟️ Gift tanlovi: eng arzon <b>{b['gift_pool_size']}</b> tasidan biri\n"
        f"📂 Gift toifasi: <b>{b['gift_category']}</b>\n"
        f"⏳ Kuniga 1 marta: {daily}\n"
        f"📝 Tavsif: {b['desc_text']}"
    )


def box_edit_keyboard(box_id: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✏️ Nom", callback_data=f"admin:box:field:name:{box_id}")
    kb.button(text="💰 Narx (balans)", callback_data=f"admin:box:field:cost:{box_id}")
    kb.button(text="💫 Narx (TG Stars)", callback_data=f"admin:box:field:tgstars:{box_id}")
    kb.button(text="⭐ Star min", callback_data=f"admin:box:field:starmin:{box_id}")
    kb.button(text="⭐ Star max", callback_data=f"admin:box:field:starmax:{box_id}")
    kb.button(text="🎁 Gift foizi %", callback_data=f"admin:box:field:prob:{box_id}")
    kb.button(text="🚀 TG Stars bonusi %", callback_data=f"admin:box:field:tgbonus:{box_id}")
    kb.button(text="🎟️ Gift soni (N)", callback_data=f"admin:box:field:pool:{box_id}")
    kb.button(text="📂 Gift toifasi", callback_data=f"admin:box:cat:{box_id}")
    kb.button(text="⏳ Kunlik cheklov", callback_data=f"admin:box:daily:{box_id}")
    kb.button(text="📝 Tavsif", callback_data=f"admin:box:field:desc:{box_id}")
    kb.button(text="🔙 Ortga", callback_data="admin:boxes")
    kb.adjust(2)
    return kb.as_markup()


@router.callback_query(F.data == "admin:boxes")
async def admin_boxes(call: CallbackQuery) -> None:
    if not is_admin(call.from_user.id):
        await call.answer("❌ Siz admin emassiz!", show_alert=True)
        return
    boxes = await get_all_boxes()
    kb = InlineKeyboardBuilder()
    for b in boxes:
        kb.button(text=b["name"], callback_data=f"admin:box:edit:{b['box_id']}")
    kb.button(text="🔙 Ortga", callback_data="admin")
    kb.adjust(1)
    await call.message.edit_text("📦 <b>Boxlar boshqaruvi</b>\n\nSozlamoqchi bo'lgan boxni tanlang:", reply_markup=kb.as_markup())
    await call.answer()


@router.callback_query(F.data.startswith("admin:box:edit:"))
async def admin_box_edit(call: CallbackQuery) -> None:
    if not is_admin(call.from_user.id):
        await call.answer("❌ Siz admin emassiz!", show_alert=True)
        return
    box_id = call.data.split(":")[3]
    box = await get_box(box_id)
    if not box:
        await call.answer("❌ Box topilmadi!", show_alert=True)
        return
    await call.message.edit_text(box_detail_text(box), reply_markup=box_edit_keyboard(box_id))
    await call.answer()


BOX_FIELD_PROMPTS = {
    "name": "✏️ <b>Yangi nomni yozing:</b>",
    "cost": "💰 <b>Yangi narxni yozing (bot balansi, yulduzda):</b>",
    "tgstars": "💫 <b>Yangi Telegram Stars narxini yozing:</b>\n(Bu — foydalanuvchi o'zining haqiqiy Telegram Stars balansidan to'laydigan narx. 0 yozsangiz, bu box uchun Stars orqali ochish o'chiriladi.)",
    "starmin": "⭐ <b>Yangi star minimumini yozing:</b>",
    "starmax": "⭐ <b>Yangi star maksimumini yozing:</b>",
    "prob": "🎁 <b>Gift tushish foizini yozing (0–100):</b>\nMasalan: 50 — 50% gift, 50% stars. 0 yozsangiz, faqat stars tushadi.",
    "tgbonus": "🚀 <b>TG Stars bonusini yozing (foiz punkti, 0–100):</b>\nBox haqiqiy Telegram Stars bilan ochilganda gift foiziga shuncha qo'shiladi. Masalan: gift foizi 50% bo'lib, bu yerga 20 yozsangiz — Stars bilan ochganda gift foizi 70% bo'ladi. 0 — bonus yo'q.",
    "pool": "🎟️ <b>Gift tanlovi sonini yozing:</b>\nDo'kondagi eng arzon shuncha giftdan biri tushadi. Masalan: 2",
    "desc": "📝 <b>Yangi tavsifni yozing:</b>",
}

@router.callback_query(F.data.startswith("admin:box:field:"))
async def admin_box_field(call: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(call.from_user.id):
        await call.answer("❌ Siz admin emassiz!", show_alert=True)
        return
    parts = call.data.split(":")
    field = parts[3]
    box_id = parts[4]
    await state.set_state(BoxStates.input)
    await state.update_data(box_id=box_id, field=field)
    await call.message.edit_text(BOX_FIELD_PROMPTS.get(field, "✏️ Qiymatni yozing:"))
    await call.answer()


@router.message(BoxStates.input)
async def box_field_input(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        await state.clear()
        await message.answer("❌ Siz admin emassiz!")
        return
    data = await state.get_data()
    box_id = data.get("box_id")
    field = data.get("field")
    raw = message.text.strip()

    value = raw
    if field in ("cost", "tgstars", "starmin", "starmax", "pool"):
        try:
            value = int(raw)
        except ValueError:
            await message.answer("❌ Iltimos, butun son kiriting!")
            return
        if value < 0:
            await message.answer("❌ Manfiy bo'lishi mumkin emas!")
            return
        if field == "pool" and value < 1:
            await message.answer("❌ Kamida 1 bo'lishi kerak!")
            return
    elif field == "prob":
        try:
            value = float(raw.replace("%", "").replace(",", "."))
        except ValueError:
            await message.answer("❌ Iltimos, son kiriting (0–100)!")
            return
        if value < 0 or value > 100:
            await message.answer("❌ Foiz 0 dan 100 gacha bo'lishi kerak!")
            return
        value = value / 100.0
    elif field == "tgbonus":
        try:
            value = float(raw.replace("%", "").replace(",", "."))
        except ValueError:
            await message.answer("❌ Iltimos, son kiriting (0–100)!")
            return
        if value < 0 or value > 100:
            await message.answer("❌ Foiz 0 dan 100 gacha bo'lishi kerak!")
            return
    else:
        if len(raw) < 1:
            await message.answer("❌ Bo'sh bo'lishi mumkin emas!")
            return

    if field == "starmax":
        box = await get_box(box_id)
        if box and value < box["star_min"]:
            await message.answer("❌ Star max, star min dan kichik bo'lishi mumkin emas!")
            return

    column = {
        "name": "name",
        "cost": "cost",
        "tgstars": "cost_tgstars",
        "starmin": "star_min",
        "starmax": "star_max",
        "prob": "gift_drop_prob",
        "tgbonus": "tgstars_bonus_percent",
        "pool": "gift_pool_size",
        "desc": "desc_text",
    }[field]
    await update_box(box_id, **{column: value})
    await state.clear()

    box = await get_box(box_id)
    await message.answer(box_detail_text(box), reply_markup=box_edit_keyboard(box_id))


@router.callback_query(F.data.startswith("admin:box:cat:"))
async def admin_box_cat(call: CallbackQuery) -> None:
    if not is_admin(call.from_user.id):
        await call.answer("❌ Siz admin emassiz!", show_alert=True)
        return
    box_id = call.data.split(":")[3]
    kb = InlineKeyboardBuilder()
    kb.button(text="🎁 Gift", callback_data=f"admin:box:catset:{box_id}:gift")
    kb.button(text="💎 Premium", callback_data=f"admin:box:catset:{box_id}:premium")
    kb.button(text="🔙 Ortga", callback_data=f"admin:box:edit:{box_id}")
    kb.adjust(2)
    await call.message.edit_text("📂 <b>Gift toifasini tanlang:</b>", reply_markup=kb.as_markup())
    await call.answer()


@router.callback_query(F.data.startswith("admin:box:catset:"))
async def admin_box_catset(call: CallbackQuery) -> None:
    if not is_admin(call.from_user.id):
        await call.answer("❌ Siz admin emassiz!", show_alert=True)
        return
    parts = call.data.split(":")
    box_id, cat = parts[3], parts[4]
    await update_box(box_id, gift_category=cat)
    box = await get_box(box_id)
    await call.message.edit_text(box_detail_text(box), reply_markup=box_edit_keyboard(box_id))
    await call.answer("✅ Saqlandi!", show_alert=False)


@router.callback_query(F.data.startswith("admin:box:daily:"))
async def admin_box_daily(call: CallbackQuery) -> None:
    if not is_admin(call.from_user.id):
        await call.answer("❌ Siz admin emassiz!", show_alert=True)
        return
    box_id = call.data.split(":")[3]
    box = await get_box(box_id)
    if not box:
        await call.answer("❌ Box topilmadi!", show_alert=True)
        return
    new = 0 if box["once_per_day"] else 1
    await update_box(box_id, once_per_day=new)
    box = await get_box(box_id)
    await call.message.edit_text(box_detail_text(box), reply_markup=box_edit_keyboard(box_id))
    await call.answer("✅ Saqlandi!", show_alert=False)


# ---------- Promokodlar boshqaruvi ----------
# Har bir promokod — aslida alohida "box": xuddi admin:boxes bilan bir xil
# sozlamalar (star diapazoni, gift foizi, gift soni, gift toifasi, kunlik
# cheklov, tavsif), faqat qo'shimcha ravishda kod matni, faol/faolsiz holati
# va muddati (umrbod yoki N kun) bilan.

def promo_detail_text(p: dict) -> str:
    prob = f"{p['gift_drop_prob'] * 100:.0f}%"
    reuse = "✅ Ha (har kuni qayta ishlatsa bo'ladi)" if p["once_per_day"] else "❌ Yo'q (faqat 1 marta)"
    status = "✅ Faol" if p["is_active"] else "⛔ Faolsizlantirilgan"
    expiry = "♾️ Umrbod" if not p["expires_at"] else f"⏳ {p['expires_at']} gacha"
    if p["shop_price_stars"] > 0 and p["shop_name"]:
        shop_line = f"✅ Ha — <b>{p['shop_name']}</b>, narxi <b>{p['shop_price_stars']} ⭐</b>"
    else:
        shop_line = "❌ Yo'q (faqat bepul kod orqali)"
    return (
        f"🎟️ <b>{p['code']}</b>\n\n"
        f"✏️ Nomi: <b>{p['name']}</b>\n"
        f"⭐ Star diapazoni: <b>{p['star_min']}–{p['star_max']}</b>\n"
        f"🎁 Gift tushish foizi: <b>{prob}</b>\n"
        f"🎟️ Gift tanlovi: eng arzon <b>{p['gift_pool_size']}</b> tasidan biri\n"
        f"📂 Gift toifasi: <b>{p['gift_category']}</b>\n"
        f"🔁 Qayta ishlatish: {reuse}\n"
        f"📝 Tavsif: {p['desc_text']}\n\n"
        f"🛍 Do'konda sotiladimi: {shop_line}\n\n"
        f"{expiry}\n"
        f"📌 Holati: <b>{status}</b>\n"
        f"👥 Ishlatilgan: <b>{p['used_count']}</b> marta\n"
        f"📅 Yaratilgan: {p['created_at']}"
    )


def promo_edit_keyboard(p: dict) -> InlineKeyboardMarkup:
    pid = p["id"]
    kb = InlineKeyboardBuilder()
    kb.button(text="✏️ Kod", callback_data=f"admin:promofield:code:{pid}")
    kb.button(text="✏️ Nom", callback_data=f"admin:promofield:name:{pid}")
    kb.button(text="⭐ Star min", callback_data=f"admin:promofield:starmin:{pid}")
    kb.button(text="⭐ Star max", callback_data=f"admin:promofield:starmax:{pid}")
    kb.button(text="🎁 Gift foizi %", callback_data=f"admin:promofield:prob:{pid}")
    kb.button(text="🎟️ Gift soni (N)", callback_data=f"admin:promofield:pool:{pid}")
    kb.button(text="📂 Gift toifasi", callback_data=f"admin:promocat:{pid}")
    kb.button(text="🔁 Qayta ishlatish", callback_data=f"admin:promodaily:{pid}")
    kb.button(text="📝 Tavsif", callback_data=f"admin:promofield:desc:{pid}")
    kb.button(text="⏳ Muddat", callback_data=f"admin:promofield:duration:{pid}")
    kb.button(text="🛍 Do'kon narxi (⭐)", callback_data=f"admin:promofield:shopprice:{pid}")
    kb.button(text="🏷 Do'kon nomi", callback_data=f"admin:promofield:shopname:{pid}")
    toggle_text = "⛔ Faolsizlantirish" if p["is_active"] else "✅ Faollashtirish"
    kb.button(text=toggle_text, callback_data=f"admin:promotoggle:{pid}")
    kb.button(text="🗑 O'chirish", callback_data=f"admin:promodel:{pid}")
    kb.button(text="🔙 Ortga", callback_data="admin:promo")
    kb.adjust(2)
    return kb.as_markup()


async def _promo_list_render() -> tuple[str, InlineKeyboardMarkup]:
    promos = await get_all_promo_codes()
    kb = InlineKeyboardBuilder()
    for p in promos:
        emoji = "✅" if p["is_active"] else "⛔"
        kb.button(text=f"{emoji} {p['code']} — {p['star_min']}–{p['star_max']}⭐", callback_data=f"admin:promoview:{p['id']}")
    kb.button(text="➕ Yangi promokod", callback_data="admin:promoadd")
    kb.button(text="🔙 Ortga", callback_data="admin")
    kb.adjust(1)
    text = "🎟 <b>Promokodlar boshqaruvi</b>\n\nJoriy promokodlar (✅ faol / ⛔ faolsiz):" if promos else "🎟 <b>Promokodlar boshqaruvi</b>\n\nHozircha promokod yo'q."
    return text, kb.as_markup()


@router.callback_query(F.data == "admin:promo")
async def admin_promo_list(call: CallbackQuery) -> None:
    if not is_admin(call.from_user.id):
        await call.answer("❌ Siz admin emassiz!", show_alert=True)
        return
    text, markup = await _promo_list_render()
    await call.message.edit_text(text, reply_markup=markup)
    await call.answer()


@router.callback_query(F.data.startswith("admin:promoview:"))
async def admin_promo_view(call: CallbackQuery) -> None:
    if not is_admin(call.from_user.id):
        await call.answer("❌ Siz admin emassiz!", show_alert=True)
        return
    promo_id = int(call.data.split(":")[2])
    p = await get_promo_by_id(promo_id)
    if not p:
        await call.answer("❌ Promokod topilmadi!", show_alert=True)
        return
    await call.message.edit_text(promo_detail_text(p), reply_markup=promo_edit_keyboard(p))
    await call.answer()


PROMO_FIELD_PROMPTS = {
    "code": "✏️ <b>Yangi promokod matnini yozing:</b>",
    "name": "✏️ <b>Yangi nomni yozing:</b>",
    "starmin": "⭐ <b>Yangi star minimumini yozing:</b>",
    "starmax": "⭐ <b>Yangi star maksimumini yozing:</b>",
    "prob": "🎁 <b>Gift tushish foizini yozing (0–100):</b>\nMasalan: 50 — 50% gift, 50% stars. 0 yozsangiz, faqat stars tushadi.",
    "pool": "🎟️ <b>Gift tanlovi sonini yozing:</b>\nDo'kondagi eng arzon shuncha giftdan biri tushadi. Masalan: 2",
    "desc": "📝 <b>Yangi tavsifni yozing:</b>\n(Bu — do'kon vitrinasida ko'rinadigan marketing matni ham bo'ladi, star diapazoni ko'rsatilmaydi.)",
    "duration": "⏳ <b>Necha kunga amal qilsin?</b>\nSon kiriting (masalan: 7). <b>0</b> yozsangiz — promokod <b>umrbod</b> (cheksiz muddatli) bo'ladi.",
    "shopprice": "🛍 <b>Do'kondagi narxini yozing (⭐, bot balansidan):</b>\n0 yozsangiz — bu promokod do'konda sotilmaydi (faqat bepul kod orqali ishlaydi).",
    "shopname": "🏷 <b>Do'konda ko'rinadigan nomni yozing:</b>\n(Diqqat: bu haqiqiy promokod matni emas — foydalanuvchilar buni ko'radi, xaqiqiy kod hech qachon ko'rsatilmaydi.)",
}


@router.callback_query(F.data.startswith("admin:promofield:"))
async def admin_promo_field(call: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(call.from_user.id):
        await call.answer("❌ Siz admin emassiz!", show_alert=True)
        return
    parts = call.data.split(":")
    field = parts[2]
    promo_id = int(parts[3])
    await state.set_state(PromoAdminStates.field_input)
    await state.update_data(promo_id=promo_id, field=field)
    await call.message.edit_text(PROMO_FIELD_PROMPTS.get(field, "✏️ Qiymatni yozing:"))
    await call.answer()


@router.message(PromoAdminStates.field_input)
async def promo_field_input(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        await state.clear()
        await message.answer("❌ Siz admin emassiz!")
        return
    data = await state.get_data()
    promo_id = data.get("promo_id")
    field = data.get("field")
    raw = message.text.strip()

    if field == "duration":
        try:
            days = int(raw)
        except ValueError:
            await message.answer("❌ Iltimos, butun son kiriting!")
            return
        if days < 0:
            await message.answer("❌ Manfiy bo'lishi mumkin emas!")
            return
        expires_at = "" if days == 0 else (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        await update_promo_code(promo_id, expires_at=expires_at)
        await state.clear()
        p = await get_promo_by_id(promo_id)
        await message.answer(promo_detail_text(p), reply_markup=promo_edit_keyboard(p))
        return

    if field == "code":
        code = raw.upper()
        if not code or len(code) < 3:
            await message.answer("❌ Promokod kamida 3 ta belgidan iborat bo'lishi kerak!")
            return
        existing = await get_promo_by_code(code)
        if existing and existing["id"] != promo_id:
            await message.answer("❌ Bu promokod allaqachon mavjud! Boshqa kod yozing:")
            return
        await update_promo_code(promo_id, code=code)
        await state.clear()
        p = await get_promo_by_id(promo_id)
        await message.answer(promo_detail_text(p), reply_markup=promo_edit_keyboard(p))
        return

    value = raw
    if field in ("starmin", "starmax", "pool", "shopprice"):
        try:
            value = int(raw)
        except ValueError:
            await message.answer("❌ Iltimos, butun son kiriting!")
            return
        if value < 0:
            await message.answer("❌ Manfiy bo'lishi mumkin emas!")
            return
        if field == "pool" and value < 1:
            await message.answer("❌ Kamida 1 bo'lishi kerak!")
            return
    elif field == "prob":
        try:
            value = float(raw.replace("%", "").replace(",", "."))
        except ValueError:
            await message.answer("❌ Iltimos, son kiriting (0–100)!")
            return
        if value < 0 or value > 100:
            await message.answer("❌ Foiz 0 dan 100 gacha bo'lishi kerak!")
            return
        value = value / 100.0
    else:
        if len(raw) < 1:
            await message.answer("❌ Bo'sh bo'lishi mumkin emas!")
            return

    if field == "starmax":
        p = await get_promo_by_id(promo_id)
        if p and value < p["star_min"]:
            await message.answer("❌ Star max, star min dan kichik bo'lishi mumkin emas!")
            return

    column = {
        "name": "name",
        "starmin": "star_min",
        "starmax": "star_max",
        "prob": "gift_drop_prob",
        "pool": "gift_pool_size",
        "desc": "desc_text",
        "shopprice": "shop_price_stars",
        "shopname": "shop_name",
    }[field]
    await update_promo_code(promo_id, **{column: value})
    await state.clear()

    p = await get_promo_by_id(promo_id)
    await message.answer(promo_detail_text(p), reply_markup=promo_edit_keyboard(p))


@router.callback_query(F.data.startswith("admin:promocat:"))
async def admin_promo_cat(call: CallbackQuery) -> None:
    if not is_admin(call.from_user.id):
        await call.answer("❌ Siz admin emassiz!", show_alert=True)
        return
    promo_id = int(call.data.split(":")[2])
    kb = InlineKeyboardBuilder()
    kb.button(text="🎁 Gift", callback_data=f"admin:promocatset:{promo_id}:gift")
    kb.button(text="💎 Premium", callback_data=f"admin:promocatset:{promo_id}:premium")
    kb.button(text="🔙 Ortga", callback_data=f"admin:promoview:{promo_id}")
    kb.adjust(2)
    await call.message.edit_text("📂 <b>Gift toifasini tanlang:</b>", reply_markup=kb.as_markup())
    await call.answer()


@router.callback_query(F.data.startswith("admin:promocatset:"))
async def admin_promo_catset(call: CallbackQuery) -> None:
    if not is_admin(call.from_user.id):
        await call.answer("❌ Siz admin emassiz!", show_alert=True)
        return
    parts = call.data.split(":")
    promo_id, cat = int(parts[2]), parts[3]
    await update_promo_code(promo_id, gift_category=cat)
    p = await get_promo_by_id(promo_id)
    await call.message.edit_text(promo_detail_text(p), reply_markup=promo_edit_keyboard(p))
    await call.answer("✅ Saqlandi!", show_alert=False)


@router.callback_query(F.data.startswith("admin:promodaily:"))
async def admin_promo_daily(call: CallbackQuery) -> None:
    if not is_admin(call.from_user.id):
        await call.answer("❌ Siz admin emassiz!", show_alert=True)
        return
    promo_id = int(call.data.split(":")[2])
    p = await get_promo_by_id(promo_id)
    if not p:
        await call.answer("❌ Promokod topilmadi!", show_alert=True)
        return
    await update_promo_code(promo_id, once_per_day=0 if p["once_per_day"] else 1)
    p = await get_promo_by_id(promo_id)
    await call.message.edit_text(promo_detail_text(p), reply_markup=promo_edit_keyboard(p))
    await call.answer("✅ Saqlandi!", show_alert=False)


@router.callback_query(F.data.startswith("admin:promotoggle:"))
async def admin_promo_toggle(call: CallbackQuery) -> None:
    if not is_admin(call.from_user.id):
        await call.answer("❌ Siz admin emassiz!", show_alert=True)
        return
    promo_id = int(call.data.split(":")[2])
    p = await get_promo_by_id(promo_id)
    if not p:
        await call.answer("❌ Promokod topilmadi!", show_alert=True)
        return
    await set_promo_active(promo_id, not p["is_active"])
    p = await get_promo_by_id(promo_id)
    await call.message.edit_text(promo_detail_text(p), reply_markup=promo_edit_keyboard(p))
    await call.answer("✅ Saqlandi!", show_alert=False)


@router.callback_query(F.data.startswith("admin:promodel:"))
async def admin_promo_delete(call: CallbackQuery) -> None:
    if not is_admin(call.from_user.id):
        await call.answer("❌ Siz admin emassiz!", show_alert=True)
        return
    promo_id = int(call.data.split(":")[2])
    await delete_promo_code(promo_id)
    await call.answer("✅ O'chirildi!", show_alert=True)
    text, markup = await _promo_list_render()
    await call.message.edit_text(text, reply_markup=markup)


@router.callback_query(F.data == "admin:promoadd")
async def admin_promo_add_start(call: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(call.from_user.id):
        await call.answer("❌ Siz admin emassiz!", show_alert=True)
        return
    await state.set_state(PromoAdminStates.new_code)
    await call.message.edit_text(
        "🎟️ <b>Yangi promokod</b>\n\n"
        "Promokod matnini yozing (masalan: <code>BONUS2026</code>).\n"
        "Yoki <code>random</code> deb yozsangiz, avtomatik kod yaratamiz.\n\n"
        "Bekor qilish: /cancel",
    )
    await call.answer()


@router.message(PromoAdminStates.new_code)
async def admin_promo_add_code(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        await state.clear()
        await message.answer("❌ Siz admin emassiz!")
        return
    raw = (message.text or "").strip()
    if raw.lower() == "random":
        for _ in range(5):
            code = "".join(random.choices(string.ascii_uppercase + string.digits, k=8))
            if not await get_promo_by_code(code):
                break
    else:
        code = raw.upper()

    if not code or len(code) < 3:
        await message.answer("❌ Promokod kamida 3 ta belgidan iborat bo'lishi kerak!")
        return
    if await get_promo_by_code(code):
        await message.answer("❌ Bu promokod allaqachon mavjud! Boshqa kod yozing:")
        return

    await state.clear()
    promo_id = await create_promo_code(code, f"🎟️ {code}")
    p = await get_promo_by_id(promo_id)
    await message.answer(
        f"✅ <b>Promokod yaratildi!</b> Endi uni xuddi oddiy box kabi sozlang 👇",
    )
    await message.answer(promo_detail_text(p), reply_markup=promo_edit_keyboard(p))


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
        kb.button(text=f"🔍 {ch['channel_id']}", callback_data=f"admin:chcheck:{ch['id']}")
        kb.button(text=f"🗑️ {ch['invite_link'][:30]}", callback_data=f"admin:chdel:{ch['id']}")
    kb.button(text="➕ Kanal qo'shish", callback_data="admin:channel_add")
    kb.button(text="🔙 Ortga", callback_data="admin")
    kb.adjust(2)

    await call.message.edit_text(text, reply_markup=kb.as_markup())
    await call.answer()


@router.callback_query(F.data.startswith("admin:chcheck:"))
async def admin_check_channel(call: CallbackQuery, bot: Bot) -> None:
    """Botning shu kanalda ADMIN ekanligini darhol tekshiradi — "obuna
    bo'lsa ham obuna emas deyapti" xatosining asosiy sababi (bot admin emas
    yoki kanal ID xato) shu yerda haqiqiy foydalanuvchi kutmasdan aniqlanadi."""
    if not is_admin(call.from_user.id):
        await call.answer("❌ Siz admin emassiz!", show_alert=True)
        return
    row_id = int(call.data.split(":")[2])
    channels = await get_channels()
    ch = next((c for c in channels if c["id"] == row_id), None)
    if not ch:
        await call.answer("❌ Kanal topilmadi!", show_alert=True)
        return

    chat_id = normalize_channel_id(ch["channel_id"])
    try:
        me = await bot.me()
        member = await bot.get_chat_member(chat_id, me.id)
        if member.status == ChatMemberStatus.ADMINISTRATOR:
            result = "✅ Hammasi joyida — bot bu kanalda <b>admin</b>, a'zolik tekshiruvi ishlaydi."
        elif member.status == ChatMemberStatus.CREATOR:
            result = "✅ Hammasi joyida — bot bu kanalning egasi, a'zolik tekshiruvi ishlaydi."
        else:
            result = (
                f"❌ Bot bu kanalda admin EMAS (holati: <code>{member.status}</code>)!\n\n"
                f"Botni kanalga <b>admin</b> qilib qo'shing, aks holda a'zolik hech qachon "
                f"to'g'ri tekshirilmaydi."
            )
    except (TelegramBadRequest, TelegramForbiddenError) as e:
        result = (
            f"❌ Kanalni tekshirib bo'lmadi: <code>{safe_error_text(e)}</code>\n\n"
            f"Ehtimol: kanal ID/username noto'g'ri kiritilgan, yoki bot bu kanalga "
            f"umuman qo'shilmagan. Kanal ID: <code>{chat_id}</code>"
        )

    await call.answer()
    await call.message.answer(f"🔍 <b>{ch['channel_id']}</b>\n\n{result}")


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
        "🔗 <b>Kanalni yuboring:</b>\n"
        "Quyidagilardan birini yozing:\n"
        "• Username: <code>@mychannel</code> yoki <code>mychannel</code>\n"
        "• Havola: <code>https://t.me/mychannel</code>\n"
        "• Yoki raqamli ID: <code>-1001234567890</code> (faqat yopiq/private kanallar uchun kerak)\n\n"
        "Bot kanalda admin bo'lishi kerak!",
    )
    await call.answer()


@router.message(AddChannelStates.channel_id)
async def channel_id_received(message: Message, state: FSMContext) -> None:
    raw = message.text.strip()

    # Taklif (invite) havolasi — t.me/+hash yoki t.me/joinchat/hash — a'zolikni
    # tekshirish uchun ISHLATIB BO'LMAYDI (bot API bunday havoladan chat_id'ni
    # bilib ola olmaydi). Bu — "obuna bo'lsa ham obuna emas" xatosining eng
    # ko'p uchraydigan sababi, shuning uchun darhol ogohlantiramiz.
    stripped = raw
    for prefix in ("https://", "http://"):
        if stripped.lower().startswith(prefix):
            stripped = stripped[len(prefix):]
            break
    if stripped.lower().startswith("t.me/+") or "t.me/joinchat/" in stripped.lower():
        await message.answer(
            "❌ <b>Bu — taklif (invite) havolasi, uni ishlatib bo'lmaydi!</b>\n\n"
            "Yopiq/private kanal uchun a'zolikni tekshirish faqat <b>raqamli kanal ID</b> "
            "orqali ishlaydi (masalan: <code>-1001234567890</code>).\n\n"
            "Raqamli ID'ni olish uchun: kanalga istalgan xabarni forward qiling "
            "@userinfobot ga, yoki kanal sozlamalaridan foydalaning.\n\n"
            "Boshqa qiymat yuboring:",
        )
        return

    normalized = normalize_channel_id(raw)
    await state.update_data(channel_id=normalized)
    await state.set_state(AddChannelStates.invite_link)
    await message.answer(
        f"✅ Kanal ID sifatida <code>{normalized}</code> saqlanadi.\n\n"
        f"🔗 <b>Kanal havolasini yuboring:</b>\n(masalan: <code>https://t.me/mychannel</code>)"
    )


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
#  MINI APP (Telegram Web App do'kon) — Stars orqali to'lov
# ============================================================
# Do'kon endi bot xabarlari o'rniga chiroyli veb-sahifa (Telegram Mini App)
# ko'rinishida ham ochilishi mumkin. Sahifa shu botning o'z aiohttp serveri
# orqali /webapp manzilida beriladi (alohida hosting kerak emas). To'lov
# Telegram'ning o'z Stars invoys mexanizmi orqali (Telegram.WebApp.openInvoice)
# amalga oshadi — muvaffaqiyatli to'lov xuddi oddiy bot chatidagi kabi
# successful_payment_handler orqali qayta ishlanadi, shuning uchun mahsulot
# yetkazib berish logikasi ikkalasida ham bir xil.

BOT_LOGO_DATA_URI = "data:image/png;base64,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"


MINI_APP_HTML = """<!doctype html>
<html lang="uz">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Stars Bot</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
  :root {
    --bg: var(--tg-theme-bg-color, #f5f6fa);
    --text: var(--tg-theme-text-color, #111111);
    --hint: var(--tg-theme-hint-color, #888888);
    --btn: var(--tg-theme-button-color, #2ea6ff);
    --btn-text: var(--tg-theme-button-text-color, #ffffff);
    --secbg: var(--tg-theme-secondary-bg-color, #ffffff);
    --header-bg: var(--tg-theme-bg-color, #ffffff);
    --card-border: rgba(255,255,255,0.35);
    --card-border-top: rgba(255,255,255,0.55);
  }
  /* Foydalanuvchi qo'lda tanlagan mavzu — Telegram'ning o'z ranglaridan
     ustun turadi, chunki bu ranglar Telegram tashqarisida ham (masalan
     brauzerda ochilsa) ishlashi kerak. */
  :root[data-theme="light"] {
    --bg: #f5f6fa; --text: #111111; --hint: #888888; --btn: #2ea6ff;
    --btn-text: #ffffff; --secbg: #ffffff; --header-bg: #ffffff;
    --card-border: rgba(255,255,255,0.35); --card-border-top: rgba(255,255,255,0.55);
  }
  :root[data-theme="dark"] {
    --bg: #101017; --text: #f0f0f5; --hint: #9797a3; --btn: #3aa9ff;
    --btn-text: #ffffff; --secbg: #1b1b24; --header-bg: #14141b;
    --card-border: rgba(255,255,255,0.08); --card-border-top: rgba(255,255,255,0.14);
  }
  :root[data-theme="dark"] .bg-decor { opacity: 0.6; }
  :root[data-theme="dark"] .bg-grid { opacity: 0.3; }
  :root[data-theme="dark"] .balance-chip,
  :root[data-theme="dark"] .theme-toggle { box-shadow: 0 2px 8px rgba(0,0,0,0.35); }
  * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
  html { scroll-behavior: smooth; }
  html, body { min-height: 100%; }
  body {
    margin: 0; padding: 0 0 40px;
    color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    position: relative;
    background-color: var(--bg);
  }
  /* ---- Fon: harakatlanuvchi rangli "aurora" dog'lari + nozik nuqta to'r ---- */
  .bg-decor {
    position: fixed; inset: -10%; z-index: -2; pointer-events: none;
    background:
      radial-gradient(40% 32% at 10% 6%, rgba(46,166,255,0.48), transparent 70%),
      radial-gradient(38% 30% at 92% 8%, rgba(123,92,255,0.44), transparent 70%),
      radial-gradient(42% 34% at 50% 50%, rgba(123,92,255,0.10), transparent 72%),
      radial-gradient(40% 34% at 48% 104%, rgba(255,149,0,0.30), transparent 70%),
      radial-gradient(32% 26% at 96% 84%, rgba(255,45,85,0.24), transparent 70%),
      radial-gradient(30% 24% at 2% 80%, rgba(255,213,74,0.24), transparent 70%);
    filter: blur(4px);
    animation: bgDrift 22s ease-in-out infinite alternate;
  }
  @keyframes bgDrift {
    0% { transform: translate(0, 0) scale(1); }
    100% { transform: translate(-2.5%, 3%) scale(1.07); }
  }
  .bg-grid {
    position: fixed; inset: 0; z-index: -1; pointer-events: none;
    background-image: radial-gradient(rgba(127,127,127,0.20) 1px, transparent 1px);
    background-size: 22px 22px;
    -webkit-mask-image: radial-gradient(85% 75% at 50% 0%, #000 35%, transparent 100%);
    mask-image: radial-gradient(85% 75% at 50% 0%, #000 35%, transparent 100%);
    opacity: 0.6;
  }
  .wrap { max-width: 560px; margin: 0 auto; padding: 0 14px; position: relative; z-index: 1; }

  /* ---- Top bar (sticky, web-style navbar) ---- */
  .topbar {
    position: sticky; top: 0; z-index: 30;
    background: color-mix(in srgb, var(--header-bg) 74%, transparent);
    backdrop-filter: blur(18px) saturate(1.4); -webkit-backdrop-filter: blur(18px) saturate(1.4);
    border-bottom: 1px solid rgba(127,127,127,0.14);
  }
  .topbar-inner {
    max-width: 560px; margin: 0 auto; padding: 12px 14px;
    display: flex; align-items: center; justify-content: space-between; gap: 10px;
  }
  .brand { display: flex; align-items: center; gap: 8px; font-weight: 800; font-size: 16px; }
  .brand .logo {
    width: 30px; height: 30px; border-radius: 9px; display: flex; align-items: center;
    justify-content: center; font-size: 16px; color: #fff; overflow: hidden;
    background: linear-gradient(135deg, var(--btn), #7b5cff 120%);
    box-shadow: 0 2px 8px rgba(0,0,0,0.25);
  }
  .brand .logo img { width: 100%; height: 100%; object-fit: cover; display: block; }
  .balance-chip {
    display: flex; align-items: center; gap: 6px; background: var(--secbg);
    border-radius: 999px; padding: 6px 12px 6px 6px; font-size: 13px; font-weight: 700;
    box-shadow: 0 2px 8px rgba(0,0,0,0.08); cursor: default;
  }
  .balance-chip .dot {
    width: 20px; height: 20px; border-radius: 999px; background: linear-gradient(135deg,#ffd54a,#ff9500);
    display: flex; align-items: center; justify-content: center; font-size: 11px;
  }
  .topbar-right { display: flex; align-items: center; gap: 8px; }
  .theme-toggle {
    width: 34px; height: 34px; border-radius: 999px; background: var(--secbg);
    display: flex; align-items: center; justify-content: center; font-size: 15px;
    cursor: pointer; box-shadow: 0 2px 8px rgba(0,0,0,0.08); flex-shrink: 0;
    border: none; transition: transform 0.15s ease;
  }
  .theme-toggle:active { transform: scale(0.88); }

  /* ---- Hero ---- */
  .hero {
    margin: 14px 0 16px; padding: 24px 18px 28px;
    background: linear-gradient(135deg, var(--btn), #7b5cff 120%);
    color: #fff; border-radius: 20px;
    box-shadow: 0 10px 26px rgba(0,0,0,0.16);
    position: relative; overflow: hidden;
  }
  .hero::after {
    content: ""; position: absolute; right: -30px; top: -30px; width: 140px; height: 140px;
    background: radial-gradient(circle, rgba(255,255,255,0.22), transparent 70%);
  }
  .hero h1 { font-size: 22px; margin: 0 0 6px; position: relative; }
  .hero p { font-size: 13px; margin: 0; opacity: 0.9; line-height: 1.5; position: relative; max-width: 90%; }
  .hero .stats { display: flex; gap: 10px; margin-top: 14px; position: relative; }
  .hero .stat {
    background: rgba(255,255,255,0.16); border-radius: 12px; padding: 8px 12px; font-size: 12px;
  }
  .hero .stat b { display: block; font-size: 15px; }

  /* ---- Tabs (sticky under topbar) ---- */
  .tabs-sticky { position: sticky; top: 58px; z-index: 20; background: var(--bg); padding: 2px 0 12px; margin-top: -2px; }
  .tabs { display: flex; gap: 8px; overflow-x: auto; padding: 2px 1px 4px; scrollbar-width: none; }
  .tabs::-webkit-scrollbar { display: none; }
  .tab {
    padding: 9px 16px; border-radius: 999px; background: var(--secbg);
    color: var(--text); font-size: 13.5px; white-space: nowrap; cursor: pointer;
    border: 1px solid rgba(127,127,127,0.14); font-weight: 600; transition: all 0.15s ease;
    box-shadow: 0 1px 4px rgba(0,0,0,0.04);
  }
  .tab.active { background: var(--btn); color: var(--btn-text); border-color: transparent; box-shadow: 0 4px 12px rgba(0,0,0,0.18); }
  .tab.jackpot.active { background: linear-gradient(135deg, #ff9500, #ff2d55); }

  /* ---- Section ---- */
  .section-title { font-size: 13px; font-weight: 700; color: var(--hint); margin: 4px 2px 10px; text-transform: uppercase; letter-spacing: 0.03em; }
  .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
  .card {
    background: color-mix(in srgb, var(--secbg) 88%, transparent);
    backdrop-filter: blur(10px); -webkit-backdrop-filter: blur(10px);
    border-radius: 18px; padding: 14px;
    display: flex; flex-direction: column; gap: 6px;
    box-shadow: 0 3px 14px rgba(0,0,0,0.07);
    border: 1px solid var(--card-border);
    border-top-color: var(--card-border-top);
    transition: transform 0.12s ease, box-shadow 0.12s ease;
    animation: fadeIn 0.25s ease both;
  }
  @keyframes fadeIn { from { opacity: 0; transform: translateY(6px); } to { opacity: 1; transform: none; } }
  .card:active { transform: scale(0.97); }
  .card.jackpot {
    background: linear-gradient(160deg, #2b1055, #4a1a7a);
    color: #fff; border: 1px solid rgba(255,193,7,0.4);
    box-shadow: 0 6px 18px rgba(122,26,122,0.35);
  }
  .card.jackpot .desc { color: #d8c8ff; }
  .card.jackpot .price { color: #ffd54a; }
  .card.jackpot.locked { opacity: 0.55; }
  .card .icon-tile {
    width: 42px; height: 42px; border-radius: 12px; font-size: 21px;
    display: flex; align-items: center; justify-content: center; margin-bottom: 2px;
    background: linear-gradient(135deg, rgba(46,166,255,0.15), rgba(123,92,255,0.15));
  }
  .card.jackpot .icon-tile { background: rgba(255,255,255,0.1); }
  .card .name { font-size: 14px; font-weight: 700; }
  .card .desc { font-size: 12px; color: var(--hint); min-height: 14px; line-height: 1.4; }
  .card .price { font-size: 13px; font-weight: 800; margin-top: 2px; }
  .card .price small { font-weight: 500; opacity: 0.7; }
  .card .btnrow { display: flex; flex-direction: column; gap: 6px; margin-top: 8px; }
  .card button {
    border: none; border-radius: 11px; padding: 10px 10px;
    background: var(--btn); color: var(--btn-text); font-size: 12.5px; font-weight: 700;
    cursor: pointer; transition: opacity 0.15s ease;
  }
  .card button.alt { background: rgba(127,127,127,0.14); color: var(--text); }
  .card.jackpot button.alt { background: rgba(255,255,255,0.14); color: #fff; }
  .card button.stars { background: linear-gradient(135deg,#ffd54a,#ff9500); color: #1a1a1a; }
  .card.jackpot button { background: linear-gradient(135deg, #ff9500, #ff2d55); color: #fff; }
  .card button:disabled { opacity: 0.5; cursor: default; }
  .card button:active { opacity: 0.8; }
  .lock-badge { font-size: 11px; font-weight: 700; color: #ffd54a; margin-top: 2px; }

  .card.promo {
    grid-column: 1 / -1;
    background: linear-gradient(160deg, #123a2e, #0d5c45);
    border: 1px solid rgba(70,255,190,0.35);
    box-shadow: 0 6px 18px rgba(20,150,110,0.3);
  }
  .card.promo .desc { color: #bdf5e2; }
  .card.promo .btnrow { flex-direction: row; gap: 8px; }
  .promo-input {
    flex: 1; min-width: 0; padding: 10px 12px; border-radius: 11px;
    border: 1px solid rgba(255,255,255,0.25); background: rgba(255,255,255,0.08);
    color: #fff; font-size: 13px; font-weight: 700; letter-spacing: 0.03em;
    text-transform: uppercase;
  }
  .promo-input::placeholder { color: rgba(255,255,255,0.5); text-transform: none; font-weight: 500; }
  .promo-input:disabled { opacity: 0.6; }
  .card.promo button { background: linear-gradient(135deg, #35e0a8, #12b886); color: #073526; white-space: nowrap; }

  .banner-card {
    grid-column: 1 / -1; border-radius: 20px; padding: 22px 18px; text-align: center;
    background: linear-gradient(160deg, #1c1c26, #33263f);
    color: #fff; box-shadow: 0 8px 20px rgba(0,0,0,0.18);
  }
  .banner-card .icon-tile { margin: 0 auto 10px; background: rgba(255,255,255,0.12); width: 52px; height: 52px; font-size: 26px; border-radius: 14px; }
  .banner-card h3 { margin: 0 0 6px; font-size: 16px; }
  .banner-card p { margin: 0 0 14px; font-size: 12.5px; color: #c9c3d6; line-height: 1.5; }
  .banner-card button { width: 100%; max-width: 260px; padding: 12px; border-radius: 12px; border: none; font-weight: 700; font-size: 13.5px; background: linear-gradient(135deg, var(--btn), #7b5cff); color: #fff; cursor: pointer; }

  .empty { grid-column: 1 / -1; color: var(--hint); text-align: center; padding: 44px 10px; font-size: 13.5px; }

  /* ---- Toast ---- */
  .toast {
    position: fixed; left: 14px; right: 14px; bottom: 18px;
    background: var(--secbg); color: var(--text); border-radius: 14px;
    padding: 13px 15px; font-size: 13px; text-align: center;
    box-shadow: 0 8px 26px rgba(0,0,0,0.22);
    transform: translateY(140%); transition: transform 0.28s ease; z-index: 50;
    max-width: 560px; margin: 0 auto;
  }
  .toast.show { transform: translateY(0); }

  /* ---- Modal ---- */
  .overlay {
    position: fixed; inset: 0; background: rgba(0,0,0,0.5); z-index: 60;
    display: flex; align-items: flex-end; justify-content: center;
    opacity: 0; pointer-events: none; transition: opacity 0.2s ease;
  }
  .overlay.show { opacity: 1; pointer-events: auto; }
  .modal {
    background: var(--bg); width: 100%; max-width: 560px; border-radius: 20px 20px 0 0;
    padding: 20px 18px 26px; transform: translateY(100%); transition: transform 0.25s ease;
    max-height: 82vh; overflow-y: auto;
  }
  .overlay.show .modal { transform: translateY(0); }
  .modal h2 { font-size: 16px; margin: 0 0 4px; }
  .modal .sub { font-size: 12.5px; color: var(--hint); margin-bottom: 14px; }
  .modal .card-num {
    background: var(--secbg); border-radius: 12px; padding: 12px 14px; font-size: 16px;
    font-weight: 800; letter-spacing: 0.04em; text-align: center; margin-bottom: 14px;
  }
  .modal label { font-size: 12.5px; font-weight: 700; color: var(--hint); display: block; margin: 12px 0 6px; }
  .modal input[type=file] {
    width: 100%; padding: 10px; border-radius: 10px; border: 1px dashed rgba(127,127,127,0.4);
    background: var(--secbg); color: var(--text); font-size: 12.5px;
  }
  .modal .actions { display: flex; gap: 10px; margin-top: 18px; }
  .modal .actions button {
    flex: 1; padding: 12px; border-radius: 12px; border: none; font-weight: 700; font-size: 13.5px; cursor: pointer;
  }
  .modal .actions .primary { background: var(--btn); color: var(--btn-text); }
  .modal .actions .secondary { background: rgba(127,127,127,0.14); color: var(--text); }
  .modal .result-body { font-size: 14px; line-height: 1.6; }
  .modal .about-body { max-height: 56vh; overflow-y: auto; padding-right: 2px; }
  .modal .about-body p { margin: 0 0 12px; }
  .modal .about-body p:last-child { margin-bottom: 0; }
  .modal .close-x { position: absolute; right: 16px; top: 14px; font-size: 18px; color: var(--hint); cursor: pointer; }

  /* ---- Jekpot raketa animatsiyasi ---- */
  .rocket-overlay {
    position: fixed; inset: 0; z-index: 80; display: flex; align-items: center; justify-content: center;
    background: radial-gradient(circle at 50% 30%, #241a45, #0a0714 75%);
    opacity: 0; pointer-events: none; transition: opacity 0.25s ease;
  }
  .rocket-overlay.show { opacity: 1; pointer-events: auto; }
  .rocket-overlay.shake { animation: rocketShake 0.5s ease; }
  @keyframes rocketShake {
    0%, 100% { transform: translate(0, 0); }
    20% { transform: translate(-8px, 4px); }
    40% { transform: translate(7px, -5px); }
    60% { transform: translate(-6px, -3px); }
    80% { transform: translate(5px, 4px); }
  }
  .rocket-stars {
    position: absolute; inset: 0;
    background-image:
      radial-gradient(1.5px 1.5px at 20% 30%, rgba(255,255,255,0.5), transparent),
      radial-gradient(1.5px 1.5px at 70% 15%, rgba(255,255,255,0.4), transparent),
      radial-gradient(1px 1px at 40% 70%, rgba(255,255,255,0.35), transparent),
      radial-gradient(1.5px 1.5px at 85% 55%, rgba(255,255,255,0.4), transparent),
      radial-gradient(1px 1px at 55% 85%, rgba(255,255,255,0.3), transparent),
      radial-gradient(1.5px 1.5px at 12% 60%, rgba(255,255,255,0.4), transparent),
      radial-gradient(1px 1px at 30% 45%, rgba(255,255,255,0.3), transparent),
      radial-gradient(1.5px 1.5px at 62% 78%, rgba(255,255,255,0.35), transparent),
      radial-gradient(1px 1px at 90% 88%, rgba(255,255,255,0.3), transparent);
    background-size: 100% 100%;
  }
  .rocket-overlay.show .rocket-stars { animation: starsTwinkle 2.6s ease-in-out infinite; }
  @keyframes starsTwinkle {
    0%, 100% { opacity: 0.75; }
    50% { opacity: 1; }
  }
  .rocket-glow {
    position: absolute; left: 50%; bottom: 0; width: 240px; height: 240px;
    transform: translateX(-50%); border-radius: 50%; pointer-events: none;
    background: radial-gradient(circle, rgba(123,92,255,0.35), transparent 70%);
    opacity: 0; transition: opacity 0.4s ease;
  }
  .rocket-overlay.show .rocket-glow { opacity: 1; }
  .rocket-flash {
    position: absolute; left: 50%; bottom: 20px; width: 20px; height: 20px;
    transform: translateX(-50%) scale(0); border-radius: 50%;
    background: radial-gradient(circle, #fff, #ffd54a 40%, transparent 72%);
    pointer-events: none; z-index: 1;
  }
  .rocket-flash.ignite { animation: flashPulse 0.5s ease-out; }
  @keyframes flashPulse {
    0% { transform: translateX(-50%) scale(0); opacity: 1; }
    60% { transform: translateX(-50%) scale(9); opacity: 0.55; }
    100% { transform: translateX(-50%) scale(13); opacity: 0; }
  }
  .rocket-track {
    position: relative; width: 100%; max-width: 340px; height: 78vh; max-height: 620px;
    margin: 0 auto;
  }
  .rocket-zone {
    position: absolute; left: 0; right: 0; display: flex; align-items: center; gap: 8px;
    font-size: 11px; font-weight: 700; color: rgba(255,255,255,0.45);
  }
  .rocket-zone::before {
    content: ""; flex: 1; height: 1px; background: rgba(255,255,255,0.14); border-style: dashed;
  }
  .rocket-zone.z1 { bottom: 8%; } .rocket-zone.z2 { bottom: 38%; }
  .rocket-zone.z3 { bottom: 68%; } .rocket-zone.z4 { bottom: 92%; }
  .rocket-trail {
    position: absolute; left: 50%; bottom: 40px; width: 4px; height: 0;
    transform: translateX(-50%); border-radius: 3px; z-index: 1;
    background: linear-gradient(to top, rgba(123,92,255,0.85), rgba(123,92,255,0.35) 55%, transparent);
    box-shadow: 0 0 10px rgba(123,92,255,0.55);
  }
  .rocket-el {
    position: absolute; left: 50%; bottom: 40px; font-size: 40px; line-height: 1;
    transform: translateX(-50%); filter: drop-shadow(0 0 10px rgba(123,92,255,0.6));
    z-index: 2;
  }
  .rocket-el.flying { animation: rocketTilt 2.1s ease-in-out; }
  @keyframes rocketTilt {
    0%   { transform: translateX(-50%) rotate(0deg); }
    22%  { transform: translateX(-50%) rotate(-7deg); }
    48%  { transform: translateX(-50%) rotate(5deg); }
    74%  { transform: translateX(-50%) rotate(-3deg); }
    100% { transform: translateX(-50%) rotate(0deg); }
  }
  .rocket-flame {
    position: absolute; left: 50%; bottom: 12px; transform: translateX(-50%); font-size: 20px;
    opacity: 0.9; animation: flameFlicker 0.12s infinite alternate; z-index: 1;
    filter: drop-shadow(0 0 8px rgba(255,149,0,0.7));
  }
  @keyframes flameFlicker { from { transform: translateX(-50%) scale(1); } to { transform: translateX(-50%) scale(0.82) translateY(2px); } }
  .rocket-particle {
    position: absolute; font-size: 20px; pointer-events: none; z-index: 3;
    animation: particleBurst 0.85s ease-out forwards;
  }
  @keyframes particleBurst {
    0% { transform: translate(0,0) rotate(0deg) scale(1); opacity: 1; }
    100% { transform: translate(var(--px), var(--py)) rotate(180deg) scale(0.4); opacity: 0; }
  }
  .rocket-spark {
    position: absolute; font-size: 11px; pointer-events: none; z-index: 1; opacity: 0.85;
    animation: sparkDrift 0.6s ease-out forwards;
  }
  @keyframes sparkDrift {
    0% { transform: translate(0,0) scale(1); opacity: 0.85; }
    100% { transform: translate(var(--px), var(--py)) scale(0.3); opacity: 0; }
  }
  .rocket-caption {
    position: absolute; top: 16px; left: 0; right: 0; text-align: center;
    color: #fff; font-size: 14px; font-weight: 700; letter-spacing: 0.02em;
  }
  .rocket-caption small { display: block; font-weight: 500; opacity: 0.6; font-size: 11.5px; margin-top: 3px; }

  /* ---- Pages / bottom nav ---- */
  body { padding-bottom: 84px; }
  .page { display: none; }
  .page.active { display: block; animation: fadeIn 0.2s ease both; }

  .bottom-nav {
    position: fixed; left: 0; right: 0; bottom: 0; z-index: 40;
    background: color-mix(in srgb, var(--header-bg) 78%, transparent);
    backdrop-filter: blur(18px) saturate(1.4); -webkit-backdrop-filter: blur(18px) saturate(1.4);
    box-shadow: 0 -4px 20px rgba(0,0,0,0.06);
    border-top: 1px solid rgba(127,127,127,0.14);
    display: flex; padding: 6px 4px calc(6px + env(safe-area-inset-bottom, 0px));
    max-width: 560px; margin: 0 auto; left: 50%; transform: translateX(-50%); width: 100%;
  }
  .nav-item {
    flex: 1; display: flex; flex-direction: column; align-items: center; gap: 2px;
    padding: 6px 2px; border-radius: 12px; font-size: 10.5px; font-weight: 700;
    color: var(--hint); cursor: pointer; transition: color 0.15s ease;
  }
  .nav-item .ic { font-size: 20px; line-height: 1; }
  .nav-item.active { color: var(--btn); }

  .profile-hero { text-align: center; padding: 14px 0 20px; }
  .avatar {
    width: 68px; height: 68px; border-radius: 50%; margin: 0 auto 10px; font-size: 26px; font-weight: 800;
    display: flex; align-items: center; justify-content: center; color: #fff;
    background: linear-gradient(135deg, var(--btn), #7b5cff 120%); box-shadow: 0 6px 18px rgba(0,0,0,0.18);
  }
  .p-name { font-size: 16px; font-weight: 800; margin-bottom: 6px; }
  .p-balance { font-size: 26px; font-weight: 800; color: var(--btn); }
  .p-refs { font-size: 12.5px; color: var(--hint); margin-top: 4px; }
  .reflink-box {
    display: flex; align-items: center; gap: 8px; background: var(--secbg); border-radius: 12px;
    padding: 12px 12px; margin-bottom: 10px; box-shadow: 0 2px 8px rgba(0,0,0,0.05);
  }
  .reflink-box code { flex: 1; font-size: 11.5px; overflow-x: auto; white-space: nowrap; color: var(--text); }
  .reflink-box button {
    border: none; border-radius: 9px; padding: 8px 12px; background: var(--btn); color: var(--btn-text);
    font-size: 12px; font-weight: 700; cursor: pointer; flex-shrink: 0;
  }
  .wide-btn {
    width: 100%; border: none; border-radius: 13px; padding: 13px; margin-bottom: 8px;
    background: linear-gradient(135deg, var(--btn), #7b5cff 120%); color: #fff;
    font-size: 14px; font-weight: 700; cursor: pointer;
  }
  .wide-btn:disabled { opacity: 0.5; }
  .hint-p { font-size: 12px; color: var(--hint); line-height: 1.5; text-align: center; padding: 0 8px; }

  .withdraw-card { background: var(--secbg); border-radius: 16px; padding: 16px; margin-bottom: 16px; box-shadow: 0 2px 10px rgba(0,0,0,0.06); }
  .wc-row { display: flex; justify-content: space-between; align-items: center; font-size: 13.5px; padding: 6px 0; }
  .wc-note { font-size: 12px; color: var(--hint); margin: 6px 0 12px; }
  .gift-list { display: flex; flex-direction: column; gap: 8px; }
  .gift-row {
    display: flex; align-items: center; justify-content: space-between; gap: 10px;
    background: var(--secbg); border-radius: 12px; padding: 12px 14px; box-shadow: 0 2px 8px rgba(0,0,0,0.05);
  }
  .gift-row .gname { font-size: 13px; font-weight: 700; }
  .gift-row .gprice { font-size: 12px; color: var(--hint); }
  .gift-row button {
    border: none; border-radius: 9px; padding: 8px 12px; background: var(--btn); color: var(--btn-text);
    font-size: 12px; font-weight: 700; cursor: pointer; flex-shrink: 0;
  }

  .contacts-list { display: flex; flex-direction: column; gap: 8px; }
  .contact-row {
    display: flex; align-items: center; gap: 12px; background: var(--secbg); border-radius: 14px;
    padding: 13px 14px; box-shadow: 0 2px 8px rgba(0,0,0,0.05); cursor: pointer; text-decoration: none; color: var(--text);
  }
  .contact-row .c-ic {
    width: 36px; height: 36px; border-radius: 10px; display: flex; align-items: center; justify-content: center;
    background: linear-gradient(135deg, rgba(46,166,255,0.15), rgba(123,92,255,0.15)); font-size: 17px;
  }
  .contact-row .c-label { font-size: 13.5px; font-weight: 700; }
  .contact-row .c-user { font-size: 11.5px; color: var(--hint); }
</style>
</head>
<body>
  <div class="bg-decor"></div>
  <div class="bg-grid"></div>
  <div class="topbar">
    <div class="topbar-inner">
      <div class="brand"><div class="logo"><img src="__LOGO_DATA_URI__" alt="logo"></div>Stars Bot</div>
      <div class="topbar-right">
        <div class="balance-chip" id="balanceChip"><div class="dot">⭐</div><span id="balanceVal">—</span></div>
        <button class="theme-toggle" id="aboutToggle" type="button" aria-label="Bot haqida">ℹ️</button>
        <button class="theme-toggle" id="themeToggle" type="button" aria-label="Mavzuni almashtirish">🌙</button>
      </div>
    </div>
  </div>

  <div class="wrap">
    <div class="page active" id="page-shop">
      <div class="hero">
        <h1>Xush kelibsiz! ✨</h1>
        <p>Telegram Stars yoki bot balansingiz bilan sovg'alar, premium va Jekpot boxlarni sotib oling — barchasi shu yerda.</p>
        <div class="stats">
          <div class="stat"><b id="statBalance">0</b>⭐ balans</div>
          <div class="stat"><b id="statRefs">0</b>referal</div>
        </div>
      </div>

      <div class="tabs-sticky">
        <div class="tabs" id="tabs"></div>
      </div>
      <div class="grid" id="grid"></div>
    </div>

    <div class="page" id="page-profile">
      <div class="profile-hero">
        <div class="avatar" id="profAvatar">?</div>
        <div class="p-name" id="profName">—</div>
        <div class="p-balance"><span id="profBalance">0</span> ⭐</div>
        <div class="p-refs"><b id="profRefs">0</b> ta do'st taklif qilingan</div>
      </div>
      <div class="section-title">Referal havolangiz</div>
      <div class="reflink-box">
        <code id="refLinkText">—</code>
        <button id="refCopyBtn">Nusxalash</button>
      </div>
      <button class="wide-btn" id="refShareBtn">📤 Do'stlarga ulashish</button>
      <p class="hint-p">Har bir yangi a'zo botga qo'shilib, majburiy kanallarga a'zo bo'lganda balansingizga bonus qo'shiladi.</p>
    </div>

    <div class="page" id="page-withdraw">
      <div class="section-title">Yulduz yechish</div>
      <div class="withdraw-card">
        <div class="wc-row"><span>Balansingiz</span><b id="wdBalance">0 ⭐</b></div>
        <div class="wc-row"><span>Minimal chegara</span><b id="wdMin">— ⭐</b></div>
        <div class="wc-note" id="wdNote"></div>
        <button class="wide-btn" id="wdStarsBtn">⭐ Yulduz sifatida yechish</button>
      </div>
      <div class="section-title">Yoki gift sifatida yeching</div>
      <div id="wdGiftList" class="gift-list"></div>
    </div>

    <div class="page" id="page-contacts">
      <div class="section-title">Aloqa</div>
      <div id="contactsList" class="contacts-list"></div>
    </div>

    <div class="page" id="page-reviews">
      <div class="section-title">Otziv</div>
      <div class="banner-card" id="reviewsBanner">
        <div class="icon-tile">⭐</div>
        <h3>Otziv kanali</h3>
        <p id="reviewsText">Yuklanmoqda...</p>
      </div>
    </div>
  </div>

  <div class="bottom-nav" id="bottomNav">
    <div class="nav-item active" data-page="shop"><div class="ic">🛍️</div>Do'kon</div>
    <div class="nav-item" data-page="profile"><div class="ic">👤</div>Profil</div>
    <div class="nav-item" data-page="withdraw"><div class="ic">💸</div>Yechish</div>
    <div class="nav-item" data-page="contacts"><div class="ic">📞</div>Aloqa</div>
    <div class="nav-item" data-page="reviews"><div class="ic">⭐</div>Otziv</div>
  </div>

  <div class="toast" id="toast"></div>

  <div class="overlay" id="uzsOverlay">
    <div class="modal">
      <span class="close-x" id="uzsClose">✕</span>
      <h2 id="uzsTitle">💳 Karta orqali to'lov</h2>
      <div class="sub" id="uzsSub"></div>
      <div class="card-num" id="uzsCard"></div>
      <label>To'lov chekini (screenshot) yuklang</label>
      <input type="file" id="uzsFile" accept="image/*">
      <div class="actions">
        <button class="secondary" id="uzsCancel">Bekor qilish</button>
        <button class="primary" id="uzsSubmit">Yuborish</button>
      </div>
    </div>
  </div>

  <div class="overlay" id="resultOverlay">
    <div class="modal">
      <span class="close-x" id="resultClose">✕</span>
      <div class="result-body" id="resultBody"></div>
      <div class="actions" id="resultClaimActions" style="display:none;"></div>
      <div class="actions" id="resultOkRow">
        <button class="primary" id="resultOk">Tushunarli</button>
      </div>
    </div>
  </div>

  <div class="overlay" id="aboutOverlay">
    <div class="modal">
      <span class="close-x" id="aboutClose">✕</span>
      <h2>ℹ️ Bot haqida — qanday ishlaydi?</h2>
      <div class="result-body about-body" id="aboutBody">Yuklanmoqda...</div>
      <div class="actions">
        <button class="primary" id="aboutOk">Tushunarli</button>
      </div>
    </div>
  </div>

  <div class="rocket-overlay" id="rocketOverlay">
    <div class="rocket-stars"></div>
    <div class="rocket-caption" id="rocketCaption">🚀 Uchmoqda...<small>Qancha baland — mukofot shuncha katta!</small></div>
    <div class="rocket-track" id="rocketTrack">
      <div class="rocket-zone z1">Kichik</div>
      <div class="rocket-zone z2">O'rta</div>
      <div class="rocket-zone z3">Katta</div>
      <div class="rocket-zone z4">MEGA 🎆</div>
      <div class="rocket-glow" id="rocketGlow"></div>
      <div class="rocket-trail" id="rocketTrail"></div>
      <div class="rocket-flash" id="rocketFlash"></div>
      <div class="rocket-flame" id="rocketFlame">🔥</div>
      <div class="rocket-el" id="rocketEl">🚀</div>
    </div>
  </div>

<script>
const tg = window.Telegram && window.Telegram.WebApp;
if (tg) { tg.ready(); tg.expand(); if (tg.setHeaderColor) { try { tg.setHeaderColor('secondary_bg_color'); } catch(e){} } }
const INIT_DATA = tg ? (tg.initData || '') : '';

// ---- Dark / Light mavzu ----
const THEME_KEY = 'starsbot_theme';
function detectDefaultTheme() {
  if (tg && tg.colorScheme) return tg.colorScheme; // Telegram'ning o'z mavzusi ('light' | 'dark')
  return (window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches) ? 'dark' : 'light';
}
function applyTheme(theme) {
  document.documentElement.setAttribute('data-theme', theme);
  const btn = document.getElementById('themeToggle');
  if (btn) btn.textContent = theme === 'dark' ? '☀️' : '🌙';
}
function loadTheme() {
  let stored = null;
  try { stored = localStorage.getItem(THEME_KEY); } catch (e) {}
  applyTheme(stored || detectDefaultTheme());
}
loadTheme();
document.getElementById('themeToggle').onclick = () => {
  const cur = document.documentElement.getAttribute('data-theme') === 'dark' ? 'dark' : 'light';
  const next = cur === 'dark' ? 'light' : 'dark';
  applyTheme(next);
  try { localStorage.setItem(THEME_KEY, next); } catch (e) {}
};
if (tg && tg.onEvent) {
  try {
    tg.onEvent('themeChanged', () => {
      let stored = null;
      try { stored = localStorage.getItem(THEME_KEY); } catch (e) {}
      if (!stored) applyTheme(detectDefaultTheme()); // faqat foydalanuvchi qo'lda tanlamagan bo'lsa
    });
  } catch (e) {}
}

// ---- "Bot haqida" ma'lumot oynasi ----
function renderAbout() {
  const body = document.getElementById('aboutBody');
  body.innerHTML = `
    <p><b>⭐ Yulduz qanday topiladi?</b><br>
    Do'stlaringizni referal havolangiz orqali taklif qiling — har biri uchun
    <b>${INFO.ref_reward_stars || 0} ⭐</b> olasiz. Shuningdek 🎰 Jekpot boxlarini oching —
    tasodifiy miqdorda ⭐ yoki gift yutib olasiz.</p>
    <p><b>🛍️ Do'kon</b><br>
    Gift, Premium va boshqa mahsulotlarni 3 xil usulda sotib olish mumkin: ichki ⭐
    balansingizdan, haqiqiy Telegram Stars'dan, yoki karta (UZS) orqali.</p>
    <p><b>🎰 Jekpot (boxlar)</b><br>
    Box ochilganda raketa uchadi — qancha baland uchsa, mukofot shuncha katta. Gift
    yutib olsangiz, uni <b>saqlab qo'yasiz</b>: keyin xohlaganingizda "🎁 Giftni olish"
    (haqiqiy sovg'a) yoki "⭐ ga aylantirish" (ichki balansga qo'shish) tugmalaridan
    birini bosasiz — shoshilish shart emas.</p>
    <p><b>💸 Yulduz yechish</b><br>
    Balansingiz kamida <b>${INFO.min_withdraw_stars || 0} ⭐</b> bo'lsa, ⭐ (real to'lov)
    yoki gift sifatida yechib olishingiz mumkin. Ba'zi giftlar avtomatik yuboriladi,
    qolganlari bot egasi tomonidan qo'lda.</p>
    <p><b>🌙 Dark/Light</b><br>
    Tepadagi 🌙/☀️ tugmasi bilan mavzuni istalgan vaqt almashtirishingiz mumkin —
    tanlovingiz eslab qolinadi.</p>
    <p>❓ Savol bo'lsa — 📞 Aloqa bo'limidan yozing (bot chatida ham mavjud).</p>
  `;
}
document.getElementById('aboutToggle').onclick = () => { renderAbout(); openOverlay('aboutOverlay'); };
document.getElementById('aboutClose').onclick = () => closeOverlay('aboutOverlay');
document.getElementById('aboutOk').onclick = () => closeOverlay('aboutOverlay');

const TABS = [
  { key: 'gift', label: '🎁 Gift' },
  { key: 'premium', label: '💎 Premium' },
  { key: 'star', label: '⭐ Yulduz' },
  { key: 'box', label: '🎰 Jekpot', jackpot: true },
  { key: 'promo', label: '🎟 Promokod' },
  { key: 'nft', label: '🖼 NFT' },
];

let SHOP = { items: [], boxes: [], shop_promos: [], nft: {}, settings: {} };
let USER = null;
let ACTIVE_CAT = 'gift';

function toast(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), 2800);
}

function openOverlay(id) { document.getElementById(id).classList.add('show'); }
function closeOverlay(id) { document.getElementById(id).classList.remove('show'); }

function showResult(html, claim) {
  document.getElementById('resultBody').innerHTML = html;
  const actionsEl = document.getElementById('resultClaimActions');
  const okRow = document.getElementById('resultOkRow');
  actionsEl.innerHTML = '';
  if (claim && claim.claim_id) {
    okRow.style.display = 'none';
    actionsEl.style.display = 'flex';
    const realBtn = document.createElement('button');
    realBtn.className = 'primary';
    realBtn.textContent = '🎁 Giftni olish';
    const starsBtn = document.createElement('button');
    starsBtn.className = 'secondary';
    starsBtn.textContent = `⭐ ${claim.gift_price_stars} ⭐ ga aylantirish`;
    const choose = async (action, btn) => {
      [realBtn, starsBtn].forEach(b => b.disabled = true);
      const old = btn.textContent;
      btn.textContent = 'Yuborilmoqda...';
      try {
        const res = await fetch('/api/gift_claim', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ init_data: INIT_DATA, action, claim_id: claim.claim_id }),
        });
        const data = await res.json();
        if (!res.ok || !data.ok) {
          toast(data.error || 'Xatolik yuz berdi');
          [realBtn, starsBtn].forEach(b => b.disabled = false);
          btn.textContent = old;
          return;
        }
        actionsEl.style.display = 'none';
        okRow.style.display = 'flex';
        document.getElementById('resultBody').innerHTML = data.message;
        await refreshMe();
      } catch (e) {
        toast('Tarmoq xatosi, qayta urinib ko\\'ring');
        [realBtn, starsBtn].forEach(b => b.disabled = false);
        btn.textContent = old;
      }
    };
    realBtn.onclick = () => choose('real', realBtn);
    starsBtn.onclick = () => choose('stars', starsBtn);
    actionsEl.appendChild(realBtn);
    actionsEl.appendChild(starsBtn);
  } else {
    actionsEl.style.display = 'none';
    okRow.style.display = 'flex';
  }
  openOverlay('resultOverlay');
}
document.getElementById('resultClose').onclick = () => closeOverlay('resultOverlay');
document.getElementById('resultOk').onclick = () => closeOverlay('resultOverlay');

function renderTabs() {
  const tabsEl = document.getElementById('tabs');
  tabsEl.innerHTML = '';
  TABS.forEach(cat => {
    const el = document.createElement('div');
    el.className = 'tab' + (cat.jackpot ? ' jackpot' : '') + (cat.key === ACTIVE_CAT ? ' active' : '');
    el.textContent = cat.label;
    el.onclick = () => { ACTIVE_CAT = cat.key; renderTabs(); renderGrid(); };
    tabsEl.appendChild(el);
  });
}

function priceLine(item) {
  const parts = [];
  if (item.price_stars > 0) parts.push(`${item.price_stars} ⭐`);
  if (item.price_uzs > 0) parts.push(`${item.price_uzs.toLocaleString('ru-RU')} so'm`);
  return parts.join(' <small>yoki</small> ');
}

function itemCard(item) {
  const card = document.createElement('div');
  card.className = 'card';
  const icon = item.cat === 'premium' ? '💎' : (item.cat === 'star' ? '⭐' : '🎁');
  let buttons = '';
  if (item.price_stars > 0) {
    buttons += `<button data-act="balance">⭐ Balansdan (${item.price_stars})</button>`;
    buttons += `<button data-act="tgstars" class="stars">✨ Stars bilan</button>`;
  }
  if (item.price_uzs > 0) {
    buttons += `<button data-act="uzs" class="alt">💳 Kartadan (${item.price_uzs.toLocaleString('ru-RU')} so'm)</button>`;
  }
  card.innerHTML = `
    <div class="icon-tile">${icon}</div>
    <div class="name">${item.name}</div>
    <div class="desc">${item.desc || ''}</div>
    <div class="price">${priceLine(item)}</div>
    <div class="btnrow">${buttons}</div>
  `;
  card.querySelectorAll('button').forEach(btn => {
    btn.onclick = () => {
      const act = btn.dataset.act;
      if (act === 'balance') buyBalance('item', item.id, btn);
      else if (act === 'tgstars') buyStars('item', item.id, btn);
      else if (act === 'uzs') openUzsModal(item);
    };
  });
  return card;
}

function boxCard(box) {
  const card = document.createElement('div');
  card.className = 'card jackpot' + (box.locked ? ' locked' : '');
  let buttons = '';
  if (!box.locked) {
    buttons += `<button data-act="balance">⭐ Ochish (${box.cost})</button>`;
    if (box.cost_tgstars > 0) buttons += `<button data-act="tgstars" class="stars">✨ Stars (${box.cost_tgstars})</button>`;
  }
  card.innerHTML = `
    <div class="icon-tile">🎰</div>
    <div class="name">${box.name}</div>
    <div class="desc">${box.desc || ''}</div>
    <div class="price">${box.cost} ⭐${box.cost_tgstars > 0 ? ` <small>yoki</small> ${box.cost_tgstars} 💫` : ''}</div>
    ${box.locked ? '<div class="lock-badge">✅ Bugun ishlatilgan — ertaga qayta oching</div>' : `<div class="btnrow">${buttons}</div>`}
  `;
  card.querySelectorAll('button').forEach(btn => {
    btn.onclick = () => {
      const act = btn.dataset.act;
      if (act === 'balance') buyBalance('box', box.id, btn);
      else if (act === 'tgstars') buyStars('box', box.id, btn);
    };
  });
  return card;
}

function promoCard() {
  const card = document.createElement('div');
  card.className = 'card jackpot promo';
  card.innerHTML = `
    <div class="icon-tile">🎟️</div>
    <div class="name">Promokod box</div>
    <div class="desc">Admin bergan promokodni kiriting — bepul ⭐ yutib oling!</div>
    <div class="btnrow">
      <input type="text" class="promo-input" placeholder="Promokodni kiriting" autocapitalize="characters">
      <button data-act="redeem">🎁 Ishlatish</button>
    </div>
  `;
  const input = card.querySelector('.promo-input');
  const btn = card.querySelector('button');
  const submit = () => redeemPromo(input.value, btn, input);
  btn.onclick = submit;
  input.onkeydown = (e) => { if (e.key === 'Enter') submit(); };
  return card;
}

function shopPromoCard(p) {
  const card = document.createElement('div');
  card.className = 'card jackpot promo';
  card.innerHTML = `
    <div class="icon-tile">🎟</div>
    <div class="name">${p.name}</div>
    <div class="desc">${p.desc || ''}</div>
    <div class="price">${p.price_stars} ⭐</div>
    <div class="btnrow"><button>💳 Sotib olish</button></div>
  `;
  card.querySelector('button').onclick = (e) => buyShopPromo(p.id, e.target);
  return card;
}

function nftCard() {
  const card = document.createElement('div');
  card.className = 'banner-card';
  const hasGroup = !!SHOP.nft.group_url;
  card.innerHTML = `
    <div class="icon-tile">🖼</div>
    <h3>NFT bozori</h3>
    <p>${hasGroup ? "NFT'lar bot ichida emas, maxsus guruhda sotiladi. Tugmani bosib guruhga o'ting." : "NFT guruhi hali sozlanmagan."}</p>
    ${hasGroup ? '<button id="nftGoBtn">Guruhga o\\'tish</button>' : ''}
  `;
  if (hasGroup) {
    card.querySelector('#nftGoBtn').onclick = () => {
      if (tg && tg.openTelegramLink && SHOP.nft.group_url.includes('t.me')) {
        tg.openTelegramLink(SHOP.nft.group_url);
      } else {
        window.open(SHOP.nft.group_url, '_blank');
      }
    };
  }
  return card;
}

function renderGrid() {
  const grid = document.getElementById('grid');
  grid.innerHTML = '';

  if (ACTIVE_CAT === 'nft') {
    grid.appendChild(nftCard());
    return;
  }
  if (ACTIVE_CAT === 'box') {
    grid.appendChild(promoCard());
    SHOP.boxes.forEach(b => grid.appendChild(boxCard(b)));
    return;
  }
  if (ACTIVE_CAT === 'promo') {
    grid.appendChild(promoCard());
    if (SHOP.shop_promos.length) {
      SHOP.shop_promos.forEach(p => grid.appendChild(shopPromoCard(p)));
    } else {
      const empty = document.createElement('div');
      empty.className = 'empty';
      empty.textContent = 'Hozircha sotuvda promokod yo\\'q.';
      grid.appendChild(empty);
    }
    return;
  }
  const items = SHOP.items.filter(i => i.cat === ACTIVE_CAT);
  if (!items.length) {
    const empty = document.createElement('div');
    empty.className = 'empty';
    empty.textContent = 'Bu bo\\'limda hozircha mahsulot yo\\'q.';
    grid.appendChild(empty);
    return;
  }
  items.forEach(i => grid.appendChild(itemCard(i)));
}

function updateHeader() {
  const balEl = document.getElementById('balanceVal');
  const statBal = document.getElementById('statBalance');
  const statRefs = document.getElementById('statRefs');
  if (USER) {
    balEl.textContent = USER.balance_stars;
    statBal.textContent = USER.balance_stars;
    statRefs.textContent = USER.referals_count;
  } else {
    balEl.textContent = '—';
  }
  renderProfile();
  renderWithdraw();
}

async function refreshMe() {
  if (!INIT_DATA) { USER = null; updateHeader(); return; }
  try {
    const res = await fetch('/api/me', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ init_data: INIT_DATA }),
    });
    const data = await res.json();
    USER = res.ok ? data : null;
  } catch (e) { USER = null; }
  updateHeader();
}

async function refreshShop() {
  const res = await fetch('/api/shop');
  const data = await res.json();
  SHOP = data;
  if (USER) {
    SHOP.boxes = SHOP.boxes.map(b => ({
      ...b,
      locked: !!(b.once_per_day && USER.last_daily_box === data.server_date),
    }));
  }
  renderGrid();
}

function spawnParticles(container, bottomPx, big) {
  const count = big ? 22 : 14;
  const emojis = big ? ['✨', '🎆', '⭐', '🟡', '💥'] : ['✨', '💥', '⭐'];
  for (let i = 0; i < count; i++) {
    const p = document.createElement('div');
    p.className = 'rocket-particle';
    p.textContent = emojis[Math.floor(Math.random() * emojis.length)];
    const angle = (Math.PI * 2 * i) / count + Math.random() * 0.5;
    const dist = 60 + Math.random() * (big ? 120 : 70);
    p.style.setProperty('--px', `${Math.cos(angle) * dist}px`);
    p.style.setProperty('--py', `${Math.sin(angle) * dist}px`);
    p.style.left = '50%';
    p.style.bottom = bottomPx + 'px';
    p.style.fontSize = (big ? 16 + Math.random() * 14 : 14 + Math.random() * 8) + 'px';
    container.appendChild(p);
    setTimeout(() => p.remove(), 900);
  }
}

function spawnSpark(container, bottomPx) {
  const s = document.createElement('div');
  s.className = 'rocket-spark';
  s.textContent = ['✨', '·', '⋆'][Math.floor(Math.random() * 3)];
  const angle = Math.PI / 2 + (Math.random() - 0.5) * 1.4;
  const dist = 10 + Math.random() * 24;
  s.style.setProperty('--px', `${Math.cos(angle) * dist}px`);
  s.style.setProperty('--py', `${Math.sin(angle) * dist}px`);
  s.style.left = (50 + (Math.random() - 0.5) * 8) + '%';
  s.style.bottom = bottomPx + 'px';
  container.appendChild(s);
  setTimeout(() => s.remove(), 650);
}

function launchRocket(ratio, kind) {
  return new Promise(resolve => {
    const overlay = document.getElementById('rocketOverlay');
    const track = document.getElementById('rocketTrack');
    const rocket = document.getElementById('rocketEl');
    const flame = document.getElementById('rocketFlame');
    const trail = document.getElementById('rocketTrail');
    const flash = document.getElementById('rocketFlash');
    const caption = document.getElementById('rocketCaption');
    const isGift = kind === 'gifts';

    rocket.style.transition = 'none';
    rocket.style.bottom = '40px';
    rocket.textContent = '🚀';
    rocket.style.fontSize = '40px';
    rocket.classList.remove('flying');
    trail.style.transition = 'none';
    trail.style.height = '0px';
    trail.style.opacity = '1';
    flash.classList.remove('ignite');
    flame.style.display = '';
    caption.innerHTML = "🚀 Uchmoqda...<small>Qancha baland — mukofot shuncha katta!</small>";
    overlay.classList.add('show');

    const trackH = track.clientHeight;
    const targetBottom = 40 + ratio * (trackH - 110);
    const climbHeight = targetBottom - 40;

    let sparkTimer = null;

    requestAnimationFrame(() => {
      flash.classList.add('ignite');
      requestAnimationFrame(() => {
        rocket.classList.add('flying');
        rocket.style.transition = 'bottom 2.1s cubic-bezier(.13,.75,.28,1)';
        rocket.style.bottom = targetBottom + 'px';
        trail.style.transition = 'height 2.1s cubic-bezier(.13,.75,.28,1)';
        trail.style.height = climbHeight + 'px';
        sparkTimer = setInterval(() => {
          const cur = parseFloat(getComputedStyle(rocket).bottom) || 40;
          spawnSpark(track, Math.max(40, cur - 4));
        }, 110);
      });
    });

    setTimeout(() => {
      if (sparkTimer) clearInterval(sparkTimer);
      flame.style.display = 'none';
      rocket.classList.remove('flying');
      trail.style.transition = 'opacity 0.4s ease';
      trail.style.opacity = '0';
      rocket.textContent = isGift ? '🎆' : '💥';
      rocket.style.fontSize = isGift ? '58px' : '46px';
      overlay.classList.add('shake');
      spawnParticles(track, targetBottom, isGift || ratio > 0.7);
      caption.innerHTML = isGift
        ? "🎇 MEGA YUTUQ!<small>Portladi — natija hozir ko'rinadi</small>"
        : "💥 Raketa to'xtadi!<small>Natija hozir ko'rinadi</small>";
      setTimeout(() => {
        overlay.classList.remove('shake');
      }, 500);
      setTimeout(() => {
        overlay.classList.remove('show');
        resolve();
      }, 950);
    }, 2150);
  });
}

async function buyBalance(kind, id, btnEl) {
  if (!INIT_DATA) { toast('Bu amal uchun botni Telegram ilovasi ichidan oching'); return; }
  const row = btnEl.parentElement;
  row.querySelectorAll('button').forEach(b => b.disabled = true);
  const oldText = btnEl.textContent;
  btnEl.textContent = 'Kutilmoqda...';
  try {
    const res = await fetch('/api/buy_balance', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ init_data: INIT_DATA, kind, id }),
    });
    const data = await res.json();
    if (!res.ok || !data.ok) {
      toast(data.error || 'Xatolik yuz berdi');
      row.querySelectorAll('button').forEach(b => b.disabled = false);
      btnEl.textContent = oldText;
      return;
    }
    if (kind === 'box' && typeof data.ratio === 'number') {
      await launchRocket(data.ratio, data.kind);
    }
    showResult(data.message, data.claim_id ? { claim_id: data.claim_id, gift_price_stars: data.gift_price_stars } : null);
    await refreshMe();
    await refreshShop();
  } catch (e) {
    toast('Tarmoq xatosi, qayta urinib ko\\'ring');
    row.querySelectorAll('button').forEach(b => b.disabled = false);
    btnEl.textContent = oldText;
  }
}

async function redeemPromo(code, btnEl, inputEl) {
  if (!INIT_DATA) { toast('Bu amal uchun botni Telegram ilovasi ichidan oching'); return; }
  code = (code || '').trim();
  if (!code) { toast('Promokodni kiriting!'); return; }
  btnEl.disabled = true;
  inputEl.disabled = true;
  const oldText = btnEl.textContent;
  btnEl.textContent = 'Tekshirilmoqda...';
  try {
    const res = await fetch('/api/redeem_promo', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ init_data: INIT_DATA, code }),
    });
    const data = await res.json();
    if (!res.ok || !data.ok) {
      toast(data.error || 'Xatolik yuz berdi');
      btnEl.disabled = false;
      inputEl.disabled = false;
      btnEl.textContent = oldText;
      return;
    }
    if (typeof data.ratio === 'number') {
      await launchRocket(data.ratio, data.kind);
    }
    showResult(data.message, data.claim_id ? { claim_id: data.claim_id, gift_price_stars: data.gift_price_stars } : null);
    inputEl.value = '';
    await refreshMe();
    await refreshShop();
  } catch (e) {
    toast('Tarmoq xatosi, qayta urinib ko\\'ring');
  } finally {
    btnEl.disabled = false;
    inputEl.disabled = false;
    btnEl.textContent = oldText;
  }
}

async function buyShopPromo(id, btnEl) {
  if (!INIT_DATA) { toast('Bu amal uchun botni Telegram ilovasi ichidan oching'); return; }
  const row = btnEl.parentElement;
  row.querySelectorAll('button').forEach(b => b.disabled = true);
  const oldText = btnEl.textContent;
  btnEl.textContent = 'Kutilmoqda...';
  try {
    const res = await fetch('/api/buy_promo', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ init_data: INIT_DATA, id }),
    });
    const data = await res.json();
    if (!res.ok || !data.ok) {
      toast(data.error || 'Xatolik yuz berdi');
      row.querySelectorAll('button').forEach(b => b.disabled = false);
      btnEl.textContent = oldText;
      return;
    }
    if (typeof data.ratio === 'number') {
      await launchRocket(data.ratio, data.kind);
    }
    showResult(data.message, data.claim_id ? { claim_id: data.claim_id, gift_price_stars: data.gift_price_stars } : null);
    await refreshMe();
    await refreshShop();
  } catch (e) {
    toast('Tarmoq xatosi, qayta urinib ko\\'ring');
    row.querySelectorAll('button').forEach(b => b.disabled = false);
    btnEl.textContent = oldText;
  }
}

async function buyStars(kind, id, btnEl) {
  const row = btnEl.parentElement;
  row.querySelectorAll('button').forEach(b => b.disabled = true);
  const oldText = btnEl.textContent;
  btnEl.textContent = 'Kutilmoqda...';
  try {
    const res = await fetch('/api/create_invoice', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ kind, id, init_data: INIT_DATA }),
    });
    const data = await res.json();
    if (!res.ok || !data.invoice_url) {
      toast(data.error || 'Xatolik yuz berdi');
      row.querySelectorAll('button').forEach(b => b.disabled = false);
      btnEl.textContent = oldText;
      return;
    }
    const finish = async (status) => {
      row.querySelectorAll('button').forEach(b => b.disabled = false);
      btnEl.textContent = oldText;
      if (status === 'paid') {
        toast('✅ To\\'lov qabul qilindi!');
        await refreshMe(); await refreshShop();
      } else if (status === 'failed') {
        toast('❌ To\\'lov amalga oshmadi');
      }
    };
    if (tg && tg.openInvoice) {
      tg.openInvoice(data.invoice_url, finish);
    } else {
      window.open(data.invoice_url, '_blank');
      finish('unknown');
    }
  } catch (e) {
    toast('Tarmoq xatosi, qayta urinib ko\\'ring');
    row.querySelectorAll('button').forEach(b => b.disabled = false);
    btnEl.textContent = oldText;
  }
}

let UZS_ITEM = null;
function openUzsModal(item) {
  UZS_ITEM = item;
  document.getElementById('uzsTitle').textContent = `💳 ${item.name}`;
  document.getElementById('uzsSub').textContent = `${item.price_uzs.toLocaleString('ru-RU')} so'm miqdorida to'lov qiling`;
  document.getElementById('uzsCard').textContent = SHOP.settings.pay_card || '—';
  document.getElementById('uzsFile').value = '';
  openOverlay('uzsOverlay');
}
document.getElementById('uzsClose').onclick = () => closeOverlay('uzsOverlay');
document.getElementById('uzsCancel').onclick = () => closeOverlay('uzsOverlay');
document.getElementById('uzsSubmit').onclick = async () => {
  if (!INIT_DATA) { toast('Bu amal uchun botni Telegram ilovasi ichidan oching'); return; }
  const fileEl = document.getElementById('uzsFile');
  if (!fileEl.files.length) { toast('Chek (screenshot) tanlang'); return; }
  const submitBtn = document.getElementById('uzsSubmit');
  submitBtn.disabled = true; submitBtn.textContent = 'Yuborilmoqda...';
  try {
    const fd = new FormData();
    fd.append('init_data', INIT_DATA);
    fd.append('item_id', UZS_ITEM.id);
    fd.append('photo', fileEl.files[0]);
    const res = await fetch('/api/upload_proof', { method: 'POST', body: fd });
    const data = await res.json();
    submitBtn.disabled = false; submitBtn.textContent = 'Yuborish';
    if (!res.ok || !data.ok) { toast(data.error || 'Xatolik yuz berdi'); return; }
    closeOverlay('uzsOverlay');
    showResult('✅ <b>Chekingiz qabul qilindi!</b><br><br>Admin tekshirib, tasdiqlagach mahsulot yetkaziladi.');
  } catch (e) {
    submitBtn.disabled = false; submitBtn.textContent = 'Yuborish';
    toast('Tarmoq xatosi, qayta urinib ko\\'ring');
  }
};

let INFO = { contacts: [], reviews_url: null, min_withdraw_stars: 0 };

function switchPage(key) {
  document.querySelectorAll('.page').forEach(p => p.classList.toggle('active', p.id === 'page-' + key));
  document.querySelectorAll('.nav-item').forEach(n => n.classList.toggle('active', n.dataset.page === key));
  window.scrollTo({ top: 0 });
}
document.getElementById('bottomNav').querySelectorAll('.nav-item').forEach(el => {
  el.onclick = () => switchPage(el.dataset.page);
});

function renderProfile() {
  const nameEl = document.getElementById('profName');
  const avatarEl = document.getElementById('profAvatar');
  const balEl = document.getElementById('profBalance');
  const refsEl = document.getElementById('profRefs');
  const linkEl = document.getElementById('refLinkText');
  if (!USER) {
    nameEl.textContent = 'Aniqlanmadi';
    avatarEl.textContent = '?';
    balEl.textContent = '0';
    refsEl.textContent = '0';
    linkEl.textContent = "Botni Telegram ilovasi ichidan oching";
    return;
  }
  const name = USER.first_name || 'Foydalanuvchi';
  nameEl.textContent = name;
  avatarEl.textContent = name.trim().charAt(0).toUpperCase() || '👤';
  balEl.textContent = USER.balance_stars;
  refsEl.textContent = USER.referals_count;
  linkEl.textContent = USER.ref_link || '—';
}

document.getElementById('refCopyBtn').onclick = async () => {
  const link = USER && USER.ref_link;
  if (!link) { toast('Havola hali tayyor emas'); return; }
  try {
    await navigator.clipboard.writeText(link);
    toast('✅ Havola nusxalandi!');
  } catch (e) {
    toast('Nusxalab bo\\'lmadi — havolani qo\\'lda belgilab oling');
  }
};
document.getElementById('refShareBtn').onclick = () => {
  const link = USER && USER.ref_link;
  if (!link) { toast('Havola hali tayyor emas'); return; }
  const shareText = "Men bu bot orqali yulduzlar yig'ib, sovg'alar olaman! Qo'shil! 🎁";
  const shareUrl = `https://t.me/share/url?url=${encodeURIComponent(link)}&text=${encodeURIComponent(shareText)}`;
  if (tg && tg.openTelegramLink) tg.openTelegramLink(shareUrl);
  else window.open(shareUrl, '_blank');
};

function renderWithdraw() {
  document.getElementById('wdBalance').textContent = (USER ? USER.balance_stars : 0) + ' ⭐';
  document.getElementById('wdMin').textContent = (INFO.min_withdraw_stars || 0) + ' ⭐';
  const note = document.getElementById('wdNote');
  const starsBtn = document.getElementById('wdStarsBtn');
  const canWithdraw = USER && USER.balance_stars >= (INFO.min_withdraw_stars || 0);
  note.textContent = canWithdraw
    ? "Butun balansingiz yechiladi (yulduz yoki gift sifatida)."
    : `Yechish uchun kamida ${INFO.min_withdraw_stars} ⭐ kerak.`;
  starsBtn.disabled = !canWithdraw;
  starsBtn.textContent = `⭐ Yulduz sifatida yechish (${USER ? USER.balance_stars : 0})`;

  const list = document.getElementById('wdGiftList');
  list.innerHTML = '';
  if (!USER) return;
  const gifts = SHOP.items.filter(i => i.cat === 'gift' && i.price_stars > 0 && i.price_stars <= USER.balance_stars);
  if (!gifts.length) {
    list.innerHTML = '<div class="hint-p">Balansingizga hozircha yetadigan gift yo\\'q.</div>';
    return;
  }
  gifts.forEach(g => {
    const row = document.createElement('div');
    row.className = 'gift-row';
    row.innerHTML = `<div><div class="gname">${g.name}</div><div class="gprice">${g.price_stars} ⭐</div></div><button>Yechish</button>`;
    row.querySelector('button').onclick = (e) => withdraw('gift', g.id, e.target);
    list.appendChild(row);
  });
}

document.getElementById('wdStarsBtn').onclick = (e) => withdraw('stars', null, e.target);

async function withdraw(kind, itemId, btnEl) {
  if (!INIT_DATA) { toast('Bu amal uchun botni Telegram ilovasi ichidan oching'); return; }
  const oldText = btnEl.textContent;
  btnEl.disabled = true; btnEl.textContent = 'Yuborilmoqda...';
  try {
    const res = await fetch('/api/withdraw', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ init_data: INIT_DATA, kind, item_id: itemId }),
    });
    const data = await res.json();
    btnEl.disabled = false; btnEl.textContent = oldText;
    if (!res.ok || !data.ok) { toast(data.error || 'Xatolik yuz berdi'); return; }
    showResult(data.message);
    await refreshMe();
  } catch (e) {
    btnEl.disabled = false; btnEl.textContent = oldText;
    toast('Tarmoq xatosi, qayta urinib ko\\'ring');
  }
}

function renderContacts() {
  const list = document.getElementById('contactsList');
  list.innerHTML = '';
  if (!INFO.contacts.length) {
    list.innerHTML = '<div class="hint-p">Kontaktlar hozircha yo\\'q.</div>';
    return;
  }
  INFO.contacts.forEach(c => {
    const a = document.createElement('a');
    a.className = 'contact-row';
    a.href = `https://t.me/${c.username}`;
    a.target = '_blank';
    a.innerHTML = `<div class="c-ic">📩</div><div><div class="c-label">${c.label}</div><div class="c-user">@${c.username}</div></div>`;
    a.onclick = (e) => {
      e.preventDefault();
      if (tg && tg.openTelegramLink) tg.openTelegramLink(`https://t.me/${c.username}`);
      else window.open(`https://t.me/${c.username}`, '_blank');
    };
    list.appendChild(a);
  });
}

function renderReviews() {
  const textEl = document.getElementById('reviewsText');
  const banner = document.getElementById('reviewsBanner');
  if (INFO.reviews_url) {
    textEl.textContent = "Otzivlar bot ichida emas, otziv kanalimizda qoldiriladi. Tugmani bosib kanalga o'ting.";
    if (!document.getElementById('reviewsGoBtn')) {
      const btn = document.createElement('button');
      btn.id = 'reviewsGoBtn';
      btn.textContent = "Kanalga o'tish";
      btn.onclick = () => {
        if (tg && tg.openTelegramLink && INFO.reviews_url.includes('t.me')) tg.openTelegramLink(INFO.reviews_url);
        else window.open(INFO.reviews_url, '_blank');
      };
      banner.appendChild(btn);
    }
  } else {
    textEl.textContent = "Otziv kanali hali sozlanmagan.";
  }
}

async function refreshInfo() {
  try {
    const res = await fetch('/api/info');
    INFO = await res.json();
  } catch (e) { /* keep defaults */ }
  renderWithdraw();
  renderContacts();
  renderReviews();
}

async function load() {
  renderTabs();
  await Promise.all([refreshMe(), refreshShop(), refreshInfo()]);
}

load();
</script>
</body>
</html>
""".replace("__LOGO_DATA_URI__", BOT_LOGO_DATA_URI)


async def webapp_page_handler(request):
    return web.Response(text=MINI_APP_HTML, content_type="text/html")


def verify_webapp_init_data(init_data: str) -> dict | None:
    """Telegram Mini App'dan kelgan initData'ni HMAC orqali tekshiradi
    (https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app).
    Muvaffaqiyatli bo'lsa foydalanuvchi ma'lumotlari lug'atini (id, first_name,
    username, ...) qaytaradi, aks holda None (soxta/o'zgartirilgan so'rov)."""
    if not init_data or not BOT_TOKEN:
        return None
    try:
        pairs = dict(parse_qsl(init_data, strict_parsing=True))
    except ValueError:
        return None

    received_hash = pairs.pop("hash", None)
    if not received_hash:
        return None

    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    computed_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(computed_hash, received_hash):
        return None

    user_raw = pairs.get("user")
    if not user_raw:
        return None
    try:
        user = json.loads(user_raw)
    except ValueError:
        return None
    if "id" not in user:
        return None
    return user


async def _webapp_identify(request) -> tuple[dict | None, dict | None]:
    """So'rov tanasidan (JSON yoki form) init_data'ni oladi, tekshiradi va
    mos foydalanuvchi bazadagi yozuvini qaytaradi. (tg_user, db_user)."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    init_data = body.get("init_data", "") if isinstance(body, dict) else ""
    tg_user = verify_webapp_init_data(init_data)
    if not tg_user:
        return None, None

    telegram_id = int(tg_user["id"])
    db_user = await get_user(telegram_id)
    if not db_user:
        await add_user(telegram_id)
        db_user = await get_user(telegram_id)
    return tg_user, db_user


async def webapp_me_handler(request):
    """Mini App header'ida va Profil bo'limida ko'rsatiladigan foydalanuvchi
    ma'lumotlari (balans, referallar soni, referal havola) — initData
    orqali xavfsiz aniqlanadi."""
    tg_user, db_user = await _webapp_identify(request)
    if not db_user:
        return web.json_response({"error": "Foydalanuvchi aniqlanmadi"}, status=401)

    ref_link = None
    if _bot is not None:
        try:
            me = await _bot.me()
            ref_link = f"https://t.me/{me.username}?start={db_user['telegram_id']}"
        except Exception:
            ref_link = None

    return web.json_response({
        "telegram_id": db_user["telegram_id"],
        "first_name": tg_user.get("first_name", ""),
        "balance_stars": db_user["balance_stars"],
        "referals_count": db_user["referals_count"],
        "last_daily_box": db_user["last_daily_box"],
        "ref_link": ref_link,
    })


async def webapp_info_handler(request):
    """Mini App'ning Aloqa/Otziv/Yechish bo'limlari uchun umumiy (foydalanuvchiga
    bog'liq bo'lmagan) sozlamalar — kontaktlar, otziv kanali, minimal yechish
    chegarasi. Admin panelda o'zgartirilgan sozlama shu yerda avtomatik ko'rinadi."""
    s = await get_settings()
    contacts = await get_contacts()
    return web.json_response({
        "contacts": [{"label": c["label"], "username": c["username"].lstrip("@")} for c in contacts],
        "reviews_url": channel_url(s["reviews_channel"]) if s["reviews_channel"] else None,
        "min_withdraw_stars": s["min_withdraw_stars"],
        "ref_reward_stars": s["ref_reward_stars"],
        "min_referals_required": s["min_referals_required"],
    })


async def webapp_shop_api_handler(request):
    """Mini App uchun do'kondagi BARCHA toifalar: Gift/Premium/Yulduz
    (bot balansi + Telegram Stars + UZS), Jekpot (boxlar) va NFT (guruh
    havolasi) — admin panelda qo'shilgan har qanday yangi narsa shu yerda
    avtomatik chiqadi, chunki ro'yxat har safar bazadan jonli o'qiladi."""
    items = []
    for it in await get_all_shop_items():
        if it["category"] == "nft":
            continue
        if it["price_stars"] <= 0 and it["price_uzs"] <= 0:
            continue
        items.append({
            "kind": "item",
            "id": it["id"],
            "cat": it["category"],
            "name": it["name"],
            "desc": it["description"] or "",
            "price_stars": it["price_stars"],
            "price_uzs": it["price_uzs"],
        })

    boxes = []
    for b in await get_all_boxes():
        boxes.append({
            "kind": "box",
            "id": b["box_id"],
            "name": b["name"],
            "desc": b["desc_text"] or "",
            "cost": b["cost"],
            "cost_tgstars": b["cost_tgstars"],
            "once_per_day": bool(b["once_per_day"]),
        })

    shop_promos = []
    for p in await get_purchasable_promo_codes():
        shop_promos.append({
            "kind": "shop_promo",
            "id": p["id"],
            "name": p["shop_name"],
            "desc": p["desc_text"] or "",
            "price_stars": p["shop_price_stars"],
        })

    s = await get_settings()

    return web.json_response({
        "items": items,
        "boxes": boxes,
        "shop_promos": shop_promos,
        "nft": {"group_url": channel_url(s["nft_group"]) if s["nft_group"] else None},
        "settings": {"pay_card": format_card(s["pay_card"])},
        "server_date": datetime.now().strftime("%Y-%m-%d"),
    })


async def webapp_create_invoice_handler(request):
    """Mini App'dan kelgan 'sotib olish' so'rovi uchun Telegram Stars
    invoys havolasini yaratadi (bot.create_invoice_link) — foydalanuvchi
    uni Telegram.WebApp.openInvoice() orqali ochadi."""
    if _bot is None:
        return web.json_response({"error": "Bot hali tayyor emas"}, status=503)

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Noto'g'ri so'rov"}, status=400)

    kind = body.get("kind")
    raw_id = body.get("id")

    try:
        if kind == "item":
            item = await get_shop_item(int(raw_id))
            if not item or item["price_stars"] <= 0:
                return web.json_response({"error": "Mahsulot topilmadi"}, status=404)
            label = CATEGORIES.get(item["category"], item["category"])
            invoice_url = await _bot.create_invoice_link(
                title=f"{label} — {item['name']}",
                description=item["description"] or f"{item['name']} ({item['price_stars']} ⭐ Telegram Stars)",
                payload=f"shop_item:{item['id']}",
                currency="XTR",
                prices=[LabeledPrice(label=item["name"], amount=item["price_stars"])],
                provider_token="",
            )
        elif kind == "box":
            box = await get_box(str(raw_id))
            if not box or box["cost_tgstars"] <= 0:
                return web.json_response({"error": "Box topilmadi"}, status=404)
            invoice_url = await _bot.create_invoice_link(
                title=f"📦 {box['name']}",
                description=box["desc_text"] or f"{box['name']} — {box['cost_tgstars']} Telegram Stars",
                payload=f"box:{box['box_id']}",
                currency="XTR",
                prices=[LabeledPrice(label=box["name"], amount=box["cost_tgstars"])],
                provider_token="",
            )
        else:
            return web.json_response({"error": "Noma'lum turi"}, status=400)
    except Exception as e:
        logger.error("Mini App invoys yaratilmadi (kind=%s, id=%s): %s", kind, raw_id, e)
        return web.json_response({"error": "Invoys yaratib bo'lmadi"}, status=500)

    return web.json_response({"invoice_url": invoice_url})


async def webapp_buy_balance_handler(request):
    """Mini App'dan bot balansi (ichki ⭐) bilan xarid — Gift/Premium
    mahsulot yoki Jekpot box. Xuddi bot-chat'dagi buy_item_callback /
    box_open_callback bilan bir xil qoidalar (referal talabi, balans
    tekshiruvi) qo'llaniladi."""
    if _bot is None:
        return web.json_response({"error": "Bot hali tayyor emas"}, status=503)

    tg_user, user = await _webapp_identify(request)
    if not user:
        return web.json_response({"error": "Foydalanuvchi aniqlanmadi — botni Telegram ichidan oching"}, status=401)

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Noto'g'ri so'rov"}, status=400)

    kind = body.get("kind")
    raw_id = body.get("id")
    telegram_id = int(tg_user["id"])
    first_name = tg_user.get("first_name", "")
    username = tg_user.get("username", "")

    if kind == "item":
        item = await get_shop_item(int(raw_id))
        if not item:
            return web.json_response({"error": "Mahsulot topilmadi"}, status=404)
        if item["category"] == "star":
            return web.json_response({"error": "⭐ Yulduzlar faqat karta (UZS) bilan sotib olinadi"}, status=400)
        if item["price_stars"] <= 0:
            return web.json_response({"error": "Bu mahsulot uchun bot balansi narxi belgilanmagan"}, status=400)

        settings = await get_settings()
        label = CATEGORIES.get(item["category"], item["category"])

        if user["referals_count"] < settings["min_referals_required"]:
            need = settings["min_referals_required"] - user["referals_count"]
            return web.json_response({
                "error": f"Minimal {settings['min_referals_required']} ta odam taklif qilishingiz kerak! "
                         f"Yana {need} ta kerak.",
            }, status=403)

        if user["balance_stars"] < item["price_stars"]:
            need_stars = item["price_stars"] - user["balance_stars"]
            return web.json_response({
                "error": f"Balansingiz yetarli emas! Yana {need_stars} ⭐ kerak.",
            }, status=402)

        await deduct_stars(telegram_id, item["price_stars"])

        gift_result = await deliver_gift(
            _bot, telegram_id, item, price_stars=item["price_stars"], source="purchase",
        )
        status_line, user_note = gift_delivery_texts(gift_result)

        for admin_id in ADMIN_IDS:
            try:
                await _bot.send_message(
                    admin_id,
                    f"🛒 <b>YANGI BUYURTMA (Mini App)!</b>\n\n"
                    f"{status_line}"
                    f"👤 Foydalanuvchi: {first_name} (@{username or '—'})\n"
                    f"🆔 ID: <code>{telegram_id}</code>\n"
                    f"{label} <b>{item['name']}</b>\n"
                    f"💰 Narxi: <b>{item['price_stars']} ⭐</b> (bot balansi)\n"
                    f"📅 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                )
            except TelegramForbiddenError:
                pass

        message = (
            f"✅ <b>Xarid muvaffaqiyatli!</b><br><br>"
            f"{label} <b>{item['name']}</b> — {item['price_stars']} ⭐ ayirildi. "
            f"{user_note}"
        )
        return web.json_response({"ok": True, "message": message})

    if kind == "box":
        box = await get_box(str(raw_id))
        if not box:
            return web.json_response({"error": "Box topilmadi"}, status=404)

        today = datetime.now().strftime("%Y-%m-%d")
        if box["once_per_day"] and user["last_daily_box"] == today:
            return web.json_response({"error": "Kunlik boxni bugun ishlatgansiz! Ertaga qayta oching."}, status=403)

        if user["balance_stars"] < box["cost"]:
            return web.json_response({"error": f"Balans yetarli emas! Kerak: {box['cost']} ⭐"}, status=402)

        await deduct_stars(telegram_id, box["cost"])
        if box["once_per_day"]:
            await set_daily_box_used(telegram_id, today)

        result = await open_box_and_award(_bot, box, telegram_id, first_name, username)
        return web.json_response({
            "ok": True,
            "message": result["text"],
            "ratio": result["ratio"],
            "kind": result["kind"],
            "claim_id": result["claim_id"],
            "gift_price_stars": result["gift_price_stars"],
        })

    return web.json_response({"error": "Noma'lum turi"}, status=400)


async def webapp_redeem_promo_handler(request):
    """Mini App'dan promokod kiritish — bot-chat'dagi promo_redeem_input bilan
    bir xil markazlashgan redeem_promo_code() funksiyasidan foydalanadi."""
    if _bot is None:
        return web.json_response({"error": "Bot hali tayyor emas"}, status=503)

    tg_user, user = await _webapp_identify(request)
    if not user:
        return web.json_response({"error": "Foydalanuvchi aniqlanmadi — botni Telegram ichidan oching"}, status=401)

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Noto'g'ri so'rov"}, status=400)

    code = str(body.get("code") or "")
    telegram_id = int(tg_user["id"])
    first_name = tg_user.get("first_name", "")
    username = tg_user.get("username", "")
    result = await redeem_promo_code(_bot, telegram_id, first_name, username, code)
    if not result["ok"]:
        return web.json_response({"error": result["error"]}, status=400)

    return web.json_response({
        "ok": True,
        "message": result["text"],
        "ratio": result["ratio"],
        "kind": result["kind"],
        "claim_id": result["claim_id"],
        "gift_price_stars": result["gift_price_stars"],
    })


async def webapp_buy_promo_handler(request):
    """Mini App'dan do'kondagi promokodni ⭐ balans evaziga sotib olish —
    bot-chat'dagi shop_promo_buy_callback bilan bir xil markazlashgan
    buy_promo_from_shop() funksiyasidan foydalanadi."""
    if _bot is None:
        return web.json_response({"error": "Bot hali tayyor emas"}, status=503)

    tg_user, user = await _webapp_identify(request)
    if not user:
        return web.json_response({"error": "Foydalanuvchi aniqlanmadi — botni Telegram ichidan oching"}, status=401)

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Noto'g'ri so'rov"}, status=400)

    try:
        promo_id = int(body.get("id"))
    except (TypeError, ValueError):
        return web.json_response({"error": "Noto'g'ri so'rov"}, status=400)

    telegram_id = int(tg_user["id"])
    first_name = tg_user.get("first_name", "")
    username = tg_user.get("username", "")
    result = await buy_promo_from_shop(_bot, telegram_id, first_name, username, promo_id)
    if not result["ok"]:
        return web.json_response({"error": result["error"]}, status=400)

    return web.json_response({
        "ok": True,
        "message": result["text"],
        "ratio": result["ratio"],
        "kind": result["kind"],
        "claim_id": result["claim_id"],
        "gift_price_stars": result["gift_price_stars"],
    })


async def webapp_gift_claim_handler(request):
    """Mini App'da box'dan gift yutilganda foydalanuvchi tanlagan variantni
    bajaradi — bot-chat'dagi giftclaim:real / giftclaim:stars bilan bir xil
    mantiq (try_auto_deliver_gift / add_stars), faqat HTTP orqali."""
    if _bot is None:
        return web.json_response({"error": "Bot hali tayyor emas"}, status=503)

    tg_user, user = await _webapp_identify(request)
    if not user:
        return web.json_response({"error": "Foydalanuvchi aniqlanmadi — botni Telegram ichidan oching"}, status=401)

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Noto'g'ri so'rov"}, status=400)

    action = body.get("action")
    claim_id = body.get("claim_id")
    telegram_id = int(tg_user["id"])

    claim = await get_gift_claim(int(claim_id)) if claim_id is not None else None
    if not claim:
        return web.json_response({"error": "Topilmadi"}, status=404)
    if claim["telegram_id"] != telegram_id:
        return web.json_response({"error": "Bu sizga tegishli emas"}, status=403)
    if claim["status"] != "pending":
        return web.json_response({"error": "Bu gift bo'yicha allaqachon tanlov qilingan"}, status=409)

    if action == "stars":
        await update_gift_claim_status(claim["id"], "claimed_stars")
        await add_stars(telegram_id, claim["price_stars"])
        message = f"⭐ <b>{claim['item_name']}</b> — {claim['price_stars']} ⭐ ga aylantirildi va balansingizga qo'shildi!"
        return web.json_response({"ok": True, "message": message})

    if action == "real":
        item = await get_shop_item(claim["item_id"])

        variants = await get_gift_variants(claim["item_id"]) if item else []
        if len(variants) >= 2:
            # Bir nechta gift turi bog'langan — tanlovni Telegram chatidagi
            # tugmalar orqali qildiramiz (claim hali "pending" holatida qoladi,
            # giftvariant: callback uni yakunlaydi — bot-chat bilan bir xil yo'l).
            try:
                await _bot.send_message(
                    telegram_id,
                    f"🎁 <b>{claim['item_name']}</b>\n\nQaysi turini xohlaysiz?",
                    reply_markup=gift_variant_choice_keyboard(claim["id"], variants),
                )
            except TelegramForbiddenError:
                pass
            message = "🎯 Sizga botning shaxsiy chatiga gift turini tanlash uchun xabar yubordik — shu yerdan tanlang!"
            return web.json_response({"ok": True, "message": message})

        delivered, error = await try_auto_deliver_gift(_bot, telegram_id, item)
        if delivered:
            await update_gift_claim_status(claim["id"], "claimed_gift")
            message = f"✅ <b>{claim['item_name']}</b> avtomatik yuborildi — Telegram'dagi \"Sovg'alar\" bo'limingizni tekshiring! ✨"
            for admin_id in ADMIN_IDS:
                try:
                    await _bot.send_message(
                        admin_id,
                        f"🎁 <b>BOX'DAN GIFT TANLANDI VA AVTOMATIK YUBORILDI (Mini App)!</b>\n\n"
                        f"👤 Foydalanuvchi: {tg_user.get('first_name', '')} (@{tg_user.get('username') or '—'})\n"
                        f"🆔 ID: <code>{telegram_id}</code>\n"
                        f"🎁 Gift: <b>{claim['item_name']}</b> ({claim['price_stars']} ⭐)\n\n"
                        f"✅ Hech narsa qilish shart emas — allaqachon yuborilgan.",
                    )
                except TelegramForbiddenError:
                    pass
        else:
            w_id = await add_withdrawal(
                telegram_id=telegram_id,
                user_name=tg_user.get("first_name", ""),
                username=tg_user.get("username", ""),
                kind="gift",
                amount_stars=claim["price_stars"],
                item_name=claim["item_name"],
            )
            await update_gift_claim_status(claim["id"], "claimed_gift")
            kb = InlineKeyboardBuilder()
            kb.button(text="✅ Gift yubordim", callback_data=f"wd_approve:{w_id}")
            kb.adjust(1)
            warn = f"⚠️ Avtomatik yuborish muvaffaqiyatsiz bo'ldi ({error}) — qo'lda yuboring!\n\n" if error else ""
            for admin_id in ADMIN_IDS:
                try:
                    await _bot.send_message(
                        admin_id,
                        f"🎁 <b>BOX'DAN GIFT TANLANDI — QO'LDA YUBORISH KERAK (Mini App)!</b>\n\n"
                        f"{warn}"
                        f"👤 Foydalanuvchi: {tg_user.get('first_name', '')} (@{tg_user.get('username') or '—'})\n"
                        f"🆔 ID: <code>{telegram_id}</code>\n"
                        f"🎁 Gift: <b>{claim['item_name']}</b> ({claim['price_stars']} ⭐)\n\n"
                        f"⚠️ Giftni foydalanuvchiga Telegram'da yuborgach, tugmani bosing.",
                        reply_markup=kb.as_markup(),
                    )
                except TelegramForbiddenError:
                    pass
            message = "✅ So'rovingiz qabul qilindi — gift tez orada admin tomonidan yuboriladi."
        return web.json_response({"ok": True, "message": message})

    return web.json_response({"error": "Noma'lum amal"}, status=400)


async def webapp_upload_proof_handler(request):
    """Mini App'dan UZS (karta) to'lovi uchun chek (screenshot) qabul
    qiladi — bot-chat'dagi uzs_proof_received bilan bir xil natija: order
    yaratiladi va adminga tasdiqlash tugmalari bilan yuboriladi."""
    if _bot is None:
        return web.json_response({"error": "Bot hali tayyor emas"}, status=503)

    init_data = ""
    item_id_raw = None
    photo_bytes = None

    try:
        reader = await request.multipart()
        async for field in reader:
            if field.name == "init_data":
                init_data = (await field.read()).decode("utf-8", "ignore")
            elif field.name == "item_id":
                item_id_raw = (await field.read()).decode("utf-8", "ignore")
            elif field.name == "photo":
                photo_bytes = await field.read()
    except Exception:
        return web.json_response({"error": "Noto'g'ri so'rov"}, status=400)

    tg_user = verify_webapp_init_data(init_data)
    if not tg_user:
        return web.json_response({"error": "Foydalanuvchi aniqlanmadi — botni Telegram ichidan oching"}, status=401)
    if not photo_bytes:
        return web.json_response({"error": "Chek (screenshot) topilmadi"}, status=400)

    telegram_id = int(tg_user["id"])
    first_name = tg_user.get("first_name", "")
    username = tg_user.get("username", "")

    try:
        item = await get_shop_item(int(item_id_raw))
    except (TypeError, ValueError):
        item = None
    if not item or item["price_uzs"] <= 0:
        return web.json_response({"error": "Mahsulot topilmadi"}, status=404)

    if not await get_user(telegram_id):
        await add_user(telegram_id)

    label = CATEGORIES.get(item["category"], item["category"])
    order_id = await add_order(
        telegram_id=telegram_id,
        user_name=first_name,
        username=username,
        item_id=item["id"],
        item_name=item["name"],
        category=item["category"],
        amount_uzs=item["price_uzs"],
        proof_file_id="",
    )

    caption = (
        f"🛒 <b>YANGI BUYURTMA #{order_id} (UZS, Mini App)!</b>\n\n"
        f"👤 Foydalanuvchi: {first_name} (@{username or '—'})\n"
        f"🆔 ID: <code>{telegram_id}</code>\n"
        f"{label} <b>{item['name']}</b>\n"
        f"💰 Summa: <b>{item['price_uzs']:,} so'm</b>\n"
        f"📅 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        f"👇 Chekni tekshiring va qaror qabul qiling:"
    )
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Tasdiqlash", callback_data=f"order_approve:{order_id}")
    kb.button(text="❌ Rad etish", callback_data=f"order_reject:{order_id}")
    kb.adjust(1)

    photo_file = BufferedInputFile(photo_bytes, filename=f"proof_{order_id}.jpg")
    for admin_id in ADMIN_IDS:
        try:
            await _bot.send_photo(admin_id, photo_file, caption=caption, reply_markup=kb.as_markup())
        except TelegramForbiddenError:
            pass

    return web.json_response({"ok": True, "order_id": order_id})


async def webapp_withdraw_handler(request):
    """Mini App'dagi 'Yulduz yechish' bo'limi — ⭐ yulduz yoki 🎁 gift
    sifatida yechish so'rovini yaratadi. Bot-chat'dagi process_stars_withdrawal
    / withdraw_gift_confirm bilan bir xil qoidalar (minimal chegara, balans
    tekshiruvi) va bir xil kuzatuv (withdrawals jadvali + admin
    Tasdiqlash/Bekor tugmalari — qayta-qayta to'lab yubormaslik uchun)."""
    if _bot is None:
        return web.json_response({"error": "Bot hali tayyor emas"}, status=503)

    tg_user, user = await _webapp_identify(request)
    if not user:
        return web.json_response({"error": "Foydalanuvchi aniqlanmadi — botni Telegram ichidan oching"}, status=401)

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Noto'g'ri so'rov"}, status=400)

    kind = body.get("kind")
    telegram_id = int(tg_user["id"])
    first_name = tg_user.get("first_name", "")
    username = tg_user.get("username", "")
    settings = await get_settings()

    if user["balance_stars"] < settings["min_withdraw_stars"]:
        need = settings["min_withdraw_stars"] - user["balance_stars"]
        return web.json_response({
            "error": f"Yechish uchun minimal {settings['min_withdraw_stars']} ⭐ kerak. Yana {need} ⭐ kerak.",
        }, status=402)

    if kind == "stars":
        amount = user["balance_stars"]
        item_name = ""
    elif kind == "gift":
        try:
            item = await get_shop_item(int(body.get("item_id")))
        except (TypeError, ValueError):
            item = None
        if not item or item["category"] != "gift" or item["price_stars"] <= 0:
            return web.json_response({"error": "Gift topilmadi"}, status=404)
        if user["balance_stars"] < item["price_stars"]:
            return web.json_response({"error": "Balans yetarli emas"}, status=402)
        amount = item["price_stars"]
        item_name = item["name"]
    else:
        return web.json_response({"error": "Noma'lum turi"}, status=400)

    await deduct_stars(telegram_id, amount)
    w_id = await add_withdrawal(
        telegram_id=telegram_id, user_name=first_name, username=username,
        kind=kind, amount_stars=amount, item_name=item_name,
    )

    kb = InlineKeyboardBuilder()
    kb.button(text="✅ To'lov qildim", callback_data=f"wd_approve:{w_id}")
    kb.button(text="❌ Bekor qilish (qaytarish)", callback_data=f"wd_reject:{w_id}")
    kb.adjust(1)

    label = f"🎁 Gift: <b>{item_name}</b>" if kind == "gift" else "⭐ Yulduz sifatida"
    for admin_id in ADMIN_IDS:
        try:
            await _bot.send_message(
                admin_id,
                f"💸 <b>YECHISH SO'ROVI #{w_id} (Mini App)</b>\n\n"
                f"👤 Foydalanuvchi: {first_name} (@{username or '—'})\n"
                f"🆔 ID: <code>{telegram_id}</code>\n"
                f"{label}\n"
                f"💰 Miqdor: <b>{amount} ⭐</b>\n"
                f"📅 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                f"⚠️ Real to'lovni o'tkazgach \"✅ To'lov qildim\" tugmasini bosing — "
                f"shunda ikki marta to'lab yubormaysiz.",
                reply_markup=kb.as_markup(),
            )
        except TelegramForbiddenError:
            pass

    message = (
        f"✅ <b>So'rovingiz qabul qilindi!</b><br><br>"
        f"💰 Miqdor: <b>{amount} ⭐</b><br>"
        f"🧾 So'rov: #{w_id}<br><br>"
        f"Yulduzlar/gift bot egasi tomonidan tez orada yuboriladi."
    )
    return web.json_response({"ok": True, "message": message})


def register_webapp_routes(app: "web.Application") -> None:
    """Mini App uchun kerakli barcha yo'llarni (routes) mavjud aiohttp
    ilovasiga qo'shadi — polling va webhook rejimlarining ikkalasida ham
    ishlatiladi."""
    app.router.add_get("/webapp", webapp_page_handler)
    app.router.add_get("/api/shop", webapp_shop_api_handler)
    app.router.add_get("/api/info", webapp_info_handler)
    app.router.add_post("/api/me", webapp_me_handler)
    app.router.add_post("/api/withdraw", webapp_withdraw_handler)
    app.router.add_post("/api/create_invoice", webapp_create_invoice_handler)
    app.router.add_post("/api/buy_balance", webapp_buy_balance_handler)
    app.router.add_post("/api/upload_proof", webapp_upload_proof_handler)
    app.router.add_post("/api/gift_claim", webapp_gift_claim_handler)
    app.router.add_post("/api/redeem_promo", webapp_redeem_promo_handler)
    app.router.add_post("/api/buy_promo", webapp_buy_promo_handler)


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

    # Xabar kiritish maydonining chap tomonidagi doimiy "Menu" tugmasini
    # to'g'ridan-to'g'ri Mini App'ni ochadigan qilib sozlaymiz (xuddi
    # @BotFather chatidagi "Открыть/Open" tugmasi kabi) — foydalanuvchi
    # pastdagi reply-klaviaturani qidirmasdan, bitta bosishda Mini App'ni
    # ochadi. Faqat PUBLIC_BASE_URL HTTPS bo'lsa sozlanadi, aks holda
    # Telegram bunday tugmani rad etadi.
    if PUBLIC_BASE_URL.startswith("https://"):
        try:
            await bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(
                    text="Stars Bot",
                    web_app=WebAppInfo(url=f"{PUBLIC_BASE_URL}/webapp"),
                ),
            )
            logger.info("Menu button (Mini App) sozlandi: %s/webapp", PUBLIC_BASE_URL)
        except Exception as e:
            logger.error("Menu button sozlanmadi: %s", e)


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

    if WEBHOOK_URL:
        # Render/webhook rejimi
        from aiohttp import web
        from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

        app = web.Application()
        app.router.add_get('/', handle_ping)
        SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path=WEBHOOK_PATH)
        setup_application(app, dp, bot=bot)
        register_webapp_routes(app)  # /webapp, /api/shop, /api/create_invoice

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, host=WEBAPP_HOST, port=WEBAPP_PORT)
        await on_startup(bot)
        await site.start()
        logger.info("Webhook server ishga tushdi: %s:%s%s", WEBAPP_HOST, WEBAPP_PORT, WEBHOOK_PATH)
        await asyncio.Event().wait()
    else:
        # Eslatma: on_startup allaqachon dp.startup.register() orqali ro'yxatdan
        # o'tgan — dp.start_polling() uni o'zi avtomatik chaqiradi. Bu yerda yana
        # qo'lda chaqirilsa, db_init/webhook o'chirish 2 marta bajariladi (zararsiz,
        # lekin ortiqcha), shuning uchun faqat bitta marta ishlaydi.
        await start_web_server()
        await dp.start_polling(bot)


if __name__ == "__main__":
    import asyncio

    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot to'xtatildi")
