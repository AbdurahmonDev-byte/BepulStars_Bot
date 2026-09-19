# -*- coding: utf-8 -*-
"""
fragment-api.uz servisi bilan ishlash — Telegram Stars'ni avtomatik yuborish.

Ushbu modul bot.py dagi "Yulduz yechish" oqimiga ulanadi: foydalanuvchi
yulduzlarini yechmoqchi bo'lganda, fragment-api.uz API'si orqali real Telegram
Stars buyurtma qilinadi va to'g'ridan-to'g'ri qabul qiluvchining @username'iga
yuboriladi. To'lov loyihaning o'z TON/USDT hamyonidan amalga oshiriladi.

Sozlash (.env / Render env):
    FRAGMENT_API_KEY   — fragment-api.uz dashboard'idan olingan API kalit.
    FRAGMENT_API_URL   — ixtiyoriy, standart: https://fragment-api.uz
    FRAGMENT_STARS_STEP— Telegram Stars paket qadami (standart 50). Avto-send
                         faqat bu qiymatga bo'linadigan summalar uchun yoqiladi.

Xavfsizlik: API kalit faqat .env da saqlanadi, repo'ga kirmaydi (qarang .env.example).
"""
import logging
import os

import aiohttp

logger = logging.getLogger(__name__)

FRAGMENT_API_URL = os.getenv("FRAGMENT_API_URL", "https://fragment-api.uz").rstrip("/")
FRAGMENT_API_KEY = os.getenv("FRAGMENT_API_KEY", "").strip()
FRAGMENT_STARS_STEP = int(os.getenv("FRAGMENT_STARS_STEP", "50") or "0")


def is_enabled() -> bool:
    """Fragment API kaliti sozlanganmi?"""
    return bool(FRAGMENT_API_KEY) and FRAGMENT_API_URL.startswith("http")


def can_auto_send(amount: int) -> bool:
    """Bu summa fragment orqali avtomatik yuborishga mosmi?

    Telegram real Stars fragmentda odatda 50 ga karrati bo'lgan paketlarda
    sotiladi, shuning uchun step'ga bo'linmaydigan summalarda avto-send
    yoqilmaydi — o'sha holatda bot eski tartibda (admin qo'lda tasdiqlaydi)
    ishlayveradi, hech narsa yo'qolmaydi."""
    if not is_enabled():
        return False
    return FRAGMENT_STARS_STEP > 0 and isinstance(amount, int) and amount > 0 and amount % FRAGMENT_STARS_STEP == 0


async def _post(path: str, payload: dict | None = None, *, timeout: int = 25):
    """X-API-Key sarlavhali POST so'rov. (ok, result_or_message) qaytaradi —
    muvaffaqiyatda (True, result dict), aks holda (False, xato matni)."""
    url = f"{FRAGMENT_API_URL}{path}"
    headers = {
        "X-API-Key": FRAGMENT_API_KEY,
        "Content-Type": "application/json",
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url,
                json=payload or {},
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                status = resp.status
                try:
                    data = await resp.json()
                except Exception:
                    data = {"ok": False, "message": (await resp.text())[:300]}
        if status >= 400 or not data.get("ok"):
            return False, data.get("message") or f"HTTP {status}"
        return True, data.get("result") or data
    except Exception as e:
        logger.warning("fragment-api.uz so'rovi muvaffaqiyatsiz (%s): %s", path, e)
        return False, str(e)


async def buy_stars(username: str, amount: int) -> tuple[bool, dict | str]:
    """Qabul qiluvchining @username'iga real Telegram Stars yuborish.

    Success: (True, {"username","amount","payment_method","cost"})."""
    username = (username or "").strip().lstrip("@")
    if not username:
        return False, "username ko'rsatilmagan"
    return await _post("/api/v1/stars/buy", {"username": username, "amount": int(amount)})


async def get_info(username: str) -> tuple[bool, dict | str]:
    """Telegram foydalanuvchini fragment orqali tekshirish (yuborishdan oldin)."""
    username = (username or "").strip().lstrip("@")
    if not username:
        return False, "username ko'rsatilmagan"
    return await _post("/api/v1/getInfo", {"username": username})


async def wallet_balance() -> tuple[bool, dict | str]:
    """Loyiha hamyonidagi TON/USDT balansini olish (admin audit uchun)."""
    return await _post("/api/v1/wallet/balance")