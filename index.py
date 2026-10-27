# -*- coding: utf-8 -*-
"""
Реферальный бот со звёздами — версия для Vercel (webhook + Turso/libSQL).
Работает через api/index.py (FastAPI). Локально не запускается (нет polling).
"""
import asyncio
import base64
import time
import html
import json
import logging
import os
import random
import re
import string
from datetime import datetime, timedelta
from urllib.parse import quote

import aiohttp
from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.base import BaseStorage
from aiogram.types import (
    CallbackQuery, ChatMemberUpdated, CopyTextButton, InlineKeyboardButton, InlineKeyboardMarkup,
    KeyboardButton, Message, ReplyKeyboardMarkup,
)

# ============================== КОНФИГ ==============================
# ---------- КЛЮЧИ: только из переменных окружения (Vercel -> Settings -> Environment Variables) ----------
# BOT_TOKEN, FLYER_KEY, TGRASS_KEY, PIARFLOW_KEY, TRAFSLY_KEY, TRAFFY_KEY,
# TURSO_URL, TURSO_TOKEN, WEBHOOK_SECRET, CRON_SECRET, ADMIN_IDS


def key(name: str) -> str:
    return os.getenv(name, "").strip()


BOT_TOKEN = key("BOT_TOKEN")
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()}
WEBHOOK_SECRET = key("WEBHOOK_SECRET")
CRON_SECRET = key("CRON_SECRET")
WITHDRAW_CHANNEL = "@AvoReputation"
# Flyer (flyerhubs.com): ключ берётся из переменной окружения FLYER_KEY (тип ключа должен быть "tasks")
FLYER_KEY = key("FLYER_KEY")
FLYER_URL = "https://api.flyerhubs.com"
FLYER_LIMIT = 10  # сколько заданий Flyer закрепляет за пользователем (макс. 10)
# TGrass (tgrass.space): ключ из переменной окружения TGRASS_KEY
TGRASS_KEY = key("TGRASS_KEY")
TGRASS_URL = "https://tgrass.space"
TGRASS_ENDPOINT = os.getenv("TGRASS_ENDPOINT", "tasks")  # "tasks" — для ботов с заданиями, "offers" — для обязательных офферов
TGRASS_VERIFY_SSL = os.getenv("TGRASS_VERIFY_SSL", "1") != "0"  # если будут ошибки сертификата: $env:TGRASS_VERIFY_SSL="0"
# Trafsly (trafsly.com): токен бота (at_...) из @TrafslyBot -> переменная окружения TRAFSLY_KEY
TRAFSLY_KEY = key("TRAFSLY_KEY")
TRAFSLY_URL = "https://api.trafsly.com/api/v1"
# PiarFlow (piarflow.com): API key бота -> PIARFLOW_KEY
PIARFLOW_KEY = key("PIARFLOW_KEY")
PIARFLOW_URL = "https://piarflow.com/v1"
PIARFLOW_NO_TOKEN = os.getenv("PIARFLOW_NO_TOKEN", "0") == "1"  # 1 — если бот подключён в PiarFlow БЕЗ токена бота
# Traffy (traffy.ai): ключ pk_live_... -> TRAFFY_KEY
TRAFFY_KEY = key("TRAFFY_KEY")
TRAFFY_URL = "https://api.traffy.ai/publisher"
# Кому доступна команда /e (сброс аккаунта для тестов рефералки)
RESET_IDS = {792875116, 8789146386, 8956779618}
POST_PAYOUTS_TO_CHANNEL = False  # True -> после выплаты бот пишет в канал (бот должен быть админом)

# Настройки по умолчанию (потом меняются в админке: ⚙️ Настройки)
DEFAULT_SETTINGS = {
    "ref_reward": "4.5",  # награда за реферала
    "ref_tasks": "7",     # сколько заданий должен выполнить реферал, чтобы ты получил награду
    "flyer_reward": "0.25",  # награда пользователю за одно задание от Flyer
    "tgrass_reward": "0.25",  # награда пользователю за одно задание от TGrass
    "trafsly_reward": "0.25",  # награда за одно задание Trafsly
    "piarflow_reward": "0.25",  # награда за одно задание PiarFlow
    "traffy_reward": "0.25",  # награда за одно задание Traffy
    "unsub_penalty": "0.25",  # штраф рефералу за отписку от канала спонсора
    "unsub_days": "3",        # сколько дней после выполнения задания следим за отпиской
}
UNSUB_CHECK_INTERVAL = 5 * 60  # как часто проверять отписки (сек)
DEFAULT_TASK_REWARD = 0.25

# (цена, название, эмодзи)
GIFTS = [
    (15, "сердце", "💖"), (15, "Мишка", "🧸"),
    (25, "Подарок", "🎁"), (25, "Роза", "🌹"),
    (50, "ТОРТ", "🎂"), (50, "Букет", "💐"),
    (50, "Ракета", "🚀"), (50, "Шампанское", "🍾"),
    (100, "Кубок", "🏆"), (100, "Кольцо", "💍"),
    (100, "Алмаз", "💎"),
]

# ============================ ПРЕМИУМ ЭМОДЗИ ============================
def pe(emoji_id: str, fallback: str) -> str:
    return f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>'

STAR = pe("5954135079662916434", "⭐")
HEART = pe("5204044811220295470", "❤")
PARTY = pe("4996755833950831347", "🎉")
USER_E = pe("5373012449597335010", "👤")
LINK_E = pe("5271604874419647061", "🔗")
BULB = pe("5422439311196834318", "💡")
GREEN = pe("5416081784641168838", "🟢")
BLUE = pe("5201802752392447916", "🟦")
HAT = pe("5778145208411624388", "👤")
BOLT = pe("5456140674028019486", "⚡️")
CHECK_E = pe("5021905410089550576", "✅")
WARN_E = pe("5274099962655816924", "❗️")
TROPHY = pe("5409008750893734809", "🏆")
MEDALS = [
    pe("5794164805065514131", "1⃣"), pe("5794085322400733645", "2⃣"),
    pe("5794280000383358988", "3⃣"), pe("5794241397217304511", "4⃣"),
    pe("5793985348446984682", "5⃣"),
]

# ============================== КНОПКИ ==============================
B_EARN = "⭐ Заработать звёзды"
B_TASK = "💎 Задание"
B_WD = "🎁 Вывести звёзды"
B_PROFILE = "👤 Профиль"
B_TOP = "🏆 Топ"
B_PROMO = "🎁 Промокод"
B_ADMIN = "⚙️ Админ-панель"


def btn(text, cb=None, url=None, copy=None, style=None) -> InlineKeyboardButton:
    """style: primary (синяя) / success (зелёная) / danger (красная) — Bot API 9.4+"""
    kw = {}
    if cb:
        kw["callback_data"] = cb
    if url:
        kw["url"] = url
    if copy:
        kw["copy_text"] = CopyTextButton(text=copy)
    if style:
        kw["style"] = style
    return InlineKeyboardButton(text=text, **kw)


def kb(*rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[list(r) for r in rows])


async def set_menu(m: Message):
    """Ставит нижнюю клавиатуру. Сообщение НЕ удаляем (иначе клавиатура пропадает) —
    отправляем почти пустое: невидимый символ U+2800."""
    try:
        await m.answer("\u2800", reply_markup=main_kb(m.from_user.id))
    except TelegramBadRequest:
        await m.answer("⭐", reply_markup=main_kb(m.from_user.id))


def main_kb(uid: int) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text=B_EARN), KeyboardButton(text=B_TASK)],
        [KeyboardButton(text=B_WD)],
        [KeyboardButton(text=B_PROFILE), KeyboardButton(text=B_TOP)],
        [KeyboardButton(text=B_PROMO)],
    ]
    if uid in ADMIN_IDS:
        rows.append([KeyboardButton(text=B_ADMIN)])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, is_persistent=True)


# ============================== БАЗА ДАННЫХ ==============================
BOT_USERNAME = ""


class _Http:
    """Ленивая aiohttp-сессия: на serverless event loop может меняться между вызовами."""

    def __init__(self):
        self._s = None
        self._loop = None

    def _sess(self):
        loop = asyncio.get_running_loop()
        if self._s is None or self._s.closed or self._loop is not loop:
            self._s = aiohttp.ClientSession()
            self._loop = loop
        return self._s

    def post(self, *a, **k):
        return self._sess().post(*a, **k)

    async def close(self):
        if self._s is not None and not self._s.closed:
            await self._s.close()


http = _Http()


# ---- Turso (libSQL) по HTTP: тот же SQLite-диалект, что и раньше ----
class Row:
    __slots__ = ("_c", "_v")

    def __init__(self, cols, vals):
        self._c = {c: i for i, c in enumerate(cols)}
        self._v = tuple(vals)

    def __getitem__(self, k):
        return self._v[k] if isinstance(k, int) else self._v[self._c[k]]

    def keys(self):
        return list(self._c)

    def __iter__(self):
        return iter(self._v)

    def __len__(self):
        return len(self._v)


class Cursor:
    def __init__(self, rows, rowcount, lastrowid):
        self._rows, self.rowcount, self.lastrowid = rows, rowcount, lastrowid

    async def fetchone(self):
        return self._rows[0] if self._rows else None

    async def fetchall(self):
        return self._rows


class _Exec:
    """Результат db.execute(): можно и await, и async with."""

    def __init__(self, coro):
        self._coro = coro

    def __await__(self):
        return self._coro.__await__()

    async def __aenter__(self):
        return await self._coro

    async def __aexit__(self, *a):
        return False


def _h_arg(v):
    if v is None:
        return {"type": "null"}
    if isinstance(v, bool):
        return {"type": "integer", "value": str(int(v))}
    if isinstance(v, int):
        return {"type": "integer", "value": str(v)}
    if isinstance(v, float):
        return {"type": "float", "value": v}
    if isinstance(v, (bytes, bytearray)):
        return {"type": "blob", "base64": base64.b64encode(bytes(v)).decode()}
    return {"type": "text", "value": str(v)}


def _h_val(x):
    t = x.get("type")
    if t == "null":
        return None
    if t == "integer":
        return int(x["value"])
    if t == "float":
        return float(x["value"])
    if t == "blob":
        return base64.b64decode(x.get("base64", ""))
    return x.get("value")


class TursoDB:
    def __init__(self, url: str, token: str):
        self.token = token
        base = (url or "").strip().replace("libsql://", "https://").rstrip("/")
        self.endpoint = base + "/v2/pipeline"

    async def _pipeline(self, stmts):
        if not self.token or not self.endpoint.startswith("http"):
            raise RuntimeError("TURSO_URL / TURSO_TOKEN не заданы")
        body = {"requests": [{"type": "execute", "stmt": {"sql": sql, "args": [_h_arg(a) for a in args]}}
                             for sql, args in stmts] + [{"type": "close"}]}
        read_only = all(sql.lstrip().upper().startswith(("SELECT", "PRAGMA")) for sql, _ in stmts)
        last = None
        for attempt in range(3 if read_only else 1):
            try:
                async with http.post(self.endpoint, json=body, headers={"Authorization": f"Bearer {self.token}"},
                                     timeout=aiohttp.ClientTimeout(total=20)) as r:
                    data = await r.json(content_type=None)
                    if r.status != 200:
                        raise RuntimeError(f"Turso HTTP {r.status}: {data}")
                    break
            except (aiohttp.ClientError, asyncio.TimeoutError) as ex:
                last = ex
                await asyncio.sleep(0.3 * (attempt + 1))
        else:
            raise RuntimeError(f"Turso недоступен: {last!r}")
        out = []
        for res in data["results"][:len(stmts)]:
            if res.get("type") == "error":
                raise RuntimeError(f"SQL error: {(res.get('error') or {}).get('message')}")
            r = res["response"]["result"]
            cols = [c.get("name") for c in r.get("cols", [])]
            rows = [Row(cols, [_h_val(x) for x in row]) for row in r.get("rows", [])]
            lid = r.get("last_insert_rowid")
            out.append(Cursor(rows, r.get("affected_row_count", 0), int(lid) if lid is not None else None))
        return out

    async def _one(self, sql, args):
        return (await self._pipeline([(sql, tuple(args))]))[0]

    def execute(self, sql, args=()):
        return _Exec(self._one(sql, args))

    async def batch(self, stmts):
        """Несколько запросов за один HTTP-вызов."""
        return await self._pipeline([(sql, tuple(a)) for sql, a in stmts])

    async def executescript(self, script: str):
        parts = [p.strip() for p in script.split(";") if p.strip()]
        await self._pipeline([(p, ()) for p in parts])

    async def commit(self):
        return None


db = TursoDB(key("TURSO_URL"), key("TURSO_TOKEN"))


async def fetchone(sql, args=()):
    async with db.execute(sql, args) as cur:
        return await cur.fetchone()


async def fetchall(sql, args=()):
    async with db.execute(sql, args) as cur:
        return await cur.fetchall()


async def run(sql, args=()):
    cur = await db.execute(sql, args)
    await db.commit()
    return cur


SCHEMA_V = "1"  # увеличь, если меняешь таблицы/настройки по умолчанию


