# -*- coding: utf-8 -*-
"""
DB moslashtiruvchi qatlam (compatibility shim).

Muammo: Render'ning free tarifida doimiy disk (persistent disk) yo'q — bu
degani, xizmat har safar qayta deploy qilinganda yoki qayta ishga tushganda
lokal fayllar (jumladan bot.db — foydalanuvchilar, balanslar, referallar,
buyurtmalar) butunlay o'chib ketadi. Bu bot kodiga aloqasi yo'q, Render
hostingining o'zi shunday ishlaydi.

Yechim: bazani Render diskiga emas, tashqi doimiy bazaga (Turso / libSQL —
bepul tarifi bor) ko'chirish. Bu modul ikki rejimda ishlaydi:

  1) TURSO_DATABASE_URL (va TURSO_AUTH_TOKEN) .env'da sozlansa — Turso'ga
     ulanadi. Ma'lumotlar endi Render'ning vaqtinchalik diskida emas, Turso
     serverida saqlanadi — redeploy/restart bo'lsa ham YO'QOLMAYDI.
  2) Sozlanmasa — avvalgidek oddiy lokal aiosqlite (bot.db fayli) ishlatiladi
     (masalan, lokal kompyuterda test qilish uchun qulay). Production'da
     (Render'da) ma'lumotni yo'qotmaslik uchun TURSO_DATABASE_URL sozlash
     SHART.

bot.py bu modulni:
    import db_compat as aiosqlite
ko'rinishida ishlatadi — shu sababli bot.py ichidagi barcha DB funksiyalari
(async with aiosqlite.connect(...), db.row_factory, .execute(), .fetchone(),
.fetchall(), .commit(), aiosqlite.OperationalError, aiosqlite.Row) birorta ham
o'zgarishsiz, xuddi avvalgidek ishlayveradi.
"""
import os
import sqlite3

import aiosqlite as _aiosqlite

TURSO_DATABASE_URL = os.getenv("TURSO_DATABASE_URL", "").strip()
TURSO_AUTH_TOKEN = os.getenv("TURSO_AUTH_TOKEN", "").strip()

# aiosqlite bilan bir xil xato turi va Row klassi — bot.py o'zgarishsiz
# ishlashi uchun shu nomlar bilan qayta eksport qilinadi.
OperationalError = sqlite3.OperationalError
Row = _aiosqlite.Row


def using_turso() -> bool:
    return bool(TURSO_DATABASE_URL)


if using_turso():
    import libsql_client


class _TursoCursor:
    """aiosqlite kursoriga o'xshab ishlaydigan wrapper.

    fetchone()/fetchall() to'g'ridan-to'g'ri dict qaytaradi — bot.py'da
    keyin qilinadigan `dict(row) if row else None` chaqiruvi buzilmaydi,
    chunki dict(dict) ham to'g'ri ishlaydi (nusxa qaytaradi).
    """

    def __init__(self, result_set):
        self._rows = list(result_set.rows) if result_set is not None else []
        self.lastrowid = getattr(result_set, "last_insert_rowid", None)
        # aiosqlite'dagi cursor.rowcount bilan bir xil ism/ma'no — UPDATE/DELETE
        # nechta qatorga ta'sir qilganini bildiradi (masalan atomik "yetarli
        # balans bo'lsagina ayirish" kabi shart bilan yozilgan UPDATE'lar
        # muvaffaqiyatli bo'lganini tekshirish uchun).
        self.rowcount = getattr(result_set, "rows_affected", -1) if result_set is not None else -1
        self._pos = 0

    @staticmethod
    def _row_to_dict(row) -> dict:
        # libsql_client.Row o'zida asdict() metodiga ega — nomi bilan qiymatlar
        # lug'atini to'g'ridan-to'g'ri qaytaradi.
        return row.asdict()

    async def fetchone(self):
        if self._pos >= len(self._rows):
            return None
        d = self._row_to_dict(self._rows[self._pos])
        self._pos += 1
        return d

    async def fetchall(self):
        result = [self._row_to_dict(r) for r in self._rows[self._pos:]]
        self._pos = len(self._rows)
        return result


class _TursoConnection:
    def __init__(self, client):
        self._client = client
        # Turso rejimida ishlatilmaydi (har doim dict qaytariladi), lekin
        # bot.py "db.row_factory = aiosqlite.Row" deb yozganda xato bermasligi
        # uchun oddiy settable atribut sifatida saqlab qo'yamiz.
        self.row_factory = None

    async def execute(self, sql, params=()):
        try:
            result_set = await self._client.execute(sql, list(params) if params else [])
        except Exception as e:
            # bot.py "except aiosqlite.OperationalError" bilan ushlaydi
            # (masalan, ALTER TABLE'da ustun allaqachon mavjud bo'lsa) —
            # shuning uchun har qanday xatoni shu turga o'raymiz.
            raise OperationalError(str(e)) from e
        return _TursoCursor(result_set)

    async def commit(self):
        # libsql HTTP/WS mijozi har bir execute()ni darhol bajaradi
        # (avto-commit), shuning uchun bu yerda qo'shimcha ish shart emas.
        pass

    async def close(self):
        await self._client.close()


def _http_url(url: str) -> str:
    """Turso URL'ni WebSocket (libsql://, wss://) o'rniga HTTP (https://) sxemasiga
    o'giradi.

    Nega: `libsql://` sxemasi `wss://` bilan bir xil — uzoq muddatli WebSocket
    ulanishini talab qiladi. Ba'zi hosting muhitlarida (masalan Render) bu
    WebSocket handshake muvaffaqiyatsiz tugaydi va
    `sqlite3.OperationalError: 400, message='Invalid response status'` xatosi
    chiqadi. HTTP sxemasi esa har bir so'rov uchun oddiy HTTPS chaqiruvidan
    foydalanadi — bu ancha barqaror va Render kabi muhitlarda ham ishonchli
    ishlaydi. Bu botda `transaction()` API ishlatilmaydi (har bir `execute()`
    o'zi avto-commit qiladi), shuning uchun HTTP rejimiga o'tish hech qanday
    funksionallikni yo'qotmaydi.
    """
    if url.startswith("libsql://"):
        return "https://" + url[len("libsql://"):]
    if url.startswith("wss://"):
        return "https://" + url[len("wss://"):]
    if url.startswith("ws://"):
        return "http://" + url[len("ws://"):]
    return url


class _TursoConnCtx:
    async def __aenter__(self):
        self._client = libsql_client.create_client(
            url=_http_url(TURSO_DATABASE_URL),
            auth_token=TURSO_AUTH_TOKEN or None,
        )
        return _TursoConnection(self._client)

    async def __aexit__(self, exc_type, exc, tb):
        await self._client.close()
        return False


def connect(path=None):
    """aiosqlite.connect() bilan bir xil imzo va ishlatilish tarzi.

    TURSO_DATABASE_URL sozlangan bo'lsa Turso'ga (doimiy, tashqi baza),
    aks holda lokal SQLite fayliga (path — masalan bot.db) ulanadi.
    """
    if using_turso():
        return _TursoConnCtx()
    return _aiosqlite.connect(path)