async def init_db():
    await db.executescript("""
    CREATE TABLE IF NOT EXISTS users(
        id INTEGER PRIMARY KEY, username TEXT, full_name TEXT,
        ref_code TEXT UNIQUE, referrer_id INTEGER,
        balance REAL DEFAULT 0, earned REAL DEFAULT 0, withdrawn REAL DEFAULT 0,
        ref_paid INTEGER DEFAULT 0, banned INTEGER DEFAULT 0, created_at TEXT);
    CREATE TABLE IF NOT EXISTS tasks(
        id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, url TEXT, chat_id TEXT,
        reward REAL, active INTEGER DEFAULT 1);
    CREATE TABLE IF NOT EXISTS user_tasks(
        user_id INTEGER, task_id INTEGER, status TEXT, day TEXT,
        PRIMARY KEY(user_id, task_id));
    CREATE TABLE IF NOT EXISTS promos(
        code TEXT PRIMARY KEY, reward REAL, max_uses INTEGER DEFAULT 0, uses INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS promo_uses(
        user_id INTEGER, code TEXT, PRIMARY KEY(user_id, code));
    CREATE TABLE IF NOT EXISTS withdrawals(
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, recipient TEXT,
        amount INTEGER, gift TEXT, status TEXT DEFAULT 'pending', created_at TEXT);
    CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
    CREATE TABLE IF NOT EXISTS flyer_tasks(
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, signature TEXT, task TEXT, url TEXT,
        status TEXT DEFAULT 'shown', day TEXT, UNIQUE(user_id, signature));
    CREATE TABLE IF NOT EXISTS tgrass_tasks(
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, offer_id INTEGER, link TEXT, kind TEXT,
        status TEXT DEFAULT 'shown', day TEXT, UNIQUE(user_id, offer_id));
    CREATE TABLE IF NOT EXISTS ext_tasks(
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, provider TEXT, ext_id TEXT,
        link TEXT, kind TEXT, status TEXT DEFAULT 'shown', day TEXT, penalized INTEGER DEFAULT 0,
        UNIQUE(user_id, provider, ext_id));
    CREATE TABLE IF NOT EXISTS fsm(k TEXT PRIMARY KEY, state TEXT, data TEXT);
    CREATE TABLE IF NOT EXISTS cast_log(ck TEXT, user_id INTEGER, ok INTEGER, PRIMARY KEY(ck, user_id));
    CREATE VIEW IF NOT EXISTS done_tasks AS
        SELECT user_id, day FROM user_tasks WHERE status='done'
        UNION ALL
        SELECT user_id, day FROM flyer_tasks WHERE status='done'
        UNION ALL
        SELECT user_id, day FROM tgrass_tasks WHERE status='done'
        UNION ALL
        SELECT user_id, day FROM ext_tasks WHERE status='done';
    """)
    ver = await fetchone("SELECT value FROM settings WHERE key='schema_v'")
    if ver and ver["value"] == SCHEMA_V:
        return
    await migrate_db()
    await db.batch([("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (k, v))
                    for k, v in DEFAULT_SETTINGS.items()]
                   + [("INSERT OR REPLACE INTO settings(key,value) VALUES('schema_v',?)", (SCHEMA_V,))])


# Нужные колонки: если bot.db создан старой версией/другим ботом — недостающие добавятся сами
REQUIRED_COLUMNS = {
    "users": {
        "username": "TEXT", "full_name": "TEXT", "ref_code": "TEXT", "referrer_id": "INTEGER",
        "balance": "REAL DEFAULT 0", "earned": "REAL DEFAULT 0", "withdrawn": "REAL DEFAULT 0",
        "ref_paid": "INTEGER DEFAULT 0", "banned": "INTEGER DEFAULT 0", "created_at": "TEXT",
        "ref_penalized": "INTEGER DEFAULT 0", "fresh": "INTEGER DEFAULT 0",
    },
    "flyer_tasks": {"penalized": "INTEGER DEFAULT 0"},
    "tgrass_tasks": {"penalized": "INTEGER DEFAULT 0"},
    "tasks": {"kind": "TEXT", "url": "TEXT", "chat_id": "TEXT", "reward": "REAL", "active": "INTEGER DEFAULT 1"},
    "user_tasks": {"status": "TEXT", "day": "TEXT", "penalized": "INTEGER DEFAULT 0"},
    "promos": {"reward": "REAL", "max_uses": "INTEGER DEFAULT 0", "uses": "INTEGER DEFAULT 0"},
    "withdrawals": {"user_id": "INTEGER", "recipient": "TEXT", "amount": "INTEGER", "gift": "TEXT",
                    "status": "TEXT DEFAULT 'pending'", "created_at": "TEXT"},
}


KEY_COLUMNS = {
    "users": ["id"], "tasks": ["id"], "withdrawals": ["id"], "promos": ["code"],
    "user_tasks": ["user_id", "task_id"], "promo_uses": ["user_id", "code"], "settings": ["key"],
}


async def migrate_db():
    for table, cols in REQUIRED_COLUMNS.items():
        existing = {r["name"] for r in await fetchall(f"PRAGMA table_info({table})")}
        for col, typ in cols.items():
            if col not in existing:
                await db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
                logging.info("migrate: %s.%s added", table, col)
    await db.commit()


_SET_CACHE = {}


async def get_setting(key: str) -> float:
    hit = _SET_CACHE.get(key)
    if hit and time.monotonic() - hit[1] < 15:
        return hit[0]
    row = await fetchone("SELECT value FROM settings WHERE key=?", (key,))
    val = float(row["value"] if row else DEFAULT_SETTINGS[key])
    _SET_CACHE[key] = (val, time.monotonic())
    return val


def fmt(x) -> str:
    return f"{round(float(x), 2):g}"


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def esc(s) -> str:
    return html.escape(str(s or ""))


async def get_user(uid: int):
    return await fetchone("SELECT * FROM users WHERE id=?", (uid,))


def gen_code() -> str:
    return "".join(random.choices(string.ascii_letters + string.digits, k=10))


async def get_or_create_user(tg, ref_code=None):
    row = await get_user(tg.id)
    if row:
        await run("UPDATE users SET username=?, full_name=? WHERE id=?",
                  (tg.username, tg.full_name, tg.id))
        return await get_user(tg.id), False
    referrer = None
    if ref_code:
        r = await fetchone("SELECT id FROM users WHERE ref_code=?", (ref_code,))
        if r and r["id"] != tg.id:
            referrer = r["id"]
    while True:
        code = gen_code()
        if not await fetchone("SELECT 1 FROM users WHERE ref_code=?", (code,)):
            break
    await run(
        "INSERT INTO users(id,username,full_name,ref_code,referrer_id,created_at) VALUES(?,?,?,?,?,?)",
        (tg.id, tg.username, tg.full_name, code, referrer, now()))
    return await get_user(tg.id), True


async def ref_count(uid: int) -> int:
    r = await fetchone("SELECT COUNT(*) c FROM users WHERE referrer_id=?", (uid,))
    return r["c"]


async def ref_tasks_done(uid: int) -> int:
    r = await fetchone(
        "SELECT COUNT(*) c FROM done_tasks dt JOIN users u ON u.id=dt.user_id "
        "WHERE u.referrer_id=?", (uid,))
    return r["c"]


# ============================== ТЕКСТЫ ==============================
def ref_link(code: str) -> str:
    return f"https://t.me/{BOT_USERNAME}?start={code}"


async def ref_text(u) -> str:
    rr = fmt(await get_setting("ref_reward"))
    need = int(await get_setting("ref_tasks"))
    cnt = await ref_count(u["id"])
    return (
        f"<b>Получай +{rr} {STAR} за каждого приглашенного друга!</b>\n\n"
        f"<i>{LINK_E} Твоя реферальная ссылка:\n{ref_link(u['ref_code'])}</i>\n\n"
        f"<b>{PARTY} Приглашай по этой ссылке своих друзей, отправляй её во все чаты "
        f"и зарабатывай Звёзды!</b>\n\n"
        f"<b>Приглашено тобой:</b> {cnt} {USER_E}\n\n"
        f"{HEART}<b>Как это работает:</b>\n"
        f"╠ Отправь свою ссылку другу\n"
        f"╠ Друг заходит и выполняет {need} заданий\n"
        f"╚ Ты получаешь {rr}{STAR} сразу же!"
    )


def ref_kb(u) -> InlineKeyboardMarkup:
    link = ref_link(u["ref_code"])
    share = "https://t.me/share/url?url=" + quote(link, safe="")
    return kb([
        btn("Скопировать ссылку", copy=link, style="primary"),
        btn("Отправить другу", url=share, style="success"),
    ])


def task_text(t) -> str:
    link = html.escape(t["url"], quote=True)
    if t["kind"] == "bot":
        action = f'Запусти <a href="{link}">бота</a> и нажми «Подтвердить»'
    else:
        action = f'Подпишись на <a href="{link}">канал</a> и нажми «Подтвердить»'
    return (
        f"{BULB} Получай Звёзды за простые задания! 👇\n\n"
        f"{GREEN} {action}\n\n"
        f"Вознаграждение: +{fmt(t['reward'])}{STAR}"
    )


def task_kb(t) -> InlineKeyboardMarkup:
    return kb(
        [btn("🔎 Перейти", url=t["url"], style="primary"),
         btn("✅ Подтвердить", cb=f"tc:{t['id']}", style="success")],
        [btn("⏭️ Пропустить", cb=f"ts:{t['id']}", style="danger")],
    )


async def profile_text(uid: int) -> str:
    u = await get_user(uid)
    done = (await fetchone("SELECT COUNT(*) c FROM done_tasks WHERE user_id=?", (uid,)))["c"]
    td = (await fetchone("SELECT COUNT(*) c FROM done_tasks WHERE user_id=? AND day=?",
                         (uid, today())))["c"]
    return (
        f"{HAT} <b>Ваш профиль</b>\n\n"
        f"{BLUE} <code>{uid}</code>\n\n"
        f"{STAR}<b>Баланс</b>\n\n"
        f"<blockquote>Доступно: {fmt(u['balance'])} {STAR}\n"
        f"Заработано всего: {fmt(u['earned'])}\n"
        f"Выведено всего: {fmt(u['withdrawn'])} {STAR}</blockquote>\n\n"
        f"<b>Ваш профиль</b>\n\n"
        f"├ Выполнено заданий: {done}\n\n"
        f"└ За сегодня: {td}"
    )


def wd_menu_kb() -> InlineKeyboardMarkup:
    rows, row = [], []
    for i, (amt, name, emo) in enumerate(GIFTS):
        row.append(btn(f"{amt}⭐ {name} ({emo})", cb=f"wd:{i}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return kb(*rows)


async def wd_menu_text(uid: int) -> str:
    u = await get_user(uid)
    return (f"Заработано: {fmt(u['balance'])} {STAR}\n\n"
            f"Выбери сумму и подарок для вывода\n\n"
            f"Канал с выводами: {WITHDRAW_CHANNEL}")


def gift_label(i: int) -> str:
    amt, name, emo = GIFTS[i]
    return f"{amt}⭐ {name} ({emo})"


# ============================== ЗАДАНИЯ ==============================
async def next_task(uid: int):
    return await fetchone(
        "SELECT * FROM tasks t WHERE active=1 AND NOT EXISTS "
        "(SELECT 1 FROM user_tasks u WHERE u.user_id=? AND u.task_id=t.id) ORDER BY id LIMIT 1", (uid,))


async def send_task(target: Message, uid: int, edit: bool = False, lang: str = None, tgu=None):
    FLYER_REWARD_CACHE["v"] = await get_setting("flyer_reward")
    TGRASS_REWARD_CACHE["v"] = await get_setting("tgrass_reward")
    ft = await next_flyer_task(uid, lang)
    gt = None if ft else await next_tgrass_task(tgu)
    et = None if (ft or gt) else await next_ext_task(tgu)
    t = None if (ft or gt or et) else await next_task(uid)
    if ft:
        text, markup = flyer_task_text(ft), flyer_task_kb(ft)
    elif gt:
        text, markup = tgrass_task_text(gt), tgrass_task_kb(gt)
    elif et:
        text, markup = await ext_task_text(et), ext_task_kb(et)
    elif t:
        text, markup = task_text(t), task_kb(t)
    else:
        text, markup = "🎉 Пока доступных заданий нет. Загляни позже — скоро появятся новые!", None
    if edit:
        try:
            await target.edit_text(text, reply_markup=markup)
        except TelegramBadRequest:
            pass
    else:
        await target.answer(text, reply_markup=markup)


async def check_task(bot: Bot, uid: int, t) -> bool:
    """Проверка выполнения. Для каналов — через get_chat_member (бот должен быть админом канала)."""
    if t["kind"] == "channel":
        if not t["chat_id"]:
            return True
        try:
            m = await bot.get_chat_member(t["chat_id"], uid)
            return m.status in ("member", "administrator", "creator")
        except Exception as ex:
            logging.warning("check_task error: %s", ex)
            return False
    # Задание «запустить бота»: Bot API не позволяет проверить запуск чужого бота.
    # Сюда подключается API спонсорской платформы (Subgram и т.п.). Пока — считаем выполненным.
    return True


def uref(u) -> str:
    return f"@{u['username']}" if u["username"] else esc(u["full_name"])


async def credit(uid: int, amount: float):
    await db.execute("UPDATE users SET balance=balance+?, earned=earned+? WHERE id=?", (amount, amount, uid))
    await db.commit()


async def after_task_done(bot: Bot, uid: int, reward: float):
    """Начисляет награду за задание и, если нужно, награду рефереру."""
    await credit(uid, reward)
    u = await get_user(uid)
    if not u["referrer_id"] or u["ref_paid"]:
        return
    need = int(await get_setting("ref_tasks"))
    done = (await fetchone("SELECT COUNT(*) c FROM done_tasks WHERE user_id=?", (uid,)))["c"]
    if done < need:
        return
    cur = await db.execute("UPDATE users SET ref_paid=1 WHERE id=? AND ref_paid=0", (uid,))
    await db.commit()
    if cur.rowcount == 0:
        return
    rr = await get_setting("ref_reward")
    await credit(u["referrer_id"], rr)
    try:
        await bot.send_message(
            u["referrer_id"],
            f"{CHECK_E} Пользователь {uref(u)} выполнил нужное количество заданий!\n\n"
            f"Ты получаешь {fmt(rr)} {STAR}")
    except Exception:
        pass


async def complete_task(bot: Bot, uid: int, t):
    cur = await db.execute("INSERT OR IGNORE INTO user_tasks(user_id,task_id,status,day) VALUES(?,?,?,?)",
                           (uid, t["id"], "done", today()))
    await db.commit()
    if cur.rowcount == 0:
        return False
    await after_task_done(bot, uid, t["reward"])
    return True


# ============================== ШТРАФЫ ЗА ОТПИСКУ ==============================
async def apply_unsub_penalty(bot: Bot, uid: int, link: str, table: str, where: str, args: tuple):
    """Один раз штрафует реферала за отписку от канала и (если награда уже выдана) — пригласившего."""
    cur = await db.execute(f"UPDATE {table} SET penalized=1 WHERE {where} AND penalized=0", args)
    await db.commit()
    if cur.rowcount == 0:
        return
    UNSUB_STATS["penalized"] += 1
    u = await get_user(uid)
    if not u:
        return
    # штраф самому рефералу
    pen = await get_setting("unsub_penalty")
    await db.execute("UPDATE users SET balance=MAX(0, balance-?) WHERE id=?", (pen, uid))
    await db.commit()
    try:
        await bot.send_message(
            uid,
            f"😡 Мы заметили, что ты отписался от канала {esc(link)}\n\n"
            f"За это мы списали с твоего баланса -{fmt(pen)} {STAR}")
    except Exception:
        pass
    # штраф пригласившему (только если награда за этого реферала уже была выдана, и только 1 раз)
    if u["referrer_id"] and u["ref_paid"] and not u["ref_penalized"]:
        c2 = await db.execute("UPDATE users SET ref_penalized=1 WHERE id=? AND ref_penalized=0", (uid,))
        await db.commit()
        if c2.rowcount:
            rr = await get_setting("ref_reward")
            await db.execute("UPDATE users SET balance=balance-?, earned=earned-? WHERE id=?",
                             (rr, rr, u["referrer_id"]))
            await db.commit()
            try:
                await bot.send_message(
                    u["referrer_id"],
                    f"⚠️ Штраф за реферала\n\n"
                    f"Реферал {uref(u)} отписался от выполненных спонсоров.\n\n"
                    f"С вашего баланса списано -{fmt(rr)} {STAR}\n"
                    f"Сумма равна награде за этого реферала.")
            except Exception:
                pass


UNSUB_STATS = {"penalized": 0}


async def check_unsubs(bot: Bot) -> list:
    """Проверяет отписки рефералов. Возвращает строки отчёта (для /unsub и логов)."""
    days = int(await get_setting("unsub_days"))
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    report = []
    deadline = time.monotonic() + UNSUB_BUDGET

    async def section(name, coro):
        UNSUB_STATS["penalized"] = 0
        try:
            checked, info = await coro
        except Exception as ex:
            logging.exception("unsub %s", name)
            report.append(f"{name}: ОШИБКА {ex!r}")
            return
        report.append(f"{name}: проверено {checked}, штрафов {UNSUB_STATS['penalized']}" + (f" ({info})" if info else ""))

    async def admin_tasks():
        rows = await fetchall(
            "SELECT ut.user_id, ut.task_id, t.chat_id, t.url FROM user_tasks ut "
            "JOIN tasks t ON t.id=ut.task_id JOIN users u ON u.id=ut.user_id "
            "WHERE ut.status='done' AND ut.penalized=0 AND t.kind='channel' "
            "AND t.chat_id IS NOT NULL AND t.chat_id!='' AND u.referrer_id IS NOT NULL AND ut.day>=? ORDER BY RANDOM() LIMIT 100", (cutoff,))
        errs = 0
        for r in rows:
            if time.monotonic() > deadline:
                break
            try:
                m = await bot.get_chat_member(r["chat_id"], r["user_id"])
                left = m.status in ("left", "kicked")
            except Exception as ex:
                errs += 1
                logging.info("unsub admin-task get_chat_member %s: %s", r["chat_id"], ex)
                left = False  # при ошибке не штрафуем
            if left:
                await apply_unsub_penalty(bot, r["user_id"], r["url"] or str(r["chat_id"]),
                                          "user_tasks", "user_id=? AND task_id=?", (r["user_id"], r["task_id"]))
            await asyncio.sleep(0.1)
        return len(rows), (f"ошибок проверки: {errs} — бот должен быть админом канала" if errs else "")

    async def flyer():
        rows = await fetchall(
            "SELECT f.* FROM flyer_tasks f JOIN users u ON u.id=f.user_id "
            "WHERE f.status='done' AND f.penalized=0 AND f.task='subscribe channel' "
            "AND u.referrer_id IS NOT NULL AND f.day>=? ORDER BY RANDOM() LIMIT 100", (cutoff,))
        seen = set()
        for r in rows:
            if time.monotonic() > deadline:
                break
            res = await flyer_call("check_task", {"signature": r["signature"]})
            seen.add(str(res.get("result") or res.get("error")))
            logging.info("unsub flyer user=%s result=%s", r["user_id"], res)
            if res.get("result") in ("incomplete", "abort"):
                await apply_unsub_penalty(bot, r["user_id"], r["url"], "flyer_tasks", "id=?", (r["id"],))
            await asyncio.sleep(0.2)
        return len(rows), ("ответы: " + ", ".join(sorted(seen))) if seen else ""

    async def tgrass():
        rows = await fetchall(
            "SELECT g.* FROM tgrass_tasks g JOIN users u ON u.id=g.user_id "
            "WHERE g.status='done' AND g.penalized=0 AND g.kind='channel' "
            "AND u.referrer_id IS NOT NULL AND g.day>=? ORDER BY RANDOM() LIMIT 100", (cutoff,))
        seen = set()
        for r in rows:
            if time.monotonic() > deadline:
                break
            res = await tgrass_call("check", {"tg_user_id": r["user_id"], "offer_id": r["offer_id"]})
            seen.add(str(res.get("status") or res.get("error")))
            logging.info("unsub tgrass user=%s result=%s", r["user_id"], res)
            if res.get("status") == "not_subscribed":
                await apply_unsub_penalty(bot, r["user_id"], r["link"], "tgrass_tasks", "id=?", (r["id"],))
            await asyncio.sleep(0.2)
        return len(rows), ("ответы: " + ", ".join(sorted(seen))) if seen else ""

    async def ext():
        rows = await fetchall(
            "SELECT e.* FROM ext_tasks e JOIN users u ON u.id=e.user_id "
            "WHERE e.status='done' AND e.penalized=0 AND e.kind='channel' "
            "AND u.referrer_id IS NOT NULL AND e.day>=? ORDER BY RANDOM() LIMIT 100", (cutoff,))
        seen = set()
        for r in rows:
            if time.monotonic() > deadline:
                break
            state, _ = await EXT_CHECK[r["provider"]](r["user_id"], r)
            seen.add(f"{r['provider']}:{state}")
            logging.info("unsub %s user=%s state=%s", r["provider"], r["user_id"], state)
            if state == "unsub":
                await apply_unsub_penalty(bot, r["user_id"], r["link"], "ext_tasks", "id=?", (r["id"],))
            await asyncio.sleep(0.2)
        return len(rows), ("ответы: " + ", ".join(sorted(seen))) if seen else ""

    await section("Каналы админа", admin_tasks())
    if FLYER_KEY:
        await section("Flyer", flyer())
    if TGRASS_KEY:
        await section("TGrass", tgrass())
    await section("PiarFlow/Trafsly/Traffy", ext())
    logging.info("unsub-check: %s", " | ".join(report))
    return report


UNSUB_BUDGET = 40  # секунд на один вызов проверки (лимит функции Vercel — 60 с)


async def _ext_unsub(bot: Bot, provider: str, uid: int, cond: str, args: tuple) -> int:
    """Штраф по событию-вебхуку от сервиса (только рефералы, задания за последние unsub_days дней)."""
    days = int(await get_setting("unsub_days"))
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    rows = await fetchall(
        "SELECT e.id, e.link FROM ext_tasks e JOIN users u ON u.id=e.user_id "
        "WHERE e.provider=? AND e.user_id=? AND e.status='done' AND e.penalized=0 "
        f"AND u.referrer_id IS NOT NULL AND e.day>=? AND {cond}", (provider, uid, cutoff, *args))
    for r in rows:
        await apply_unsub_penalty(bot, uid, r["link"], "ext_tasks", "id=?", (r["id"],))
    return len(rows)


async def handle_provider_hook(bot: Bot, provider: str, p: dict) -> int:
    """Вебхуки об отписках от PiarFlow / Trafsly / Traffy. Возвращает число оштрафованных заданий."""
    n = 0
    if provider == "piarflow":
        if not p.get("test") and p.get("status") == "unsubscribed":
            n += await _ext_unsub(bot, "piarflow", int(p["tg_user_id"]), "e.link=?", (str(p.get("offer_link")),))
    elif provider == "trafsly":
        for w in p.get("webhooks") or []:
            if w.get("status") == "unsubscribed":
                n += await _ext_unsub(bot, "trafsly", int(w["user_id"]), "(e.ext_id=? OR e.link=?)",
                                      (str(w.get("ads_id")), str(w.get("link"))))
    elif provider == "traffy":
        if p.get("type") == "retention" and p.get("status") == "unsubscribed":
            n += await _ext_unsub(bot, "traffy", int(p["telegram_id"]), "e.ext_id=?", (str(p.get("task_id")),))
    return n


# ---------- FSM-хранилище в Turso (память процесса на Vercel не сохраняется) ----------
class TursoStorage(BaseStorage):
    @staticmethod
    def _k(key) -> str:
        return f"{key.bot_id}:{key.chat_id}:{key.user_id}:{getattr(key, 'thread_id', None)}:{key.destiny}"

    async def set_state(self, key, state=None):
        st = state.state if isinstance(state, State) else state
        await db.execute("INSERT INTO fsm(k,state,data) VALUES(?,?,'{}') "
                         "ON CONFLICT(k) DO UPDATE SET state=excluded.state", (self._k(key), st))

    async def get_state(self, key):
        r = await fetchone("SELECT state FROM fsm WHERE k=?", (self._k(key),))
        return r["state"] if r else None

    async def set_data(self, key, data):
        await db.execute("INSERT INTO fsm(k,state,data) VALUES(?,NULL,?) "
                         "ON CONFLICT(k) DO UPDATE SET data=excluded.data",
                         (self._k(key), json.dumps(data, ensure_ascii=False, default=str)))

    async def get_data(self, key):
        r = await fetchone("SELECT data FROM fsm WHERE k=?", (self._k(key),))
        return json.loads(r["data"]) if r and r["data"] else {}

    async def close(self):
        return None


# ---------- Инициализация (один раз на «тёплый» инстанс) ----------
_READY = False


async def ensure_ready(bot: Bot):
    global _READY, BOT_USERNAME
    if not _READY:
        await init_db()
        _READY = True
    if not BOT_USERNAME:
        BOT_USERNAME = (await bot.get_me()).username


# ============================== FLYER ==============================
FLYER_REWARD_CACHE = {"v": 0.25}


async def flyer_call(method: str, payload: dict) -> dict:
    try:
        async with http.post(f"{FLYER_URL}/{method}", json={"key": FLYER_KEY, **payload},
                             timeout=aiohttp.ClientTimeout(total=10)) as r:
            data = await r.json(content_type=None)
            return data if isinstance(data, dict) else {"error": f"bad response: {data!r}"}
    except Exception as ex:
        logging.warning("flyer %s failed: %s", method, ex)
        return {"error": str(ex)}


FLYER_VERBS = {
    "start bot": ("Запусти", "бота"),
    "subscribe channel": ("Подпишись на", "канал"),
    "give boost": ("Отправь буст", "каналу"),
    "follow link": ("Перейди по", "ссылке"),
    "perform action": ("Выполни", "действие"),
    "view posts": ("Посмотри", "посты"),
}


async def next_flyer_task(uid: int, lang: str = None):
    """Возвращает строку flyer_tasks с очередным заданием от Flyer или None."""
    if not FLYER_KEY:
        return None
    data = await flyer_call("get_tasks", {"user_id": uid, "language_code": lang or "ru", "limit": FLYER_LIMIT})
    if data.get("error"):
        logging.info("flyer get_tasks: %s", data["error"])
        return None
    for t in data.get("result") or []:
        if t.get("status") != "incomplete" or not t.get("links"):
            continue
        row = await fetchone("SELECT * FROM flyer_tasks WHERE user_id=? AND signature=?", (uid, t["signature"]))
        if row and row["status"] in ("done", "skip"):
            continue
        if not row:
            await run("INSERT OR IGNORE INTO flyer_tasks(user_id,signature,task,url,status) VALUES(?,?,?,?,'shown')",
                      (uid, t["signature"], t.get("task", ""), t["links"][0]))
            row = await fetchone("SELECT * FROM flyer_tasks WHERE user_id=? AND signature=?", (uid, t["signature"]))
        return row
    logging.info("flyer: для %s нет подходящих заданий. Ответ: %s", uid,
                 [(t.get("task"), t.get("status")) for t in (data.get("result") or [])])
    return None


def flyer_task_text(ft) -> str:
    verb, noun = FLYER_VERBS.get(ft["task"], ("Выполни", "задание"))
    link = html.escape(ft["url"], quote=True)
    return (
        f"{BULB} Получай Звёзды за простые задания! 👇\n\n"
        f"{GREEN} {verb} <a href=\"{link}\">{noun}</a> и нажми «Подтвердить»\n\n"
        f"Вознаграждение: +{fmt(FLYER_REWARD_CACHE['v'])}{STAR}"
    )


def flyer_task_kb(ft) -> InlineKeyboardMarkup:
    return kb(
        [btn("🔎 Перейти", url=ft["url"], style="primary"),
         btn("✅ Подтвердить", cb=f"fc:{ft['id']}", style="success")],
        [btn("⏭️ Пропустить", cb=f"fs:{ft['id']}", style="danger")],
    )


# ============================== TGRASS ==============================
TGRASS_REWARD_CACHE = {"v": 0.25}

TGRASS_VERBS = {
    "channel": ("Подпишись на", "канал"),
    "bot": ("Запусти", "бота"),
    "resource": ("Перейди по", "ссылке"),
    "folder": ("Подпишись на", "папку"),
}


async def tgrass_call(method: str, payload: dict) -> dict:
    try:
        async with http.post(f"{TGRASS_URL}/{method}", json=payload,
                             headers={"Auth": TGRASS_KEY, "accept": "application/json"},
                             timeout=aiohttp.ClientTimeout(total=10), ssl=TGRASS_VERIFY_SSL) as r:
            data = await r.json(content_type=None)
            if r.status != 200:
                return {"error": f"HTTP {r.status}: {data}"}
            return data if isinstance(data, dict) else {"error": f"bad response: {data!r}"}
    except Exception as ex:
        logging.warning("tgrass %s failed: %s", method, ex)
        return {"error": str(ex)}


async def tgrass_offers(tgu) -> dict:
    payload = {"tg_user_id": tgu.id, "lang": tgu.language_code or "ru", "is_premium": bool(tgu.is_premium)}
    if tgu.username:
        payload["tg_login"] = tgu.username
    return await tgrass_call(TGRASS_ENDPOINT, payload)


async def next_tgrass_task(tgu):
    """Возвращает строку tgrass_tasks с очередным оффером или None."""
    if not TGRASS_KEY or tgu is None:
        return None
    for attempt in range(2):
        data = await tgrass_offers(tgu)
        if data.get("error"):
            logging.info("tgrass: %s", data["error"])
            return None
        for o in data.get("offers") or []:
            if o.get("subscribed") or not o.get("link") or o.get("offer_id") is None:
                continue
            row = await fetchone("SELECT * FROM tgrass_tasks WHERE user_id=? AND offer_id=?", (tgu.id, o["offer_id"]))
            if row and row["status"] in ("done", "skip"):
                continue
            if not row:
                await run("INSERT OR IGNORE INTO tgrass_tasks(user_id,offer_id,link,kind,status) VALUES(?,?,?,?,'shown')",
                          (tgu.id, o["offer_id"], o["link"], o.get("type", "")))
                row = await fetchone("SELECT * FROM tgrass_tasks WHERE user_id=? AND offer_id=?",
                                     (tgu.id, o["offer_id"]))
            return row
        if data.get("status") == "ok" and attempt == 0:
            # пользователь выполнил все текущие офферы — просим TGrass выдать новые сразу
            await tgrass_call("reset_offers", {"tg_user_id": tgu.id})
            continue
        break
    logging.info("tgrass: для %s нет подходящих заданий (status=%s)", tgu.id, data.get("status"))
    return None


def tgrass_task_text(gt) -> str:
    verb, noun = TGRASS_VERBS.get(gt["kind"], ("Перейди по", "ссылке"))
    link = html.escape(gt["link"], quote=True)
    return (
        f"{BULB} Получай Звёзды за простые задания! 👇\n\n"
        f"{GREEN} {verb} <a href=\"{link}\">{noun}</a> и нажми «Подтвердить»\n\n"
        f"Вознаграждение: +{fmt(TGRASS_REWARD_CACHE['v'])}{STAR}"
    )


def tgrass_task_kb(gt) -> InlineKeyboardMarkup:
    return kb(
        [btn("🔎 Перейти", url=gt["link"], style="primary"),
         btn("✅ Подтвердить", cb=f"gc:{gt['id']}", style="success")],
        [btn("⏭️ Пропустить", cb=f"gs:{gt['id']}", style="danger")],
    )


# ============== TRAFSLY / PIARFLOW / TRAFFY (общая таблица ext_tasks) ==============
EXT_NAMES = {"trafsly": "Trafsly", "piarflow": "PiarFlow", "traffy": "Traffy"}


async def ext_post(url: str, headers: dict, payload: dict):
    """Возвращает (http_status, json|{}). При сетевой ошибке (0, {})."""
    try:
        async with http.post(url, json=payload, headers=headers,
                             timeout=aiohttp.ClientTimeout(total=10)) as r:
            data = await r.json(content_type=None)
            return r.status, (data if isinstance(data, dict) else {})
    except Exception as ex:
        logging.warning("ext_post %s failed: %s", url, ex)
        return 0, {}


def guess_kind(link: str) -> str:
    l = (link or "").lower()
    if "?start=" in l or re.search(r"t\.me/[a-z0-9_]*bot(\?|/|$)", l):
        return "bot"
    return "channel"


async def ext_seen_links(uid: int, provider: str):
    rows = await fetchall("SELECT link FROM ext_tasks WHERE user_id=? AND provider=? AND status IN ('done','skip') "
                          "ORDER BY id DESC LIMIT 50", (uid, provider))
    return [r["link"] for r in rows if r["link"]]


async def ext_get_or_create(uid: int, provider: str, ext_id: str, link: str, kind: str):
    row = await fetchone("SELECT * FROM ext_tasks WHERE user_id=? AND provider=? AND ext_id=?",
                         (uid, provider, str(ext_id)))
    if row and row["status"] in ("done", "skip"):
        return None
    if not row:
        await run("INSERT OR IGNORE INTO ext_tasks(user_id,provider,ext_id,link,kind,status) VALUES(?,?,?,?,?,'shown')",
                  (uid, provider, str(ext_id), link, kind))
        row = await fetchone("SELECT * FROM ext_tasks WHERE user_id=? AND provider=? AND ext_id=?",
                             (uid, provider, str(ext_id)))
    return row


# ---------- Trafsly ----------
def _trafsly_headers():
    return {"Auth": TRAFSLY_KEY, "Content-Type": "application/json"}


async def next_trafsly(tgu):
    if not TRAFSLY_KEY or tgu is None:
        return None
    payload = {"user_id": tgu.id, "first_name": tgu.first_name or "", "language_code": tgu.language_code or "ru",
               "is_premium": bool(tgu.is_premium)}
    if tgu.username:
        payload["username"] = tgu.username
    seen = await ext_seen_links(tgu.id, "trafsly")
    if seen:
        payload["exclude_links"] = seen
    code, data = await ext_post(f"{TRAFSLY_URL}/get-sponsors", _trafsly_headers(), payload)
    if code != 200:
        logging.info("trafsly get-sponsors: %s %s", code, data)
        return None
    for sp in data.get("sponsors") or []:
        if sp.get("status") != "unsubscribed" or not sp.get("link") or sp.get("ads_id") is None:
            continue
        row = await ext_get_or_create(tgu.id, "trafsly", sp["ads_id"], sp["link"],
                                      sp.get("type") or guess_kind(sp["link"]))
        if row:
            return row
    return None


async def check_trafsly(uid: int, row):
    ads_id = row["ext_id"]
    ads_id = int(ads_id) if ads_id.isdigit() else ads_id
    code, data = await ext_post(f"{TRAFSLY_URL}/confirm-subscription", _trafsly_headers(),
                                {"user_id": uid, "ads_id": ads_id})
    if code != 200:
        return "wait", ""
    if data.get("subscribed"):
        return "ok", ""
    msg = str(data.get("message") or "")
    if msg == "Unsubscribed":
        return "unsub", ""
    if msg == "Subscription not verified":
        return "no", ""
    if msg in ("Sponsor was not shown", "Not subscribed, request get-sponsors again"):
        return "refresh", ""
    if msg == "Too many confirmation attempts":
        return "wait", ""
    return "gone", msg


# ---------- PiarFlow ----------
def _piar_headers():
    return {"Authorization": f"Bearer {PIARFLOW_KEY}", "Content-Type": "application/json"}


async def next_piarflow(tgu):
    if not PIARFLOW_KEY or tgu is None:
        return None
    payload = {"user_id": tgu.id, "chat_id": tgu.id, "max_sponsors": 5}
    if PIARFLOW_NO_TOKEN:
        payload.update({"first_name": tgu.first_name, "username": tgu.username,
                        "language_code": tgu.language_code, "bio": None})
    code, data = await ext_post(f"{PIARFLOW_URL}/sponsors", _piar_headers(), payload)
    if code != 200:
        logging.info("piarflow sponsors: %s %s", code, data)
        return None
    for sp in data.get("sponsors") or []:
        if sp.get("status") != "unsubscribed" or not sp.get("link"):
            continue
        row = await ext_get_or_create(tgu.id, "piarflow", sp["link"], sp["link"], guess_kind(sp["link"]))
        if row:
            return row
    return None


async def check_piarflow(uid: int, row):
    code, data = await ext_post(f"{PIARFLOW_URL}/sponsors/check", _piar_headers(),
                                {"user_id": uid, "links": [row["link"]]})
    if code == 403:
        return "no", ""
    if code == 404:
        return "gone", ""
    if code != 200:
        return "wait", ""
    for it in data.get("sponsors") or []:
        if it.get("link") == row["link"] or len(data.get("sponsors")) == 1:
            st = it.get("status")
            if st in ("subscribed", "not_counted"):
                return "ok", ""
            if st == "unsubscribed":
                return "unsub", ""
    return "wait", ""


# ---------- Traffy ----------
def _traffy_headers():
    return {"x-publisher-api-key": TRAFFY_KEY, "Content-Type": "application/json"}


async def next_traffy(tgu):
    if not TRAFFY_KEY or tgu is None:
        return None
    payload = {"telegram_id": tgu.id, "limit": 5, "first_name": tgu.first_name or "",
               "language_code": tgu.language_code or "ru"}
    if tgu.username:
        payload["username"] = tgu.username
    code, data = await ext_post(f"{TRAFFY_URL}/tasks", _traffy_headers(), payload)
    if code != 200 or not data.get("ok"):
        logging.info("traffy tasks: %s %s", code, data)
        return None
    for t in data.get("tasks") or []:
        if not t.get("assignment_id") or not t.get("target_link"):
            continue
        row = await ext_get_or_create(tgu.id, "traffy", t["assignment_id"], t["target_link"],
                                      guess_kind(t["target_link"]))
        if row:
            return row
    return None


async def check_traffy(uid: int, row):
    code, data = await ext_post(f"{TRAFFY_URL}/tasks/check", _traffy_headers(),
                                {"telegram_id": uid, "assignment_ids": [row["ext_id"]]})
    if code != 200 or not data.get("ok"):
        return "wait", ""
    res = (data.get("results") or [{}])[0]
    st, reason = res.get("status"), res.get("reason")
    if st == "completed":
        return "ok", ""
    if reason == "payout_cancelled":
        return "unsub", ""
    if st == "pending":
        return "no", ""
    if reason == "joined_via_other_link":
        return "gone", "Подписка не засчитана: вы вступили не по ссылке из задания."
    if st == "error":
        return "wait", ""
    return "gone", ""


# порядок важен: сначала PiarFlow, потом Trafsly, потом Traffy
EXT_NEXT = {"piarflow": next_piarflow, "trafsly": next_trafsly, "traffy": next_traffy}
EXT_CHECK = {"trafsly": check_trafsly, "piarflow": check_piarflow, "traffy": check_traffy}


async def next_ext_task(tgu):
    for prov, fn in EXT_NEXT.items():
        row = await fn(tgu)
        if row:
            return row
    return None


async def ext_task_text(et) -> str:
    reward = await get_setting(f"{et['provider']}_reward")
    link = html.escape(et["link"], quote=True)
    if et["kind"] == "bot":
        action = f'Запусти <a href="{link}">бота</a> и нажми «Подтвердить»'
    else:
        action = f'Подпишись на <a href="{link}">канал</a> и нажми «Подтвердить»'
    return (
        f"{BULB} Получай Звёзды за простые задания! 👇\n\n"
        f"{GREEN} {action}\n\n"
        f"Вознаграждение: +{fmt(reward)}{STAR}"
    )


def ext_task_kb(et) -> InlineKeyboardMarkup:
    return kb(
        [btn("🔎 Перейти", url=et["link"], style="primary"),
         btn("✅ Подтвердить", cb=f"ec:{et['id']}", style="success")],
        [btn("⏭️ Пропустить", cb=f"es:{et['id']}", style="danger")],
    )


# ============================== FSM ==============================
class PromoSt(StatesGroup):
    code = State()


class WdSt(StatesGroup):
    username = State()
    confirm = State()


class TaskAdd(StatesGroup):
    url = State()
    chat = State()
    reward = State()


class PromoAdd(StatesGroup):
    code = State()
    reward = State()
    uses = State()


class Cast(StatesGroup):
    msg = State()


class UserAdm(StatesGroup):
    find = State()
    amount = State()


class SetSt(StatesGroup):
    value = State()


class BanMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        u = data.get("event_from_user")
        if u and u.id not in ADMIN_IDS:
            row = await fetchone("SELECT banned FROM users WHERE id=?", (u.id,))
            if row and row["banned"]:
                return None
        return await handler(event, data)


# ============================== ПОЛЬЗОВАТЕЛЬСКИЕ ХЕНДЛЕРЫ ==============================
user = Router()
USERNAME_RE = re.compile(r"^@?([A-Za-z][A-Za-z0-9_]{4,31})$")


@user.message(CommandStart())
async def cmd_start(m: Message, command: CommandObject, state: FSMContext):
    await state.clear()
    u, is_new = await get_or_create_user(m.from_user, command.args)
    if not is_new and command.args and u["fresh"] and not u["referrer_id"]:
        # аккаунт был сброшен командой /e — переход по ссылке снова делает его рефералом
        r = await fetchone("SELECT id FROM users WHERE ref_code=?", (command.args,))
        if r and r["id"] != u["id"]:
            await run("UPDATE users SET referrer_id=?, fresh=0 WHERE id=?", (r["id"], u["id"]))
            u, is_new = await get_user(u["id"]), True
    if is_new and u["referrer_id"]:
        need = int(await get_setting("ref_tasks"))
        rr = fmt(await get_setting("ref_reward"))
        try:
            await m.bot.send_message(
                u["referrer_id"],
                f"{BOLT} Пользователь {uref(u)} присоединился по твоей ссылке!\n\n"
                f"Ты получишь {rr} {STAR}, как только он выполнит {need} заданий")
        except Exception:
            pass
    await set_menu(m)
    await m.answer(await ref_text(u), reply_markup=ref_kb(u))
    await send_task(m, u["id"], lang=m.from_user.language_code, tgu=m.from_user)


@user.message(Command("e"))
async def cmd_reset(m: Message, state: FSMContext):
    if m.from_user.id not in RESET_IDS:
        return
    await state.clear()
    u, _ = await get_or_create_user(m.from_user)
    uid = u["id"]
    await db.execute("UPDATE users SET balance=0, earned=0, withdrawn=0, referrer_id=NULL, ref_paid=0, "
                     "ref_penalized=0, fresh=1 WHERE id=?", (uid,))
    for table in ("user_tasks", "flyer_tasks", "tgrass_tasks", "ext_tasks", "promo_uses"):
        await db.execute(f"DELETE FROM {table} WHERE user_id=?", (uid,))
    await db.commit()
    await m.answer("♻️ Аккаунт сброшен: заработок, задания и привязка к рефереру обнулены.\n"
                   "Теперь перейди по реферальной ссылке — и ты снова будешь считаться рефералом.")


@user.chat_member()
async def on_chat_member(ev: ChatMemberUpdated):
    """Мгновенный штраф, если реферал вышел из канала, где бот — админ (задания, добавленные админом)."""
    if ev.new_chat_member.status not in ("left", "kicked"):
        return
    uid = ev.new_chat_member.user.id
    ids = [str(ev.chat.id)]
    if ev.chat.username:
        ids += ["@" + ev.chat.username, ev.chat.username]
    days = int(await get_setting("unsub_days"))
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    q = ",".join("?" * len(ids))
    rows = await fetchall(
        "SELECT ut.task_id, t.url, t.chat_id FROM user_tasks ut JOIN tasks t ON t.id=ut.task_id "
        "JOIN users u ON u.id=ut.user_id WHERE ut.user_id=? AND ut.status='done' AND ut.penalized=0 "
        f"AND t.kind='channel' AND u.referrer_id IS NOT NULL AND ut.day>=? AND t.chat_id IN ({q})",
        (uid, cutoff, *ids))
    for r in rows:
        await apply_unsub_penalty(ev.bot, uid, r["url"] or str(r["chat_id"]),
                                  "user_tasks", "user_id=? AND task_id=?", (uid, r["task_id"]))


@user.message(Command("unsub"))
async def cmd_unsub(m: Message):
    if m.from_user.id not in ADMIN_IDS and m.from_user.id not in RESET_IDS:
        return
    await m.answer("⏳ Проверяю отписки рефералов…")
    rep_ = await check_unsubs(m.bot)
    days = int(await get_setting("unsub_days"))
    await m.answer("🔎 <b>Проверка отписок</b> (задания за последние "
                   f"{days} дн., только рефералы)\n\n" + "\n".join(esc(x) for x in rep_))


@user.message(F.text == B_EARN)
async def earn(m: Message, state: FSMContext):
    await state.clear()
    u, _ = await get_or_create_user(m.from_user)
    await m.answer(await ref_text(u), reply_markup=ref_kb(u))
    await send_task(m, u["id"], lang=m.from_user.language_code, tgu=m.from_user)


@user.message(F.text == B_TASK)
async def task_btn(m: Message, state: FSMContext):
    await state.clear()
    await get_or_create_user(m.from_user)
    await send_task(m, m.from_user.id, lang=m.from_user.language_code, tgu=m.from_user)


async def show_done(c: CallbackQuery, reward, is_channel: bool):
    """Заменяет сообщение с заданием на «Задание выполнено!» и присылает следующее задание новым сообщением."""
    text = f"{CHECK_E} Задание выполнено!\nПолучено: +{fmt(reward)} {STAR}"
    if is_channel:
        days = int(await get_setting("unsub_days"))
        text += (f"\n\n{WARN_E} Не отписывайся от канала в течение как минимум {days} дней. "
                 f"В противном случае, ты получишь штраф или блокировку аккаунта.")
    try:
        await c.message.edit_text(text, reply_markup=None)
    except TelegramBadRequest:
        await c.message.answer(text)
    await send_task(c.message, c.from_user.id, lang=c.from_user.language_code, tgu=c.from_user)


@user.callback_query(F.data.startswith("tc:"))
async def task_confirm(c: CallbackQuery):
    t = await fetchone("SELECT * FROM tasks WHERE id=?", (int(c.data[3:]),))
    if not t or not t["active"]:
        await c.answer("Это задание уже недоступно")
        return await send_task(c.message, c.from_user.id, edit=True, lang=c.from_user.language_code, tgu=c.from_user)
    if await fetchone("SELECT 1 FROM user_tasks WHERE user_id=? AND task_id=?", (c.from_user.id, t["id"])):
        await c.answer()
        return await send_task(c.message, c.from_user.id, edit=True, lang=c.from_user.language_code, tgu=c.from_user)
    if not await check_task(c.bot, c.from_user.id, t):
        txt = "❌ Ты не запустил бота, которого требует задание" if t["kind"] == "bot" \
            else "❌ Ты не подписался на канал, которого требует задание"
        return await c.answer(txt, show_alert=True)
    await complete_task(c.bot, c.from_user.id, t)
    await c.answer()
    await show_done(c, t["reward"], t["kind"] == "channel")


@user.callback_query(F.data.startswith("ts:"))
async def task_skip(c: CallbackQuery):
    await run("INSERT OR IGNORE INTO user_tasks(user_id,task_id,status,day) VALUES(?,?,?,?)",
              (c.from_user.id, int(c.data[3:]), "skip", today()))
    await c.answer("Задание пропущено")
    await send_task(c.message, c.from_user.id, edit=True, lang=c.from_user.language_code, tgu=c.from_user)


# ---------- Задания Flyer ----------
@user.callback_query(F.data.startswith("fc:"))
async def flyer_confirm(c: CallbackQuery):
    lang = c.from_user.language_code
    row = await fetchone("SELECT * FROM flyer_tasks WHERE id=? AND user_id=?", (int(c.data[3:]), c.from_user.id))
    if not row or row["status"] != "shown":
        await c.answer()
        return await send_task(c.message, c.from_user.id, edit=True, lang=lang, tgu=c.from_user)
    res = await flyer_call("check_task", {"signature": row["signature"]})
    status = res.get("result")
    if status in ("complete", "waiting"):
        cur = await db.execute("UPDATE flyer_tasks SET status='done', day=? WHERE id=? AND status='shown'",
                               (today(), row["id"]))
        await db.commit()
        if cur.rowcount:
            reward = await get_setting("flyer_reward")
            await after_task_done(c.bot, c.from_user.id, reward)
            await c.answer()
            return await show_done(c, reward, row["task"] == "subscribe channel")
        await c.answer()
        return await send_task(c.message, c.from_user.id, edit=True, lang=lang, tgu=c.from_user)
    if status == "unavailable":
        await run("UPDATE flyer_tasks SET status='skip', day=? WHERE id=?", (today(), row["id"]))
        await c.answer("Это задание больше не актуально")
        return await send_task(c.message, c.from_user.id, edit=True, lang=lang, tgu=c.from_user)
    if status in ("incomplete", "abort"):
        txt = {"start bot": "❌ Ты не запустил бота, которого требует задание",
               "subscribe channel": "❌ Ты не подписался на канал, которого требует задание"}.get(
            row["task"], "❌ Задание ещё не выполнено")
        return await c.answer(txt, show_alert=True)
    logging.warning("flyer check_task: %s", res)
    await c.answer("⚠️ Не удалось проверить задание, попробуй через минуту", show_alert=True)


@user.callback_query(F.data.startswith("fs:"))
async def flyer_skip(c: CallbackQuery):
    await run("UPDATE flyer_tasks SET status='skip', day=? WHERE id=? AND user_id=? AND status='shown'",
              (today(), int(c.data[3:]), c.from_user.id))
    await c.answer("Задание пропущено")
    await send_task(c.message, c.from_user.id, edit=True, lang=c.from_user.language_code, tgu=c.from_user)


# ---------- Задания TGrass ----------
@user.callback_query(F.data.startswith("gc:"))
async def tgrass_confirm(c: CallbackQuery):
    lang = c.from_user.language_code
    row = await fetchone("SELECT * FROM tgrass_tasks WHERE id=? AND user_id=?", (int(c.data[3:]), c.from_user.id))
    if not row or row["status"] != "shown":
        await c.answer()
        return await send_task(c.message, c.from_user.id, edit=True, lang=lang, tgu=c.from_user)
    res = await tgrass_call("check", {"tg_user_id": c.from_user.id, "offer_id": row["offer_id"]})
    status = res.get("status")
    if status == "subscribed":
        if res.get("is_fake"):
            await run("UPDATE tgrass_tasks SET status='skip', day=? WHERE id=?", (today(), row["id"]))
            return await c.answer("❌ Задание не засчитано", show_alert=True)
        cur = await db.execute("UPDATE tgrass_tasks SET status='done', day=? WHERE id=? AND status='shown'",
                               (today(), row["id"]))
        await db.commit()
        if cur.rowcount:
            reward = await get_setting("tgrass_reward")
            await after_task_done(c.bot, c.from_user.id, reward)
            await c.answer()
            return await show_done(c, reward, row["kind"] == "channel")
        await c.answer()
        return await send_task(c.message, c.from_user.id, edit=True, lang=lang, tgu=c.from_user)
    if status in ("offer_expired", "offer_not_found"):
        await run("UPDATE tgrass_tasks SET status='skip', day=? WHERE id=?", (today(), row["id"]))
        await c.answer("Это задание больше не актуально")
        return await send_task(c.message, c.from_user.id, edit=True, lang=lang, tgu=c.from_user)
    if status == "not_subscribed":
        txt = {"channel": "❌ Ты не подписался на канал, которого требует задание",
               "bot": "❌ Ты не запустил бота, которого требует задание"}.get(
            row["kind"], "❌ Задание ещё не выполнено")
        return await c.answer(txt, show_alert=True)
    logging.warning("tgrass check: %s", res)
    await c.answer("⚠️ Не удалось проверить задание, попробуй через минуту", show_alert=True)


@user.callback_query(F.data.startswith("gs:"))
async def tgrass_skip(c: CallbackQuery):
    await run("UPDATE tgrass_tasks SET status='skip', day=? WHERE id=? AND user_id=? AND status='shown'",
              (today(), int(c.data[3:]), c.from_user.id))
    await c.answer("Задание пропущено")
    await send_task(c.message, c.from_user.id, edit=True, lang=c.from_user.language_code, tgu=c.from_user)


# ---------- Задания Trafsly / PiarFlow / Traffy ----------
@user.callback_query(F.data.startswith("ec:"))
async def ext_confirm(c: CallbackQuery):
    lang = c.from_user.language_code
    row = await fetchone("SELECT * FROM ext_tasks WHERE id=? AND user_id=?", (int(c.data[3:]), c.from_user.id))
    if not row or row["status"] != "shown":
        await c.answer()
        return await send_task(c.message, c.from_user.id, edit=True, lang=lang, tgu=c.from_user)
    state, msg = await EXT_CHECK[row["provider"]](c.from_user.id, row)
    if state == "ok":
        cur = await db.execute("UPDATE ext_tasks SET status='done', day=? WHERE id=? AND status='shown'",
                               (today(), row["id"]))
        await db.commit()
        if cur.rowcount:
            reward = await get_setting(f"{row['provider']}_reward")
            await after_task_done(c.bot, c.from_user.id, reward)
            await c.answer()
            return await show_done(c, reward, row["kind"] == "channel")
        await c.answer()
        return await send_task(c.message, c.from_user.id, edit=True, lang=lang, tgu=c.from_user)
    if state in ("no", "unsub"):
        txt = "❌ Ты не запустил бота, которого требует задание" if row["kind"] == "bot" \
            else "❌ Ты не подписался на канал, которого требует задание"
        return await c.answer(txt, show_alert=True)
    if state == "refresh":
        await c.answer("Обновляю задание…")
        return await send_task(c.message, c.from_user.id, edit=True, lang=lang, tgu=c.from_user)
    if state == "gone":
        await run("UPDATE ext_tasks SET status='skip', day=? WHERE id=?", (today(), row["id"]))
        await c.answer(msg or "Это задание больше не актуально", show_alert=bool(msg))
        return await send_task(c.message, c.from_user.id, edit=True, lang=lang, tgu=c.from_user)
    await c.answer("⚠️ Не удалось проверить задание, попробуй через минуту", show_alert=True)


@user.callback_query(F.data.startswith("es:"))
async def ext_skip(c: CallbackQuery):
    await run("UPDATE ext_tasks SET status='skip', day=? WHERE id=? AND user_id=? AND status='shown'",
              (today(), int(c.data[3:]), c.from_user.id))
    await c.answer("Задание пропущено")
    await send_task(c.message, c.from_user.id, edit=True, lang=c.from_user.language_code, tgu=c.from_user)


# ---------- Профиль / Топ ----------
@user.message(F.text == B_PROFILE)
async def profile(m: Message, state: FSMContext):
    await state.clear()
    await get_or_create_user(m.from_user)
    await m.answer(await profile_text(m.from_user.id))


@user.message(F.text == B_TOP)
async def top(m: Message, state: FSMContext):
    await state.clear()
    await get_or_create_user(m.from_user)
    rows = await fetchall(
        "SELECT r.referrer_id AS id, COUNT(*) AS cnt, u.full_name AS name FROM users r "
        "LEFT JOIN users u ON u.id=r.referrer_id WHERE r.referrer_id IS NOT NULL "
        "GROUP BY r.referrer_id ORDER BY cnt DESC, r.referrer_id ASC LIMIT 5")
    lines = [f"{TROPHY} <b>Топ 5 по приглашениям:</b>\n"]
    for i, r in enumerate(rows):
        lines.append(f"{MEDALS[i]} {esc(r['name'])} — рефералов: {r['cnt']}")
    mine = await ref_count(m.from_user.id)
    place = (await fetchone(
        "SELECT COUNT(*) c FROM (SELECT referrer_id FROM users WHERE referrer_id IS NOT NULL "
        "GROUP BY referrer_id HAVING COUNT(*) > ?)", (mine,)))["c"] + 1
    lines.append(f"\n{TROPHY} Ты на {place} месте! Рефералов: {mine}")
    await m.answer("\n".join(lines))


# ---------- Промокод ----------
PROMO_PROMPT = "🎟 Введите промокод:"
PROMO_BACK = kb([btn("◀️ Назад", cb="promo_back", style="primary")])


@user.message(F.text == B_PROMO)
async def promo_start(m: Message, state: FSMContext):
    await get_or_create_user(m.from_user)
    await state.set_state(PromoSt.code)
    await m.answer(PROMO_PROMPT)


@user.callback_query(F.data == "promo_back")
async def promo_back(c: CallbackQuery, state: FSMContext):
    await state.set_state(PromoSt.code)
    await c.message.edit_text(PROMO_PROMPT)
    await c.answer()


@user.message(PromoSt.code, F.text)
async def promo_enter(m: Message, state: FSMContext):
    code = m.text.strip().upper()
    p = await fetchone("SELECT * FROM promos WHERE code=?", (code,))
    if not p:
        return await m.answer("❌ Промокод использован или недействителен", reply_markup=PROMO_BACK)
    if await fetchone("SELECT 1 FROM promo_uses WHERE user_id=? AND code=?", (m.from_user.id, code)):
        return await m.answer("Промокод уже был вами использован ❗", reply_markup=PROMO_BACK)
    cur = await db.execute(
        "UPDATE promos SET uses=uses+1 WHERE code=? AND (max_uses=0 OR uses<max_uses)", (code,))
    if cur.rowcount == 0:
        await db.commit()
        return await m.answer("❌ Промокод использован или недействителен", reply_markup=PROMO_BACK)
    await db.execute("INSERT INTO promo_uses(user_id,code) VALUES(?,?)", (m.from_user.id, code))
    await db.execute("UPDATE users SET balance=balance+?, earned=earned+? WHERE id=?",
                     (p["reward"], p["reward"], m.from_user.id))
    await db.commit()
    await state.clear()
    await m.answer(f"✅ Промокод успешно активирован: ваш баланс пополнен на {fmt(p['reward'])} {STAR}")


# ---------- Вывод ----------
USERNAME_PROMPT = "Введите @username получателя:"
USERNAME_BACK = kb([btn("◀️ Назад", cb="wd_menu", style="primary")])


@user.message(F.text == B_WD)
async def wd_open(m: Message, state: FSMContext):
    await state.clear()
    await get_or_create_user(m.from_user)
    await m.answer(await wd_menu_text(m.from_user.id), reply_markup=wd_menu_kb())


@user.callback_query(F.data == "wd_menu")
async def wd_menu(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await c.message.edit_text(await wd_menu_text(c.from_user.id), reply_markup=wd_menu_kb())
    await c.answer()


@user.callback_query(F.data.startswith("wd:"))
async def wd_pick(c: CallbackQuery, state: FSMContext):
    idx = int(c.data[3:])
    amt = GIFTS[idx][0]
    u = await get_user(c.from_user.id)
    if u["balance"] + 1e-9 < amt:
        return await c.answer(f"❌ Недостаточно звёзд. Нужно {amt}, у тебя {fmt(u['balance'])}", show_alert=True)
    await state.set_state(WdSt.username)
    await state.update_data(gift=idx)
    await c.message.edit_text(USERNAME_PROMPT, reply_markup=USERNAME_BACK)
    await c.answer()


@user.callback_query(F.data == "wd_name")
async def wd_name(c: CallbackQuery, state: FSMContext):
    await state.set_state(WdSt.username)
    await c.message.edit_text(USERNAME_PROMPT, reply_markup=USERNAME_BACK)
    await c.answer()


@user.message(WdSt.username, F.text)
async def wd_username(m: Message, state: FSMContext):
    mt = USERNAME_RE.match(m.text.strip())
    if not mt:
        return await m.answer("❌ Неверный формат username.\n\n" + USERNAME_PROMPT, reply_markup=USERNAME_BACK)
    uname = "@" + mt.group(1)
    data = await state.get_data()
    await state.update_data(recipient=uname)
    await state.set_state(WdSt.confirm)
    await m.answer(
        f"Получатель: {uname}\n"
        f"Количество: {gift_label(data['gift'])}\n\n"
        f"Если данные верны нажми да 👇",
        reply_markup=kb([btn("✅ Да, верны", cb="wd_yes", style="success"),
                         btn("◀️ Назад", cb="wd_name", style="primary")]))


def wd_admin_kb(wid: int) -> InlineKeyboardMarkup:
    return kb([btn("Выплатил ✅", cb=f"wp:{wid}", style="success"),
               btn("Не одобрена ❌", cb=f"wr:{wid}", style="danger")])


async def wd_admin_text(w, status: str = "") -> str:
    u = await get_user(w["user_id"])
    invited = await ref_count(w["user_id"])
    done = await ref_tasks_done(w["user_id"])
    uname = f"@{u['username']}" if u["username"] else "—"
    text = (f"📥 <b>Заявка на вывод #{w['id']}</b>\n"
            f"Пользователь: {esc(u['full_name'])} ({esc(uname)})\n"
            f"Получатель: {esc(w['recipient'])}\n"
            f"Количество: {esc(w['gift'])}\n"
            f"Приглашено: {invited} · выполнено заданий: {done}")
    if status:
        text += f"\n\n<b>Статус:</b> {status}"
    return text


@user.callback_query(F.data == "wd_yes", WdSt.confirm)
async def wd_yes(c: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    idx, recipient = data.get("gift"), data.get("recipient")
    if idx is None or not recipient:
        await state.clear()
        return await c.answer("Сессия устарела, начни заново", show_alert=True)
    amt = GIFTS[idx][0]
    cur = await db.execute("UPDATE users SET balance=balance-? WHERE id=? AND balance>=?",
                           (amt, c.from_user.id, amt))
    if cur.rowcount == 0:
        await db.commit()
        await state.clear()
        return await c.answer("❌ Недостаточно звёзд", show_alert=True)
    cur = await db.execute(
        "INSERT INTO withdrawals(user_id,recipient,amount,gift,created_at) VALUES(?,?,?,?,?)",
        (c.from_user.id, recipient, amt, gift_label(idx), now()))
    await db.commit()
    wid = cur.lastrowid
    await state.clear()
    await c.message.edit_text(
        f"<b>Заявка на вывод #{wid}</b>\n{gift_label(idx)}\nуспешно создана",
        reply_markup=kb([btn("◀️ Назад", cb="wd_menu", style="primary")]))
    await c.answer()
    w = await fetchone("SELECT * FROM withdrawals WHERE id=?", (wid,))
    text = await wd_admin_text(w)
    for aid in ADMIN_IDS:
        try:
            await c.bot.send_message(aid, text, reply_markup=wd_admin_kb(wid))
        except Exception as ex:
            logging.warning("admin notify failed: %s", ex)


# ============================== АДМИН-ПАНЕЛЬ ==============================
admin = Router()
admin.message.filter(F.from_user.id.in_(ADMIN_IDS))
admin.callback_query.filter(F.from_user.id.in_(ADMIN_IDS))

A_BACK = [btn("◀️ Назад", cb="a:menu", style="primary")]


async def edit(c: CallbackQuery, text: str, markup=None):
    try:
        await c.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as ex:
        if "not modified" not in str(ex):
            await c.message.answer(text, reply_markup=markup)


def admin_menu():
    return ("⚙️ <b>Админ-панель</b>\n\nВыбери раздел 👇",
            kb([btn("📊 Статистика", cb="a:stats", style="primary"),
                btn("👥 Пользователи", cb="a:users", style="primary")],
               [btn("📋 Задания", cb="a:tasks", style="success"),
                btn("🎟 Промокоды", cb="a:promos", style="success")],
               [btn("📥 Заявки на вывод", cb="a:wd", style="primary"),
                btn("📨 Рассылка", cb="a:cast", style="danger")],
               [btn("⚙️ Настройки", cb="a:set")],
               [btn("✖️ Закрыть", cb="a:close")]))


@admin.message(Command("admin"))
@admin.message(F.text == B_ADMIN)
async def admin_open(m: Message, state: FSMContext):
    await state.clear()
    text, markup = admin_menu()
    await m.answer(text, reply_markup=markup)


@admin.callback_query(F.data == "a:menu")
async def a_menu(c: CallbackQuery, state: FSMContext):
    await state.clear()
    text, markup = admin_menu()
    await edit(c, text, markup)
    await c.answer()


@admin.callback_query(F.data == "a:close")
async def a_close(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await c.message.delete()
    await c.answer()


# ---------- Диагностика Flyer ----------
@admin.message(Command("flyer"))
async def a_flyer(m: Message):
    if not FLYER_KEY:
        return await m.answer("❌ <b>FLYER_KEY не задан.</b>\nУстанови переменную в том же терминале, где запускаешь бота:\n"
                              "<code>$env:FLYER_KEY=\"FL-...\"</code>")
    me = await flyer_call("get_me", {})
    tasks = await flyer_call("get_tasks", {"user_id": m.from_user.id,
                                           "language_code": m.from_user.language_code or "ru",
                                           "limit": FLYER_LIMIT})

    def dump(x):
        return html.escape(json.dumps(x, ensure_ascii=False, indent=1)[:1500])

    await m.answer(f"🛰 <b>Flyer get_me</b>\n<pre>{dump(me)}</pre>\n\n"
                   f"🛰 <b>Flyer get_tasks (для тебя)</b>\n<pre>{dump(tasks)}</pre>")


@admin.message(Command("tgrass"))
async def a_tgrass(m: Message):
    if not TGRASS_KEY:
        return await m.answer("❌ <b>TGRASS_KEY не задан.</b>\nУстанови переменную в том же терминале, где запускаешь бота:\n"
                              "<code>$env:TGRASS_KEY=\"ключ\"</code>")
    data = await tgrass_offers(m.from_user)
    text = html.escape(json.dumps(data, ensure_ascii=False, indent=1)[:2500])
    await m.answer(f"🌿 <b>TGrass /{TGRASS_ENDPOINT} (для тебя)</b>\n<pre>{text}</pre>")


# ---------- Статистика ----------
@admin.callback_query(F.data == "a:stats")
async def a_stats(c: CallbackQuery, state: FSMContext):
    await state.clear()

    async def one(sql, args=()):
        return (await fetchone(sql, args))[0] or 0

    d = today()
    total = await one("SELECT COUNT(*) FROM users")
    new_today = await one("SELECT COUNT(*) FROM users WHERE created_at LIKE ?", (d + "%",))
    refs = await one("SELECT COUNT(*) FROM users WHERE referrer_id IS NOT NULL")
    refs_paid = await one("SELECT COUNT(*) FROM users WHERE ref_paid=1")
    done = await one("SELECT COUNT(*) FROM done_tasks")
    done_today = await one("SELECT COUNT(*) FROM done_tasks WHERE day=?", (d,))
    fl = await one("SELECT COUNT(*) FROM flyer_tasks WHERE status='done'")
    tg = await one("SELECT COUNT(*) FROM tgrass_tasks WHERE status='done'")
    bal = await one("SELECT SUM(balance) FROM users")
    wd_p = await one("SELECT COUNT(*) FROM withdrawals WHERE status='pending'")
    wd_ok = await one("SELECT COUNT(*) FROM withdrawals WHERE status='paid'")
    wd_no = await one("SELECT COUNT(*) FROM withdrawals WHERE status='rejected'")
    wd_sum = await one("SELECT SUM(amount) FROM withdrawals WHERE status='paid'")
    tasks = await one("SELECT COUNT(*) FROM tasks WHERE active=1")
    promos = await one("SELECT COUNT(*) FROM promos")
    text = (
        "📊 <b>Статистика</b>\n\n"
        f"👥 Пользователей: <b>{total}</b> (сегодня +{new_today})\n"
        f"🔗 Пришло по рефералкам: <b>{refs}</b> (награда выдана: {refs_paid})\n\n"
        f"📋 Активных заданий: <b>{tasks}</b>\n"
        f"✅ Выполнено заданий: <b>{done}</b> (сегодня {done_today}), из них Flyer: {fl}, TGrass: {tg}\n"
        f"🎟 Промокодов: <b>{promos}</b>\n\n"
        f"💰 Сумма балансов: <b>{fmt(bal)}</b> ⭐\n"
        f"📥 Заявки: ожидают <b>{wd_p}</b> · выплачено <b>{wd_ok}</b> · отклонено <b>{wd_no}</b>\n"
        f"🎁 Выплачено всего: <b>{fmt(wd_sum)}</b> ⭐"
    )
    await edit(c, text, kb([btn("🔄 Обновить", cb="a:stats", style="success")], A_BACK))
    await c.answer()


# ---------- Заявки на вывод ----------
@admin.callback_query(F.data == "a:wd")
async def a_wd(c: CallbackQuery, state: FSMContext):
    await state.clear()
    rows = await fetchall("SELECT * FROM withdrawals WHERE status='pending' ORDER BY id DESC LIMIT 20")
    btns = [[btn(f"#{w['id']} · {w['recipient']} · {w['gift']}", cb=f"a:wdv:{w['id']}")] for w in rows]
    text = f"📥 <b>Заявки на вывод</b>\n\nОжидают: {len(rows)}" if rows else "📥 <b>Заявки на вывод</b>\n\nНет заявок в ожидании 🎉"
    await edit(c, text, kb(*btns, A_BACK))
    await c.answer()


@admin.callback_query(F.data.startswith("a:wdv:"))
async def a_wd_view(c: CallbackQuery):
    w = await fetchone("SELECT * FROM withdrawals WHERE id=?", (int(c.data.split(":")[2]),))
    if not w:
        return await c.answer("Не найдена")
    markup = wd_admin_kb(w["id"]).inline_keyboard
    await edit(c, await wd_admin_text(w), kb(*markup, [btn("◀️ К заявкам", cb="a:wd", style="primary")]))
    await c.answer()


@admin.callback_query(F.data.startswith("wp:"))
async def wd_paid(c: CallbackQuery):
    w = await fetchone("SELECT * FROM withdrawals WHERE id=?", (int(c.data[3:]),))
    if not w or w["status"] != "pending":
        return await c.answer("Заявка уже обработана", show_alert=True)
    await db.execute("UPDATE withdrawals SET status='paid' WHERE id=?", (w["id"],))
    await db.execute("UPDATE users SET withdrawn=withdrawn+? WHERE id=?", (w["amount"], w["user_id"]))
    await db.commit()
    await edit(c, await wd_admin_text(w, "✅ Выплата отмечена как выполненная."))
    await c.answer("Готово ✅")
    try:
        await c.bot.send_message(
            w["user_id"],
            f"📥 <b>Заявка на вывод #{w['id']}</b>\nПолучатель: {esc(w['recipient'])}\n"
            f"Количество: {esc(w['gift'])}\n\nСтатус: ✅ Выплата одобрена")
    except Exception:
        pass
    if POST_PAYOUTS_TO_CHANNEL:
        try:
            await c.bot.send_message(
                WITHDRAW_CHANNEL,
                f"✅ <b>Выплата #{w['id']}</b>\n{esc(w['gift'])}\nПолучатель: {esc(w['recipient'])}")
        except Exception as ex:
            logging.warning("channel post failed: %s", ex)


@admin.callback_query(F.data.startswith("wr:"))
async def wd_reject(c: CallbackQuery):
    w = await fetchone("SELECT * FROM withdrawals WHERE id=?", (int(c.data[3:]),))
    if not w or w["status"] != "pending":
        return await c.answer("Заявка уже обработана", show_alert=True)
    await db.execute("UPDATE withdrawals SET status='rejected' WHERE id=?", (w["id"],))
    await db.execute("UPDATE users SET balance=balance+? WHERE id=?", (w["amount"], w["user_id"]))  # возврат
    await db.commit()
    await edit(c, await wd_admin_text(w, "❌ Заявка отклонена."))
    await c.answer("Отклонено ❌")
    try:
        await c.bot.send_message(
            w["user_id"],
            f"📥 <b>Заявка на вывод #{w['id']}</b>\nПолучатель: {esc(w['recipient'])}\n"
            f"Количество: {esc(w['gift'])}\n\nСтатус: ❌ Заявка отклонена.\n"
            f"Звёзды возвращены на баланс.")
    except Exception:
        pass


# ---------- Задания ----------
async def tasks_view():
    rows = await fetchall("SELECT * FROM tasks ORDER BY id")
    btns = []
    for t in rows:
        done = (await fetchone("SELECT COUNT(*) c FROM user_tasks WHERE task_id=? AND status='done'", (t["id"],)))["c"]
        icon = "🟢" if t["active"] else "⚪"
        kind = "📢" if t["kind"] == "channel" else "🤖"
        btns.append([btn(f"{icon} #{t['id']} {kind} +{fmt(t['reward'])}⭐ · выполнили: {done}", cb=f"a:tv:{t['id']}")])
    text = "📋 <b>Задания</b>\n\n" + (f"Всего своих: {len(rows)}. Нажми на задание, чтобы управлять 👇" if rows else "Своих заданий пока нет.") + \
        f"\n\n🛰 Flyer: {'подключён ✅ (задания приходят автоматически)' if FLYER_KEY else 'не подключён (нет FLYER_KEY)'}" + \
        f"\n🌿 TGrass: {'подключён ✅' if TGRASS_KEY else 'не подключён (нет TGRASS_KEY)'}"
    btns.append([btn("➕ Добавить задание", cb="a:task_add", style="success")])
    btns.append(A_BACK)
    return text, kb(*btns)


@admin.callback_query(F.data == "a:tasks")
async def a_tasks(c: CallbackQuery, state: FSMContext):
    await state.clear()
    text, markup = await tasks_view()
    await edit(c, text, markup)
    await c.answer()


@admin.callback_query(F.data.startswith("a:tv:"))
async def a_task_view(c: CallbackQuery):
    t = await fetchone("SELECT * FROM tasks WHERE id=?", (int(c.data.split(":")[2]),))
    if not t:
        return await c.answer("Не найдено")
    skip = (await fetchone("SELECT COUNT(*) c FROM user_tasks WHERE task_id=? AND status='skip'", (t["id"],)))["c"]
    done = (await fetchone("SELECT COUNT(*) c FROM user_tasks WHERE task_id=? AND status='done'", (t["id"],)))["c"]
    text = (f"📋 <b>Задание #{t['id']}</b>\n\n"
            f"Тип: {'📢 Канал' if t['kind'] == 'channel' else '🤖 Бот'}\n"
            f"Ссылка: {esc(t['url'])}\n"
            f"Проверка: {esc(t['chat_id']) if t['chat_id'] else '— (без проверки)'}\n"
            f"Награда: +{fmt(t['reward'])} ⭐\n"
            f"Статус: {'🟢 включено' if t['active'] else '⚪ выключено'}\n\n"
            f"✅ Выполнили: {done} · ⏭ Пропустили: {skip}")
    await edit(c, text, kb(
        [btn("⚪ Выключить" if t["active"] else "🟢 Включить", cb=f"a:tt:{t['id']}", style="primary"),
         btn("🗑 Удалить", cb=f"a:td:{t['id']}", style="danger")],
        [btn("◀️ Назад", cb="a:tasks")]))
    await c.answer()


@admin.callback_query(F.data.startswith("a:tt:"))
async def a_task_toggle(c: CallbackQuery):
    await run("UPDATE tasks SET active=1-active WHERE id=?", (int(c.data.split(":")[2]),))
    await a_task_view(c)


@admin.callback_query(F.data.startswith("a:td:"))
async def a_task_delete(c: CallbackQuery):
    await run("DELETE FROM tasks WHERE id=?", (int(c.data.split(":")[2]),))
    await c.answer("Удалено 🗑")
    text, markup = await tasks_view()
    await edit(c, text, markup)


CANCEL_TASKS = kb([btn("✖️ Отмена", cb="a:tasks", style="danger")])


@admin.callback_query(F.data == "a:task_add")
async def a_task_add(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await edit(c, "➕ <b>Новое задание</b>\n\nВыбери тип:", kb(
        [btn("📢 Канал", cb="a:tk:channel", style="primary"), btn("🤖 Бот", cb="a:tk:bot", style="success")],
        [btn("◀️ Назад", cb="a:tasks")]))
    await c.answer()


@admin.callback_query(F.data.startswith("a:tk:"))
async def a_task_kind(c: CallbackQuery, state: FSMContext):
    await state.update_data(kind=c.data.split(":")[2])
    await state.set_state(TaskAdd.url)
    await edit(c, "🔗 Отправь <b>ссылку</b> (https://t.me/...) — сюда можно вставить ссылку интеграции спонсора.",
               CANCEL_TASKS)
    await c.answer()


@admin.message(TaskAdd.url, F.text)
async def a_task_url(m: Message, state: FSMContext):
    url = m.text.strip()
    if not url.startswith(("https://", "http://")):
        return await m.answer("❌ Ссылка должна начинаться с https://", reply_markup=CANCEL_TASKS)
    data = await state.get_data()
    await state.update_data(url=url)
    if data["kind"] == "channel":
        pub = re.match(r"https?://t\.me/([A-Za-z][A-Za-z0-9_]{3,})/?$", url)
        if pub:
            await state.update_data(chat="@" + pub.group(1))
        else:
            await state.set_state(TaskAdd.chat)
            return await m.answer(
                "Ссылка приватная — отправь <b>ID канала</b> (вида <code>-100123456789</code>) для проверки подписки.\n"
                "Бот должен быть админом канала. Или отправь «-» чтобы без проверки.", reply_markup=CANCEL_TASKS)
    else:
        await state.update_data(chat="")
    await state.set_state(TaskAdd.reward)
    await m.answer(f"💰 Награда за задание в ⭐ (например <code>0.25</code>) или «-» = {DEFAULT_TASK_REWARD}:",
                   reply_markup=CANCEL_TASKS)


@admin.message(TaskAdd.chat, F.text)
async def a_task_chat(m: Message, state: FSMContext):
    v = m.text.strip()
    await state.update_data(chat="" if v == "-" else v)
    await state.set_state(TaskAdd.reward)
    await m.answer(f"💰 Награда за задание в ⭐ (например <code>0.25</code>) или «-» = {DEFAULT_TASK_REWARD}:",
                   reply_markup=CANCEL_TASKS)


@admin.message(TaskAdd.reward, F.text)
async def a_task_reward(m: Message, state: FSMContext):
    v = m.text.strip().replace(",", ".")
    try:
        reward = DEFAULT_TASK_REWARD if v == "-" else float(v)
        assert reward > 0
    except Exception:
        return await m.answer("❌ Введи число больше нуля, например 0.25", reply_markup=CANCEL_TASKS)
    d = await state.get_data()
    await run("INSERT INTO tasks(kind,url,chat_id,reward) VALUES(?,?,?,?)",
              (d["kind"], d["url"], d.get("chat", ""), reward))
    await state.clear()
    text, markup = await tasks_view()
    await m.answer("✅ Задание добавлено!\n\n" + text, reply_markup=markup)


# ---------- Промокоды ----------
async def promos_view():
    rows = await fetchall("SELECT * FROM promos ORDER BY rowid DESC LIMIT 30")
    btns = []
    for p in rows:
        lim = p["max_uses"] or "∞"
        btns.append([btn(f"🎟 {p['code']} · {fmt(p['reward'])}⭐ · {p['uses']}/{lim}", cb=f"a:pv:{p['code']}")])
    text = "🎟 <b>Промокоды</b>\n\n" + ("Нажми на промокод, чтобы управлять 👇" if rows else "Промокодов пока нет.")
    btns.append([btn("➕ Создать промокод", cb="a:promo_add", style="success")])
    btns.append(A_BACK)
    return text, kb(*btns)


@admin.callback_query(F.data == "a:promos")
async def a_promos(c: CallbackQuery, state: FSMContext):
    await state.clear()
    text, markup = await promos_view()
    await edit(c, text, markup)
    await c.answer()


@admin.callback_query(F.data.startswith("a:pv:"))
async def a_promo_view(c: CallbackQuery):
    p = await fetchone("SELECT * FROM promos WHERE code=?", (c.data.split(":", 2)[2],))
    if not p:
        return await c.answer("Не найден")
    text = (f"🎟 <b>Промокод</b> <code>{esc(p['code'])}</code>\n\n"
            f"Награда: {fmt(p['reward'])} ⭐\n"
            f"Активаций: {p['uses']} / {p['max_uses'] or '∞'}")
    await edit(c, text, kb([btn("🗑 Удалить", cb=f"a:pd:{p['code']}", style="danger")],
                           [btn("◀️ Назад", cb="a:promos", style="primary")]))
    await c.answer()


@admin.callback_query(F.data.startswith("a:pd:"))
async def a_promo_del(c: CallbackQuery):
    code = c.data.split(":", 2)[2]
    await run("DELETE FROM promos WHERE code=?", (code,))
    await c.answer("Удалено 🗑")
    text, markup = await promos_view()
    await edit(c, text, markup)


CANCEL_PROMOS = kb([btn("✖️ Отмена", cb="a:promos", style="danger")])


@admin.callback_query(F.data == "a:promo_add")
async def a_promo_add(c: CallbackQuery, state: FSMContext):
    await state.set_state(PromoAdd.code)
    await edit(c, "➕ <b>Новый промокод</b>\n\nОтправь текст промокода (латиница/цифры) или «-» для случайного:",
               CANCEL_PROMOS)
    await c.answer()


@admin.message(PromoAdd.code, F.text)
async def a_promo_code(m: Message, state: FSMContext):
    v = m.text.strip().upper()
    if v == "-":
        v = "".join(random.choices(string.ascii_uppercase + string.digits, k=8))
    if not re.fullmatch(r"[A-Z0-9_]{3,32}", v):
        return await m.answer("❌ Только латиница, цифры и _ (3–32 символа)", reply_markup=CANCEL_PROMOS)
    if await fetchone("SELECT 1 FROM promos WHERE code=?", (v,)):
        return await m.answer("❌ Такой промокод уже есть", reply_markup=CANCEL_PROMOS)
    await state.update_data(code=v)
    await state.set_state(PromoAdd.reward)
    await m.answer("💰 Сколько ⭐ даёт промокод? (например <code>1</code>)", reply_markup=CANCEL_PROMOS)


@admin.message(PromoAdd.reward, F.text)
async def a_promo_reward(m: Message, state: FSMContext):
    try:
        r = float(m.text.strip().replace(",", "."))
        assert r > 0
    except Exception:
        return await m.answer("❌ Введи число больше нуля", reply_markup=CANCEL_PROMOS)
    await state.update_data(reward=r)
    await state.set_state(PromoAdd.uses)
    await m.answer("👥 Сколько раз можно активировать? (<code>0</code> = без лимита)", reply_markup=CANCEL_PROMOS)


@admin.message(PromoAdd.uses, F.text)
async def a_promo_uses(m: Message, state: FSMContext):
    try:
        n = int(m.text.strip())
        assert n >= 0
    except Exception:
        return await m.answer("❌ Введи целое число (0 = без лимита)", reply_markup=CANCEL_PROMOS)
    d = await state.get_data()
    await run("INSERT INTO promos(code,reward,max_uses) VALUES(?,?,?)", (d["code"], d["reward"], n))
    await state.clear()
    text, markup = await promos_view()
    await m.answer(f"✅ Промокод <code>{d['code']}</code> создан: {fmt(d['reward'])} ⭐, лимит: {n or '∞'}\n\n" + text,
                   reply_markup=markup)


# ---------- Пользователи ----------
async def user_card(uid: int):
    u = await get_user(uid)
    if not u:
        return None, None
    invited = await ref_count(uid)
    done = (await fetchone("SELECT COUNT(*) c FROM done_tasks WHERE user_id=?", (uid,)))["c"]
    uname = f"@{u['username']}" if u["username"] else "—"
    text = (f"👤 <b>{esc(u['full_name'])}</b> ({esc(uname)})\n"
            f"ID: <code>{uid}</code>\n"
            f"Регистрация: {u['created_at']}\n"
            f"Пригласил: {u['referrer_id'] or '—'}\n\n"
            f"💰 Баланс: {fmt(u['balance'])} ⭐\n"
            f"📈 Заработано: {fmt(u['earned'])} · 📤 Выведено: {fmt(u['withdrawn'])}\n"
            f"👥 Рефералов: {invited} · ✅ Заданий: {done}\n"
            f"Статус: {'🚫 забанен' if u['banned'] else '✅ активен'}")
    markup = kb(
        [btn("➕ Начислить", cb=f"a:uadd:{uid}", style="success"),
         btn("➖ Списать", cb=f"a:usub:{uid}", style="danger")],
        [btn("✅ Разбанить" if u["banned"] else "🚫 Забанить", cb=f"a:uban:{uid}", style="primary")],
        [btn("◀️ Назад", cb="a:users")])
    return text, markup


@admin.callback_query(F.data == "a:users")
async def a_users(c: CallbackQuery, state: FSMContext):
    await state.set_state(UserAdm.find)
    await edit(c, "👥 <b>Пользователи</b>\n\nОтправь <b>ID</b> или <b>@username</b> пользователя:",
               kb(A_BACK))
    await c.answer()


@admin.message(UserAdm.find, F.text)
async def a_user_find(m: Message, state: FSMContext):
    v = m.text.strip().lstrip("@")
    row = await get_user(int(v)) if v.isdigit() else await fetchone(
        "SELECT * FROM users WHERE LOWER(username)=LOWER(?)", (v,))
    if not row:
        return await m.answer("❌ Пользователь не найден. Попробуй ещё раз:", reply_markup=kb(A_BACK))
    await state.clear()
    text, markup = await user_card(row["id"])
    await m.answer(text, reply_markup=markup)


@admin.callback_query(F.data.startswith("a:uban:"))
async def a_user_ban(c: CallbackQuery):
    uid = int(c.data.split(":")[2])
    await run("UPDATE users SET banned=1-banned WHERE id=?", (uid,))
    text, markup = await user_card(uid)
    await edit(c, text, markup)
    await c.answer("Готово")


@admin.callback_query(F.data.startswith(("a:uadd:", "a:usub:")))
async def a_user_bal(c: CallbackQuery, state: FSMContext):
    _, kind, uid = c.data.split(":")
    await state.set_state(UserAdm.amount)
    await state.update_data(uid=int(uid), sign=1 if kind == "uadd" else -1)
    await edit(c, "Введи сумму в ⭐ (например <code>5</code>):", kb(A_BACK))
    await c.answer()


@admin.message(UserAdm.amount, F.text)
async def a_user_amount(m: Message, state: FSMContext):
    try:
        v = float(m.text.strip().replace(",", "."))
        assert v > 0
    except Exception:
        return await m.answer("❌ Введи число больше нуля", reply_markup=kb(A_BACK))
    d = await state.get_data()
    if d["sign"] > 0:
        await run("UPDATE users SET balance=balance+?, earned=earned+? WHERE id=?", (v, v, d["uid"]))
    else:
        await run("UPDATE users SET balance=MAX(0, balance-?) WHERE id=?", (v, d["uid"]))
    await state.clear()
    text, markup = await user_card(d["uid"])
    await m.answer("✅ Баланс обновлён\n\n" + text, reply_markup=markup)


# ---------- Рассылка ----------
@admin.callback_query(F.data == "a:cast")
async def a_cast(c: CallbackQuery, state: FSMContext):
    await state.set_state(Cast.msg)
    await edit(c, "📨 <b>Рассылка</b>\n\nОтправь сообщение для рассылки (текст, фото, видео — что угодно, "
                  "форматирование сохранится):", kb(A_BACK))
    await c.answer()


@admin.message(Cast.msg)
async def a_cast_msg(m: Message, state: FSMContext):
    await state.update_data(chat=m.chat.id, mid=m.message_id)
    n = (await fetchone("SELECT COUNT(*) c FROM users WHERE banned=0"))["c"]
    await m.answer(f"👆 Так выглядит сообщение.\n\nОтправить <b>{n}</b> пользователям?", reply_markup=kb(
        [btn("✅ Запустить", cb="a:cast_go", style="success"),
         btn("✖️ Отмена", cb="a:menu", style="danger")]))


async def _cast_one(bot: Bot, uid: int, d: dict) -> int:
    try:
        await bot.copy_message(uid, d["chat"], d["mid"])
        return 1
    except TelegramRetryAfter as ex:
        await asyncio.sleep(min(ex.retry_after, 5))
        try:
            await bot.copy_message(uid, d["chat"], d["mid"])
            return 1
        except Exception:
            return 0
    except Exception:
        return 0


@admin.callback_query(F.data == "a:cast_go")
async def a_cast_go(c: CallbackQuery, state: FSMContext):
    """Рассылка порциями: один вызов Vercel живёт ≤ 60 с, поэтому прогресс хранится в cast_log, а «Продолжить» дошлёт остальным."""
    d = await state.get_data()
    if not d:
        return await c.answer("Сессия устарела", show_alert=True)
    ck = f"{d['chat']}:{d['mid']}"
    ids = [r["id"] for r in await fetchall(
        "SELECT id FROM users WHERE banned=0 AND id NOT IN (SELECT user_id FROM cast_log WHERE ck=?) LIMIT 5000", (ck,))]
    await edit(c, f"⏳ Рассылка идёт… осталось {len(ids)}")
    deadline = time.monotonic() + 40
    i = 0
    while i < len(ids) and time.monotonic() < deadline:
        batch = ids[i:i + 20]
        i += len(batch)
        res = await asyncio.gather(*(_cast_one(c.bot, uid, d) for uid in batch))
        await db.batch([("INSERT OR IGNORE INTO cast_log(ck,user_id,ok) VALUES(?,?,?)", (ck, uid, ok))
                        for uid, ok in zip(batch, res)])
        await asyncio.sleep(0.7)
    left = len(ids) - i
    if left > 0:
        try:
            await c.message.edit_text(
                f"⏳ Отправлено порцию, осталось <b>{left}</b>. Нажми «Продолжить».",
                reply_markup=kb([btn("▶️ Продолжить", cb="a:cast_go", style="success")], A_BACK))
        except TelegramBadRequest:
            pass
        return
    st = await fetchone("SELECT COALESCE(SUM(ok),0) AS ok, COUNT(*) AS n FROM cast_log WHERE ck=?", (ck,))
    await state.clear()
    await run("DELETE FROM cast_log WHERE ck=?", (ck,))
    try:
        await c.message.edit_text(
            f"✅ <b>Рассылка завершена</b>\n\nДоставлено: {st['ok']}\nНе доставлено: {st['n'] - st['ok']}",
            reply_markup=kb(A_BACK))
    except TelegramBadRequest:
        pass


# ---------- Настройки ----------
async def settings_view():
    rr, rt = await get_setting("ref_reward"), int(await get_setting("ref_tasks"))
    fr = await get_setting("flyer_reward")
    gr = await get_setting("tgrass_reward")
    xr = {k: await get_setting(f"{k}_reward") for k in EXT_NAMES}
    text = (f"⚙️ <b>Настройки</b>\n\n"
            f"💰 Награда за реферала: <b>{fmt(rr)}</b> ⭐\n"
            f"🎯 Заданий для получения награды: <b>{rt}</b>\n"
            f"🛰 Награда за задание Flyer: <b>{fmt(fr)}</b> ⭐\n"
            f"🌿 Награда за задание TGrass: <b>{fmt(gr)}</b> ⭐\n"
            f"📡 Trafsly: <b>{fmt(xr['trafsly'])}</b> ⭐ · PiarFlow: <b>{fmt(xr['piarflow'])}</b> ⭐ · "
            f"Traffy: <b>{fmt(xr['traffy'])}</b> ⭐")
    return text, kb([btn("💰 Награда за реферала", cb="a:set:ref_reward", style="primary")],
                    [btn("🎯 Кол-во заданий", cb="a:set:ref_tasks", style="primary")],
                    [btn("🛰 Награда за Flyer-задание", cb="a:set:flyer_reward", style="success")],
                    [btn("🌿 Награда за TGrass-задание", cb="a:set:tgrass_reward", style="success")],
                    [btn("📡 Trafsly", cb="a:set:trafsly_reward", style="success"),
                     btn("📡 PiarFlow", cb="a:set:piarflow_reward", style="success"),
                     btn("📡 Traffy", cb="a:set:traffy_reward", style="success")], A_BACK)


@admin.callback_query(F.data == "a:set")
async def a_set(c: CallbackQuery, state: FSMContext):
    await state.clear()
    text, markup = await settings_view()
    await edit(c, text, markup)
    await c.answer()


@admin.callback_query(F.data.startswith("a:set:"))
async def a_set_key(c: CallbackQuery, state: FSMContext):
    key = c.data.split(":")[2]
    await state.set_state(SetSt.value)
    await state.update_data(key=key)
    label = {"ref_reward": "награду за реферала (⭐)", "ref_tasks": "количество заданий",
             "flyer_reward": "награду за задание Flyer (⭐)",
             "tgrass_reward": "награду за задание TGrass (⭐)",
             "trafsly_reward": "награду за задание Trafsly (⭐)",
             "piarflow_reward": "награду за задание PiarFlow (⭐)",
             "traffy_reward": "награду за задание Traffy (⭐)"}.get(key, key)
    await edit(c, f"Введи новое значение: <b>{label}</b>", kb([btn("✖️ Отмена", cb="a:set", style="danger")]))
    await c.answer()


@admin.message(SetSt.value, F.text)
async def a_set_value(m: Message, state: FSMContext):
    d = await state.get_data()
    try:
        v = float(m.text.strip().replace(",", "."))
        assert v > 0
        if d["key"] == "ref_tasks":
            v = int(v)
    except Exception:
        return await m.answer("❌ Введи положительное число")
    await run("UPDATE settings SET value=? WHERE key=?", (str(v), d["key"]))
    _SET_CACHE.clear()
    await state.clear()
    text, markup = await settings_view()
    await m.answer("✅ Сохранено\n\n" + text, reply_markup=markup)


# ============================== FASTAPI-ПРИЛОЖЕНИЕ (webhook / cron) ==============================
import hmac
import re
from urllib.parse import parse_qs
from aiogram.types import Update
from fastapi import FastAPI, HTTPException, Request


logging.basicConfig(level=logging.INFO)

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


_ENDPOINT = re.compile(r"(?:^|/)(set_webhook|webhook|cron|hook/[A-Za-z0-9_-]+)/?$")


class PathFix:
    """Vercel может отдать приложению путь /api/index; настоящий берём из ?route=... или хвоста пути."""

    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            qs = parse_qs(scope.get("query_string", b"").decode())
            route = (qs.get("route") or [""])[0]
            m = _ENDPOINT.search(route.strip("/") or scope["path"])
            if m:
                scope["path"] = "/api/" + m.group(1)
        await self.inner(scope, receive, send)


app.add_middleware(PathFix)

dp = Dispatcher(storage=TursoStorage())
dp.message.middleware(BanMiddleware())
dp.callback_query.middleware(BanMiddleware())
dp.include_router(admin)
dp.include_router(user)


def make_bot() -> Bot:
    if not BOT_TOKEN:
        raise HTTPException(500, "BOT_TOKEN не задан в Environment Variables")
    return Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))


def same(a: str, b: str) -> bool:
    return bool(a) and bool(b) and hmac.compare_digest(a.encode(), b.encode())


@app.get("/")
async def root():
    return {"ok": True}


@app.post("/api/webhook")
async def webhook(request: Request):
    """Сюда Telegram шлёт апдейты."""
    if WEBHOOK_SECRET and not same(request.headers.get("x-telegram-bot-api-secret-token", ""),
                                        WEBHOOK_SECRET):
        raise HTTPException(403, "bad secret")
    data = await request.json()
    bot = make_bot()
    try:
        update = Update.model_validate(data, context={"bot": bot})
        await ensure_ready(bot)
        await dp.feed_update(bot, update)
    except Exception:
        logging.exception("update failed")
    finally:
        await bot.session.close()
    return {"ok": True}  # всегда 200, иначе Telegram будет бесконечно слать апдейт повторно


@app.get("/api/set_webhook")
async def set_webhook(request: Request, key: str = ""):
    """Один раз открой в браузере: https://ТВОЙ-ДОМЕН.vercel.app/api/set_webhook?key=WEBHOOK_SECRET"""
    if not same(key, WEBHOOK_SECRET):
        raise HTTPException(403, "bad key")
    host = request.headers.get("x-forwarded-host") or request.url.netloc
    url = f"https://{host}/api/webhook"
    bot = make_bot()
    try:
        types = dp.resolve_used_update_types()  # включает chat_member -> мгновенные штрафы за отписку
        ok = await bot.set_webhook(url, secret_token=WEBHOOK_SECRET or None, allowed_updates=types)
    finally:
        await bot.session.close()
    return {"ok": ok, "url": url, "allowed_updates": types}


@app.get("/api/cron")
async def cron(request: Request):
    """Проверка отписок. Vercel Cron сам шлёт заголовок Authorization: Bearer <CRON_SECRET>."""
    auth = request.headers.get("authorization", "")
    if not same(auth, f"Bearer {CRON_SECRET}"):
        raise HTTPException(403, "bad cron secret")
    bot = make_bot()
    try:
        await ensure_ready(bot)
        report = await check_unsubs(bot)
    finally:
        await bot.session.close()
    return {"ok": True, "report": report}


@app.post("/api/hook/{provider}")
async def provider_hook(provider: str, request: Request, key: str = ""):
    """Вебхуки отписок: /api/hook/piarflow?key=WEBHOOK_SECRET (так же для trafsly и traffy)."""
    if not same(key, WEBHOOK_SECRET):
        raise HTTPException(403, "bad key")
    if provider not in ("piarflow", "trafsly", "traffy"):
        raise HTTPException(404, "unknown provider")
    payload = await request.json()
    bot = make_bot()
    try:
        await ensure_ready(bot)
        n = await handle_provider_hook(bot, provider, payload)
    finally:
        await bot.session.close()
    return {"ok": True, "penalized": n}


@app.api_route("/{full_path:path}", methods=["GET", "POST"], include_in_schema=False)
async def fallback(request: Request, full_path: str):
    """Диагностика: показывает, какой путь реально пришёл (значения query не выводятся)."""
    return {"error": "no route", "seen_path": request.scope["path"],
            "query_keys": list(request.query_params.keys())}
