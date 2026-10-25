import asyncio
import hmac
import html
import json
import os
import re
import time
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import aiohttp
from flask import Flask, request
from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.base import BaseStorage, StorageKey, StateType
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    ChatMemberUpdated,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    Update,
)

# ======================================================================
#                         НАСТРОЙКИ
#  Всё задаётся через переменные окружения Vercel.
#  Если переменной нет — используется значение-заглушка.
# ======================================================================
BOT_TOKEN = os.environ.get("BOT_TOKEN")   # токен бота (из Vercel)
ADMIN_ID = 619112100                      # ID администратора
# Кому доступна команда /woot (полный сброс своего аккаунта для тестов)
WOOT_IDS = {ADMIN_ID, 8789146386, 792875116}

# Наш канал — всегда показывается как 6-й спонсор (бот должен быть АДМИНОМ канала)
OWN_CHANNEL_ID = -1004361145540
OWN_CHANNEL_LINK = "https://t.me/DeveloperLead"

TGRASS_API_KEY = os.environ.get("TGRASS_API_KEY", "6d6e9e9701e7440a81fe099d360710d5")
PIARFLOW_API_KEY = os.environ.get("PIARFLOW_API_KEY", "FNDvblihCM-RXnMX1U_UrErt_KuD8ujv")
TRAFSLY_API_KEY = os.environ.get("TRAFSLY_API_KEY", "at_99ddd0618296db80b905c92ff9adaa8c")
TRAFFY_API_KEY = os.environ.get("TRAFFY_API_KEY", "pk_live_DJZaxY7dRIECCycKvEL5ALT0wW7UhEKb")
FLYER_API_KEY = os.environ.get("FLYER_API_KEY", "FL-VZEliI-kGJGIJ-iSddvG-KPxDqs")

# Upstash Redis — ОБЯЗАТЕЛЕН: в нём хранятся все данные бота.
# Vercel -> Storage -> Upstash Redis
# Vercel может назвать переменные по-разному (UPSTASH_REDIS_REST_* или KV_REST_API_*) — поддерживаем оба варианта
UPSTASH_URL = os.environ.get("UPSTASH_REDIS_REST_URL") or os.environ.get("KV_REST_API_URL")
UPSTASH_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN") or os.environ.get("KV_REST_API_TOKEN")
# ======================================================================

PIARFLOW_API_URL = "https://piarflow.com/v1"
TRAFSLY_API_URL = "https://api.trafsly.com"
TGRASS_API_URL = "https://tgrass.space"
TRAFFY_API_URL = "https://api.traffy.ai/publisher"
FLYER_API_URL = "https://api.flyerhubs.com"

PLACEHOLDER_MARK = "ВСТАВЬТЕ_КЛЮЧ"


def key_filled(value: Optional[str]) -> bool:
    return bool(value) and PLACEHOLDER_MARK not in value


for _name, _val in (
    ("BOT_TOKEN", BOT_TOKEN),
    ("TGRASS_API_KEY", TGRASS_API_KEY),
    ("PIARFLOW_API_KEY", PIARFLOW_API_KEY),
    ("TRAFSLY_API_KEY", TRAFSLY_API_KEY),
    ("TRAFFY_API_KEY", TRAFFY_API_KEY),
    ("FLYER_API_KEY", FLYER_API_KEY),
    ("UPSTASH_REDIS_REST_URL", UPSTASH_URL),
    ("UPSTASH_REDIS_REST_TOKEN", UPSTASH_TOKEN),
):
    if not key_filled(_val):
        print(f"⚠️ НЕ ЗАПОЛНЕНО: {_name}")

PHOTO_URL = "https://i.postimg.cc/XvPS1pB2/5883351235946024467.jpg"
REFERRAL_PHOTO_URL = "https://i.postimg.cc/TYbwVvFM/6219881422739149359.jpg"
PROFILE_PHOTO_URL = "https://i.postimg.cc/bvcFZkbf/6219881422739149361.jpg"
TOP_PHOTO_URL = "https://i.postimg.cc/tJfCWpFM/6219881422739149363.jpg"
WITHDRAW_PHOTO_URL = "https://i.postimg.cc/RVdQKYYf/6219881422739149362.jpg"

TARGET_COUNT = 6                  # всего кнопок: 5 спонсоров из сервисов + наш канал
EXTERNAL_LIMIT = 5                # максимум спонсоров из сервисов
INCOME_PER_SPONSOR = 1.0          # доход (руб) за одну подписку на спонсора
DEFAULT_REFERRAL_REWARD = 4.5     # награда за реферала по умолчанию
FLYER_REWARD = 0.25               # награда за одно задание Flyer, ⭐
FLYER_TASKS_LIMIT = 5             # сколько заданий Flyer закрепляется за пользователем (макс. 10)
MIN_SPONSORS_FOR_REWARD = 4       # минимум спонсоров (3 из сервисов + наш канал), чтобы пригласивший получил награду
FSM_TTL = 7 * 24 * 3600           # сколько хранить состояние диалога


# ================= REDIS (Upstash REST) =================
# Сессия aiohttp на время одного апдейта — чтобы не открывать соединение на каждую команду.
_redis_session: ContextVar = ContextVar("redis_session", default=None)
# Бот текущего апдейта — нужен для проверки подписки на наш канал внутри проверок
_current_bot: ContextVar = ContextVar("current_bot", default=None)


def redis_enabled() -> bool:
    return bool(UPSTASH_URL and UPSTASH_TOKEN)


async def redis_cmd(*args):
    """Выполняет команду Redis через Upstash REST. При ошибке или без Redis — None."""
    if not redis_enabled():
        return None
    payload = [str(a) for a in args]
    session = _redis_session.get()
    own = session is None or session.closed
    if own:
        session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5))
    try:
        async with session.post(
            UPSTASH_URL,
            json=payload,
            headers={"Authorization": f"Bearer {UPSTASH_TOKEN}"},
        ) as resp:
            data = await resp.json(content_type=None)
            if isinstance(data, dict) and data.get("error"):
                print(f"redis error [{payload[0]}]: {data['error']}")
                return None
            return data.get("result") if isinstance(data, dict) else None
    except Exception as e:
        print(f"redis exception [{payload[0] if payload else ''}]: {e!r}")
        return None
    finally:
        if own:
            await session.close()


async def redis_pipeline(commands: list) -> list:
    """Несколько команд Redis одним запросом (Upstash /pipeline). При сбое — по одной."""
    if not commands:
        return []
    if not redis_enabled():
        return [None] * len(commands)
    payload = [[str(a) for a in cmd] for cmd in commands]
    session = _redis_session.get()
    own = session is None or session.closed
    if own:
        session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5))
    try:
        async with session.post(
            UPSTASH_URL.rstrip("/") + "/pipeline",
            json=payload,
            headers={"Authorization": f"Bearer {UPSTASH_TOKEN}"},
        ) as resp:
            data = await resp.json(content_type=None)
        if isinstance(data, list) and len(data) == len(commands):
            return [d.get("result") if isinstance(d, dict) else None for d in data]
    except Exception as e:
        print(f"redis pipeline exception: {e!r}")
    finally:
        if own:
            await session.close()
    return list(await asyncio.gather(*(redis_cmd(*cmd) for cmd in commands)))


async def redis_get_json(key: str, default=None):
    raw = await redis_cmd("GET", key)
    if raw is None:
        return default
    try:
        return json.loads(raw)
    except Exception:
        return default


async def redis_set_json(key: str, value, ex: Optional[int] = None) -> None:
    args = ["SET", key, json.dumps(value, ensure_ascii=False, default=str)]
    if ex:
        args += ["EX", ex]
    await redis_cmd(*args)


def to_int(value, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def to_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


# ================= FSM-ХРАНИЛИЩЕ В REDIS =================
class UpstashStorage(BaseStorage):
    """Состояния диалогов (ввод суммы, username, промокода и т.д.) хранятся в Redis,
    поэтому не теряются при перезапуске Vercel."""

    @staticmethod
    def _k(key: StorageKey, part: str) -> str:
        return f"fsm:{key.chat_id}:{key.user_id}:{part}"

    async def set_state(self, key: StorageKey, state: StateType = None) -> None:
        value = state.state if isinstance(state, State) else state
        if value is None:
            await redis_cmd("DEL", self._k(key, "state"))
        else:
            await redis_cmd("SET", self._k(key, "state"), value, "EX", FSM_TTL)

    async def get_state(self, key: StorageKey) -> Optional[str]:
        return await redis_cmd("GET", self._k(key, "state"))

    async def set_data(self, key: StorageKey, data: dict[str, Any]) -> None:
        if not data:
            await redis_cmd("DEL", self._k(key, "data"))
        else:
            await redis_set_json(self._k(key, "data"), dict(data), ex=FSM_TTL)

    async def get_data(self, key: StorageKey) -> dict[str, Any]:
        data = await redis_get_json(self._k(key, "data"), {})
        return data if isinstance(data, dict) else {}

    async def close(self) -> None:
        pass


dp = Dispatcher(storage=UpstashStorage() if redis_enabled() else MemoryStorage())
router = Router()


class AlwaysAnswerCallback(BaseMiddleware):
    """Гарантирует ответ на нажатие кнопки. Если обработчик упал с ошибкой или забыл
    ответить, Telegram иначе держит кнопку «в загрузке» и повторно нажать её нельзя."""

    async def __call__(self, handler, event, data):
        try:
            return await handler(event, data)
        finally:
            try:
                await event.answer()  # если уже отвечали — Telegram вернёт ошибку, её глушим
            except Exception:
                pass


router.callback_query.outer_middleware(AlwaysAnswerCallback())


# ================= FSM =================
class WithdrawStates(StatesGroup):
    waiting_for_custom_amount = State()
    waiting_for_username = State()
    waiting_for_confirmation = State()


class PromocodeStates(StatesGroup):
    waiting_for_activation_input = State()
    admin_name = State()
    admin_activations = State()
    admin_price = State()


class AdminBroadcastStates(StatesGroup):
    waiting_for_text = State()
    confirm = State()


class AdminTaskStates(StatesGroup):
    text = State()
    link = State()
    chat_id = State()
    reward = State()
    max_completions = State()
    preview = State()


class AdminRewardStates(StatesGroup):
    waiting_for_new_value = State()
    confirm = State()


# ================= ХЕЛПЕРЫ ИНТЕРФЕЙСА =================
try:  # aiogram 3.7+
    from aiogram.types import LinkPreviewOptions
except ImportError:  # более старые версии
    LinkPreviewOptions = None


def no_preview() -> dict:
    """Параметры отправки сообщения без предпросмотра ссылок."""
    if LinkPreviewOptions is not None:
        return {"link_preview_options": LinkPreviewOptions(is_disabled=True)}
    return {"disable_web_page_preview": True}


async def safe_answer(callback: CallbackQuery, *args, **kwargs) -> None:
    """Повторный callback.answer() вызывает ошибку, поэтому глушим её."""
    try:
        await callback.answer(*args, **kwargs)
    except Exception:
        pass


def back_markup(cb: str = "back_to_main", text: str = "⬅️ Назад") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=text, callback_data=cb)]])


async def replace_message(
    callback: CallbackQuery, text: str, markup=None,
    parse_mode: Optional[str] = None, disable_preview: bool = False,
) -> bool:
    """Отправляет новое сообщение и удаляет старое. True — новое сообщение отправлено."""
    try:
        kwargs = {"parse_mode": parse_mode} if parse_mode else {}
        if disable_preview:
            kwargs.update(no_preview())
        await callback.message.answer(text, reply_markup=markup, **kwargs)
    except Exception as e:
        print(f"replace_message error: {e}")
        return False
    try:
        await callback.message.delete()
    except Exception:
        pass
    return True


async def replace_with_photo(callback: CallbackQuery, photo: str, text: str, markup=None) -> None:
    try:
        await callback.message.answer_photo(photo=photo, caption=text, reply_markup=markup)
    except Exception as e:
        print(f"photo error: {e}")
        try:
            await callback.message.answer(text, reply_markup=markup)
        except Exception:
            return
    try:
        await callback.message.delete()
    except Exception:
        pass


# ================= ПОЛЬЗОВАТЕЛИ (Redis) =================
def user_key(uid: int) -> str:
    return f"user:{uid}"


def _parse_user(res) -> Optional[dict]:
    if not res:
        return None
    if isinstance(res, list):
        d = dict(zip(res[0::2], res[1::2]))
    elif isinstance(res, dict):
        d = res
    else:
        return None
    return {
        "name": d.get("name") or "",
        "username": d.get("username") or "",
        "balance": to_float(d.get("balance")),
        "referrals": to_int(d.get("referrals")),
        "join_ts": to_float(d.get("join_ts")),
    }


async def get_user(uid: int) -> Optional[dict]:
    return _parse_user(await redis_cmd("HGETALL", user_key(uid)))


async def _create_user(uid: int, name: str, username: str) -> dict:
    now = time.time()
    await redis_cmd(
        "HSET", user_key(uid),
        "name", name, "username", username,
        "balance", "0", "referrals", "0", "join_ts", str(now),
    )
    await redis_cmd("SADD", "users", uid)
    await redis_cmd("ZADD", "users_joined", now, uid)
    return {"name": name, "username": username, "balance": 0.0, "referrals": 0, "join_ts": now}


async def ensure_user(user) -> dict:
    """Возвращает данные пользователя; при первом обращении создаёт запись.
    В результате есть ключ is_new (True, если пользователь создан только что)."""
    uid = user.id
    username_str = f"@{user.username}" if user.username else f"id{uid}"
    data = await get_user(uid)
    if data is None:
        data = await _create_user(uid, user.first_name or "", username_str)
        data["is_new"] = True
    else:
        data["is_new"] = False
    return data


async def ensure_user_by_id(uid: int) -> dict:
    data = await get_user(uid)
    if data is None:
        data = await _create_user(uid, f"id{uid}", f"id{uid}")
    return data


async def add_balance(uid: int, amount: float) -> Optional[float]:
    """Атомарно меняет баланс. Возвращает новый баланс."""
    res = await redis_cmd("HINCRBYFLOAT", user_key(uid), "balance", amount)
    return to_float(res) if res is not None else None


# ================= НАСТРОЙКИ И СТАТИСТИКА (Redis) =================
async def get_referral_reward() -> float:
    val = await redis_cmd("GET", "cfg:referral_reward")
    return to_float(val, DEFAULT_REFERRAL_REWARD) if val is not None else DEFAULT_REFERRAL_REWARD


async def add_referral_to_top(ref_id: int, name: str) -> None:
    await redis_cmd("ZINCRBY", "top_refs", 1, ref_id)
    await redis_cmd("HSET", "top_names", ref_id, name or f"id{ref_id}")


async def get_top(limit: int = 5) -> list[tuple[int, str, int]]:
    """Возвращает [(user_id, имя, рефералов), ...] от большего к меньшему."""
    res = await redis_cmd("ZREVRANGE", "top_refs", 0, limit - 1, "WITHSCORES")
    if not res:
        return []
    ids = res[0::2]
    scores = res[1::2]
    names = await redis_cmd("HMGET", "top_names", *ids) if ids else []
    names = names or [None] * len(ids)
    return [
        (int(uid), name or f"id{uid}", to_int(score))
        for uid, name, score in zip(ids, names, scores)
    ]


async def get_user_refs_and_rank(user_id: int) -> tuple[int, Optional[int]]:
    score = await redis_cmd("ZSCORE", "top_refs", user_id)
    if score is None:
        u = await get_user(user_id)
        return (u["referrals"] if u else 0), None
    rank = await redis_cmd("ZREVRANK", "top_refs", user_id)
    return to_int(score), (int(rank) + 1 if rank is not None else None)


async def mark_passed(uid: int) -> bool:
    """Пользователь прошёл спонсоров — учитываем один раз."""
    added = await redis_cmd("SADD", "passed_users", uid)
    if to_int(added) == 1:
        await redis_cmd("INCR", "stat:sponsors_completed")
        return True
    return False


async def add_income(user_id: int, count: int) -> None:
    """Добавляет к доходу число спонсоров, на которых подписался пользователь.
    Каждый пользователь учитывается один раз."""
    if count <= 0:
        return
    added = await redis_cmd("SADD", "income_users", user_id)
    if added is None or to_int(added) == 0:
        return
    await redis_cmd("INCRBY", "income_sponsors", count)


async def get_income_sponsors() -> int:
    return to_int(await redis_cmd("GET", "income_sponsors"))


async def pop_sponsor_total(user_id: int, fallback: int = 0) -> int:
    """Достаёт из Redis исходное число показанных спонсоров и удаляет ключ."""
    val = await redis_cmd("GET", f"sp_total:{user_id}")
    await redis_cmd("DEL", f"sp_total:{user_id}")
    return min(max(to_int(val), fallback), TARGET_COUNT)  # доход за одного пользователя — не больше TARGET_COUNT


# ================= АКТИВНЫЕ СПОНСОРЫ / ВЫВОД (Redis) =================
async def get_active(uid: int) -> Optional[dict]:
    return await redis_get_json(f"sp_active:{uid}")


async def set_active(uid: int, data: dict) -> None:
    await redis_set_json(f"sp_active:{uid}", data, ex=86400)


async def del_active(uid: int) -> None:
    await redis_cmd("DEL", f"sp_active:{uid}")


async def get_wd(uid: int) -> dict:
    return await redis_get_json(f"wd:{uid}", {}) or {}


async def set_wd(uid: int, data: dict) -> None:
    await redis_set_json(f"wd:{uid}", data, ex=3600)


async def del_wd(uid: int) -> None:
    await redis_cmd("DEL", f"wd:{uid}")


# ================= ЗАДАНИЯ (Redis) =================
TASKS_KEY = "admin_tasks"


async def load_tasks() -> list[dict]:
    tasks = await redis_get_json(TASKS_KEY, [])
    return tasks if isinstance(tasks, list) else []


async def save_tasks(tasks: list[dict]) -> None:
    await redis_set_json(TASKS_KEY, tasks)


async def pick_task(uid: int, reset: bool = True) -> Optional[dict]:
    """Первое задание, которое пользователь ещё не выполнял и не пропускал.
    reset=False — если все оставшиеся пропущены, вернуть None (чтобы показать задания из других источников)."""
    tasks = await load_tasks()
    if not tasks:
        return None
    done = {to_int(x) for x in (await redis_cmd("SMEMBERS", f"user_done:{uid}") or [])}
    candidates = [t for t in tasks if t["id"] not in done]
    if not candidates:
        return None
    skipped = {to_int(x) for x in (await redis_cmd("SMEMBERS", f"user_skip:{uid}") or [])}
    fresh = [t for t in candidates if t["id"] not in skipped]
    if not fresh:
        if not reset:
            return None
        await redis_cmd("DEL", f"user_skip:{uid}")
        fresh = candidates
    return fresh[0]


# ================= ПРОВЕРКА ПОДПИСКИ НА КАНАЛ ЗАДАНИЯ =================
def normalize_task_link(text: str) -> Optional[str]:
    """Приводит ввод админа к ссылке https://t.me/... Возвращает None, если формат не подходит."""
    t = (text or "").strip()
    if re.fullmatch(r"@\w{4,}", t):
        return f"https://t.me/{t[1:]}"
    m = re.match(r"^(?:https?://)?(?:t|telegram)\.me/(\S+)$", t, re.I)
    if m:
        return f"https://t.me/{m.group(1)}"
    return None


def chat_ref_from_link(link: str) -> Optional[str]:
    """@username канала из публичной ссылки. Для закрытых ссылок (t.me/+...) возвращает None."""
    m = re.match(r"^(?:https?://)?(?:t|telegram)\.me/(\S+)$", (link or "").strip(), re.I)
    if not m:
        return None
    first = m.group(1).split("?")[0].strip("/").split("/")[0]
    if not first or first.startswith("+") or first.lower() in ("joinchat", "c", "addlist"):
        return None
    return f"@{first}" if re.fullmatch(r"\w{4,}", first) else None


def _status_str(member) -> str:
    return str(getattr(member.status, "value", member.status))


async def validate_task_chat(bot: Bot, chat_ref) -> tuple[Optional[int], str, str]:
    """Проверяет, что канал существует и бот в нём администратор (иначе подписки не проверить).
    Возвращает (chat_id, название, текст_ошибки)."""
    try:
        chat = await bot.get_chat(chat_ref)
    except Exception:
        return None, "", "❌ Канал не найден. Проверьте ссылку или ID и попробуйте снова."
    try:
        me = await bot.me()
        member = await bot.get_chat_member(chat.id, me.id)
    except Exception:
        member = None
    if member is None or _status_str(member) not in ("administrator", "creator"):
        return None, "", (
            "❌ Бот не администратор в этом канале, поэтому не сможет проверять подписки.\n"
            "Добавьте бота админом и отправьте ссылку снова."
        )
    return chat.id, (chat.title or chat.username or str(chat.id)), ""


async def check_subscription(bot: Bot, chat_ref, user_id: int) -> Optional[bool]:
    """True — подписан, False — нет, None — не удалось проверить."""
    try:
        member = await bot.get_chat_member(chat_id=chat_ref, user_id=user_id)
    except Exception as e:
        print(f"check_subscription error: {e!r}")
        return None
    status = _status_str(member)
    if status in ("creator", "administrator", "member"):
        return True
    if status == "restricted":
        return bool(getattr(member, "is_member", False))
    return False


# ================= ПРОМОКОДЫ (Redis) =================
async def get_promo(code: str) -> Optional[dict]:
    res = await redis_cmd("HGETALL", f"promo:{code}")
    if not res:
        return None
    d = dict(zip(res[0::2], res[1::2])) if isinstance(res, list) else dict(res)
    return {
        "name": d.get("name") or code,
        "price": to_float(d.get("price")),
        "activations": to_int(d.get("activations")),
    }


async def create_promo(name: str, activations: int, price: float) -> None:
    await redis_cmd("DEL", f"promo_used:{name}")
    await redis_cmd("HSET", f"promo:{name}", "name", name, "price", price, "activations", activations)
    await redis_cmd("ZADD", "promos", time.time(), name)


async def delete_promo(code: str) -> None:
    await redis_cmd("DEL", f"promo:{code}")
    await redis_cmd("DEL", f"promo_used:{code}")
    await redis_cmd("ZREM", "promos", code)


# ================= API СПОНСОРОВ =================
async def api_post(session, name, url, payload, headers):
    """Возвращает (http_status, json_или_None, сырое_тело). Всё логирует."""
    try:
        async with session.post(url, json=payload, headers=headers) as resp:
            body = await resp.text()
            print(f"[{name}] HTTP {resp.status}: {body[:500]}")
            try:
                data = json.loads(body)
            except Exception:
                data = None
            return resp.status, data, body
    except Exception as e:
        print(f"[{name}] ОШИБКА ЗАПРОСА: {e!r}")
        return None, None, repr(e)


def tgrass_headers():
    return {"Auth": TGRASS_API_KEY, "Content-Type": "application/json", "accept": "application/json"}


def piarflow_headers():
    return {"Authorization": f"Bearer {PIARFLOW_API_KEY}", "Content-Type": "application/json"}


def trafsly_headers():
    return {"Auth": TRAFSLY_API_KEY, "Content-Type": "application/json"}


async def fetch_tgrass(session, user_id, user_data, limit):
    payload = {
        "tg_user_id": int(user_id),
        "tg_login": user_data.get("username"),
        "lang": user_data.get("language_code") or "ru",
        "is_premium": bool(user_data.get("is_premium", False)),
        "offers_limit": limit,
    }
    _, data, _ = await api_post(session, "tgrass", f"{TGRASS_API_URL}/offers", payload, tgrass_headers())
    if not isinstance(data, dict):
        return []
    if data.get("status") == "ok":
        return []
    offers = data.get("offers", []) or []
    return [o for o in offers if not o.get("subscribed", True)][:limit]


async def fetch_piarflow(session, user_id, chat_id, limit):
    payload = {"user_id": user_id, "chat_id": chat_id, "max_sponsors": limit}
    _, data, _ = await api_post(session, "piarflow", f"{PIARFLOW_API_URL}/sponsors", payload, piarflow_headers())
    if not isinstance(data, dict):
        return []
    sponsors = data.get("sponsors", []) or []
    return [
        {"link": s["link"], "type": "piarflow"}
        for s in sponsors
        if s.get("status") == "unsubscribed" and s.get("link")
    ][:limit]


async def fetch_trafsly(session, user_id, chat_id, user_data, limit):
    payload = {
        "user_id": user_id,
        "chat_id": chat_id,
        "first_name": user_data.get("first_name", ""),
        "username": user_data.get("username", ""),
        "language_code": user_data.get("language_code", "ru"),
        "is_premium": bool(user_data.get("is_premium", False)),
        "max_sponsors": limit,
    }
    _, data, _ = await api_post(session, "trafsly", f"{TRAFSLY_API_URL}/api/v1/get-sponsors", payload, trafsly_headers())
    if not isinstance(data, dict):
        return []
    sponsors = data.get("sponsors", []) or []
    return [
        {"link": s["link"], "ads_id": s.get("ads_id"), "type": "trafsly"}
        for s in sponsors
        if s.get("link")
    ][:limit]


# ================= ДОПОЛНИТЕЛЬНЫЕ СПОНСОРЫ: Traffy и др. =================
# Порядок = приоритет: 1 tgrass, 2 traffy, 3 piarflow, 4 trafsly. Затем наш канал.
EXTERNAL_SERVICES = ("tgrass", "traffy", "piarflow", "trafsly")
SERVICES = EXTERNAL_SERVICES + ("own",)


def links_of(data: dict) -> list[str]:
    """Все ссылки спонсоров в порядке приоритета сервисов."""
    return [item["link"] for name in SERVICES for item in (data.get(name) or [])]


def limit_sponsors(data: dict, limit: int = TARGET_COUNT) -> dict:
    """Жёстко обрезает общий список спонсоров до limit (по приоритету сервисов)."""
    left = limit
    for name in SERVICES:
        items = (data.get(name) or [])[:max(left, 0)]
        data[name] = items
        left -= len(items)
    return data


def fill_test_sponsors(data: dict) -> dict:
    """ТЕСТ-РЕЖИМ (/woot): гарантирует ровно TARGET_COUNT кнопок, даже если человек уже подписан
    на часть спонсоров или сервисы отдали меньше. Наш канал показывается всегда, а недостающие
    места занимают заглушки (ссылка на наш канал, помечены test=True — проверка считает их выполненными)."""
    own = list(data.get("own") or [])
    if not own:
        own = [{"link": OWN_CHANNEL_LINK, "chat_id": OWN_CHANNEL_ID}]
    external = sum(len(data.get(n) or []) for n in EXTERNAL_SERVICES)
    for _ in range(max(TARGET_COUNT - external - len(own), 0)):
        own.append({"link": OWN_CHANNEL_LINK, "test": True})
    data["own"] = own
    return data


def pick_sponsors(results) -> dict[str, list]:
    """Из ответов сервисов (в порядке EXTERNAL_SERVICES) набирает до EXTERNAL_LIMIT спонсоров без повторов."""
    collected: dict[str, list] = {name: [] for name in SERVICES}
    seen: set[str] = set()
    remaining = EXTERNAL_LIMIT
    for name, res in zip(EXTERNAL_SERVICES, results):
        if remaining <= 0:
            break
        if isinstance(res, Exception):
            print(f"[{name}] ошибка получения спонсоров: {res!r}")
            continue
        for item in res or []:
            link = item.get("link")
            if not link or link in seen:
                continue
            seen.add(link)
            collected[name].append(item)
            remaining -= 1
            if remaining <= 0:
                break
    return collected


def traffy_headers():
    return {"x-publisher-api-key": TRAFFY_API_KEY, "Content-Type": "application/json"}


async def fetch_traffy(session, user_id, user_data, limit):
    if not key_filled(TRAFFY_API_KEY):
        return []
    payload = {"telegram_id": user_id, "limit": limit}
    if user_data.get("first_name"):
        payload["first_name"] = user_data["first_name"]
    if user_data.get("username"):
        payload["username"] = user_data["username"]
    if user_data.get("language_code"):
        payload["language_code"] = user_data["language_code"]
    _, data, _ = await api_post(session, "traffy", f"{TRAFFY_API_URL}/tasks", payload, traffy_headers())
    if not isinstance(data, dict):
        return []
    tasks = data.get("tasks") or []
    return [
        {"link": t["target_link"], "assignment_id": t["assignment_id"]}
        for t in tasks
        if t.get("target_link") and t.get("assignment_id")
    ][:limit]


# ----- проверка выполнения (каждая функция возвращает, что осталось невыполненным) -----
async def check_tgrass(session, user, items):
    payload = {
        "tg_user_id": int(user.id),
        "tg_login": user.username,
        "lang": user.language_code or "ru",
        "is_premium": bool(user.is_premium),
    }
    _, res, _ = await api_post(session, "tgrass-check", f"{TGRASS_API_URL}/offers", payload, tgrass_headers())
    if not isinstance(res, dict):
        return items
    if res.get("status") == "ok":
        return []
    offers = res.get("offers", []) or []
    active_links = {o["link"] for o in offers if not o.get("subscribed", True)}
    return [o for o in items if o["link"] in active_links]


async def check_piarflow(session, user, items):
    _, res, _ = await api_post(
        session, "piarflow-check", f"{PIARFLOW_API_URL}/sponsors/check",
        {"user_id": user.id, "links": [i["link"] for i in items]}, piarflow_headers(),
    )
    if not isinstance(res, dict):
        return items
    sponsors = res.get("sponsors", []) or []
    subscribed = {s["link"] for s in sponsors if s.get("status") == "subscribed"}
    return [i for i in items if i["link"] not in subscribed]


async def check_trafsly(session, user, items):
    async def one(sponsor):
        _, res, _ = await api_post(
            session, "trafsly-check", f"{TRAFSLY_API_URL}/api/v1/confirm-subscription",
            {"user_id": user.id, "ads_id": sponsor.get("ads_id")}, trafsly_headers(),
        )
        return sponsor, bool(isinstance(res, dict) and res.get("subscribed", False))

    results = await asyncio.gather(*(one(s) for s in items))
    return [s for s, done in results if not done]


async def check_traffy(session, user, items):
    _, res, _ = await api_post(
        session, "traffy-check", f"{TRAFFY_API_URL}/tasks/check",
        {"telegram_id": user.id, "assignment_ids": [i["assignment_id"] for i in items]}, traffy_headers(),
    )
    if not isinstance(res, dict):
        return items
    status_by_id = {r.get("assignment_id"): r.get("status") for r in (res.get("results") or [])}
    # completed — выполнено; rejected/expired — выполнить уже нельзя, не держим пользователя
    return [i for i in items if status_by_id.get(i["assignment_id"], "pending") in ("pending", "error")]


# ----- наш канал -----
async def fetch_own(bot, user_id):
    """Наш канал показываем, только если пользователь ещё не подписан."""
    status = await check_subscription(bot, OWN_CHANNEL_ID, user_id)
    if status is None:
        print("[own] не удалось проверить подписку — бот должен быть админом канала")
        return []
    if status is False:
        return [{"link": OWN_CHANNEL_LINK, "chat_id": OWN_CHANNEL_ID}]
    return []


async def check_own(session, user, items):
    bot = _current_bot.get()
    if bot is None:
        return items
    left = []
    for item in items:
        if item.get("test"):  # заглушка тест-режима — всегда считается выполненной
            continue
        st = await check_subscription(bot, item.get("chat_id", OWN_CHANNEL_ID), user.id)
        if st is False:
            left.append(item)
    return left


CHECKERS = {
    "own": check_own,
    "tgrass": check_tgrass,
    "piarflow": check_piarflow,
    "trafsly": check_trafsly,
    "traffy": check_traffy,
}


# ================= /start =================
@router.message(CommandStart())
async def start_handler(message: Message, command: CommandObject, state: FSMContext) -> None:
    await state.clear()
    user_id = message.from_user.id
    username_str = f"@{message.from_user.username}" if message.from_user.username else f"id{user_id}"

    data = await ensure_user(message.from_user)
    is_new = data["is_new"]
    await redis_cmd(
        "HSET", user_key(user_id),
        "name", message.from_user.first_name or "", "username", username_str,
    )

    args = command.args
    if args and args.startswith("ref_") and is_new:
        try:
            referrer_id = int(args.split("_")[1])
            if referrer_id != user_id:
                saved = await redis_cmd("SET", f"pending_ref:{user_id}", referrer_id, "NX", "EX", 2592000)
                if saved and await get_user(referrer_id):
                    reward = await get_referral_reward()
                    ref_msg = (
                        f"реферал: {username_str}\n"
                        f"доход: 0.00 ⭐\n"
                        f"как только реферал подпишется на спонсоров, вы получите "
                        f"{reward}⭐"
                    )
                    try:
                        await message.bot.send_message(chat_id=referrer_id, text=ref_msg)
                    except Exception:
                        pass
        except ValueError:
            pass

    await show_sponsors(message)


# ================= /debug (только админ) =================
@router.message(Command("debug"))
async def debug_handler(message: Message, state: FSMContext) -> None:
    if message.from_user.id != ADMIN_ID:
        return
    await state.clear()

    user = message.from_user
    user_id = user.id
    chat_id = message.chat.id

    def key_status(v: Optional[str]) -> str:
        return f"заполнен ({len(v)} симв.)" if key_filled(v) else "НЕ ЗАПОЛНЕН"

    ping = await redis_cmd("PING") if redis_enabled() else None
    if not redis_enabled():
        redis_line = "НЕ НАСТРОЕН — данные НЕ сохраняются!"
    elif ping == "PONG":
        redis_line = "работает ✅"
    else:
        redis_line = "настроен, но НЕ отвечает ❌ (проверьте URL и токен)"

    lines = [
        "🔧 ДИАГНОСТИКА",
        f"BOT_TOKEN: {'задан' if BOT_TOKEN else 'НЕ ЗАДАН'}",
        f"TGRASS_API_KEY: {key_status(TGRASS_API_KEY)}",
        f"PIARFLOW_API_KEY: {key_status(PIARFLOW_API_KEY)}",
        f"TRAFSLY_API_KEY: {key_status(TRAFSLY_API_KEY)}",
        f"TRAFFY_API_KEY: {key_status(TRAFFY_API_KEY)}",
        f"FLYER_API_KEY: {key_status(FLYER_API_KEY)}",
        f"REDIS: {redis_line}",
        "",
    ]

    timeout = aiohttp.ClientTimeout(total=5)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        st, _, body = await api_post(
            session, "tgrass", f"{TGRASS_API_URL}/offers",
            {
                "tg_user_id": int(user_id),
                "tg_login": user.username,
                "lang": user.language_code or "ru",
                "is_premium": bool(user.is_premium),
                "offers_limit": 3,
            },
            tgrass_headers(),
        )
        lines.append(f"TGRASS → HTTP {st}\n{body[:500]}\n")

        st, _, body = await api_post(
            session, "piarflow", f"{PIARFLOW_API_URL}/sponsors",
            {"user_id": user_id, "chat_id": chat_id, "max_sponsors": 3},
            piarflow_headers(),
        )
        lines.append(f"PIARFLOW → HTTP {st}\n{body[:500]}\n")

        st, _, body = await api_post(
            session, "trafsly", f"{TRAFSLY_API_URL}/api/v1/get-sponsors",
            {
                "user_id": user_id,
                "chat_id": chat_id,
                "first_name": user.first_name or "",
                "username": user.username or "",
                "language_code": user.language_code or "ru",
                "is_premium": bool(user.is_premium),
                "max_sponsors": 3,
            },
            trafsly_headers(),
        )
        lines.append(f"TRAFSLY → HTTP {st}\n{body[:500]}\n")

        traffy_payload = {"telegram_id": user_id, "limit": 3}
        if user.language_code:
            traffy_payload["language_code"] = user.language_code
        st, _, body = await api_post(
            session, "traffy", f"{TRAFFY_API_URL}/tasks", traffy_payload, traffy_headers(),
        )
        lines.append(f"TRAFFY → HTTP {st}\n{body[:500]}\n")

        # Flyer: тип ключа (для заданий нужен «tasks») и пробный запрос заданий
        me_data = await flyer_post("get_me", {})
        lines.append(f"FLYER get_me → {me_data}\n")
        fl_payload = {"user_id": user_id, "limit": 3}
        if user.language_code:
            fl_payload["language_code"] = user.language_code
        lines.append(f"FLYER get_tasks → {await flyer_post('get_tasks', fl_payload)}\n")


    try:
        me_bot = await message.bot.me()
        member = await message.bot.get_chat_member(OWN_CHANNEL_ID, me_bot.id)
        st_own = _status_str(member)
        ok = st_own in ("administrator", "creator")
        lines.append(f"НАШ КАНАЛ: бот — {st_own} " + ("✅" if ok else "❌ сделайте бота администратором"))
    except Exception as e:
        lines.append(f"НАШ КАНАЛ: нет доступа ❌ {e!r}")

    text = "\n".join(lines)
    for i in range(0, len(text), 3900):
        await message.answer(text[i:i + 3900])


# ================= /resetme (только админ, для тестов) =================
@router.message(Command("resetme"))
async def resetme_handler(message: Message, state: FSMContext) -> None:
    if message.from_user.id != ADMIN_ID:
        return
    await state.clear()
    uid = message.from_user.id
    await redis_cmd("SREM", "income_users", uid)
    await redis_cmd("SREM", "passed_users", uid)
    await redis_cmd("DEL", f"sp_total:{uid}")
    await redis_cmd("DEL", f"sp_active:{uid}")
    await redis_cmd("DEL", f"flyer_done:{uid}", f"flyer_skip:{uid}", f"flyer_cur:{uid}", f"flyer_dead:{uid}")
    await redis_cmd("DEL", f"pending_ref:{uid}")
    await redis_cmd("DEL", f"verify:{uid}")
    await redis_cmd("DEL", f"verify_pending:{uid}")
    await message.answer(
        "✅ Ваш аккаунт сброшен для теста. Нажмите /start — снова пройдёте спонсоров и проверку номера."
    )


# ================= /woot (только админ): полный сброс аккаунта =================
@router.message(Command("woot"))
async def woot_handler(message: Message, state: FSMContext) -> None:
    if message.from_user.id not in WOOT_IDS:
        return
    await state.clear()
    uid = message.from_user.id

    invited = await redis_cmd("SMEMBERS", f"refs:{uid}") or []
    promos = await redis_cmd("ZRANGE", "promos", 0, -1) or []

    cmds: list = []
    # у приглашённых этим аккаунтом убираем привязку, чтобы штрафы не шли на сброшенный аккаунт
    for rid in invited:
        cmds.append(["DEL", f"refby:{rid}"])
    # можно снова активировать промокоды
    for code in promos:
        cmds.append(["SREM", f"promo_used:{code}", uid])
    cmds.append([
        "DEL", user_key(uid), f"refs:{uid}", f"refby:{uid}", f"pending_ref:{uid}",
        f"sp_total:{uid}", f"sp_active:{uid}", f"watch:{uid}", f"penalized:{uid}",
        f"user_done:{uid}", f"user_skip:{uid}", f"wd:{uid}", f"verify_pending:{uid}",
        f"flyer_done:{uid}", f"flyer_skip:{uid}", f"flyer_cur:{uid}", f"flyer_dead:{uid}",
    ])
    cmds += [
        ["SREM", "users", uid],
        ["ZREM", "users_joined", uid],
        ["ZREM", "top_refs", uid],
        ["HDEL", "top_names", uid],
        ["SREM", "income_users", uid],
        ["SREM", "passed_users", uid],
        ["ZREM", "watch_list", uid],
    ]
    await redis_pipeline(cmds)
    await redis_cmd("DEL", f"verify:{uid}")
    # тест-режим: после /woot бот всегда выдаёт 6 спонсоров (см. fill_test_sponsors)
    await redis_cmd("SET", f"woot_test:{uid}", "1")

    await message.answer(
        "✅ Аккаунт полностью сброшен: баланс, рефералы, спонсоры, штрафы.\n"
        "🧪 Тест-режим: вам всегда выдаётся 6 спонсоров, даже если вы уже подписаны на часть из них.\n\n"
        "Теперь вы как новый пользователь: нажмите /start (спонсоры запросятся заново) "
        "или перейдите по реферальной ссылке — реферал засчитается."
    )


# ================= ЭТАП СПОНСОРОВ ЗАВЕРШЁН =================
async def after_sponsors(bot: Bot, target, user, had_sponsors: bool, sponsors_count: int = 0) -> None:
    """Этап спонсоров пройден (проверки номера больше нет): награда рефералу и меню."""
    uid = user.id
    # если человек застрял на старой проверке номера — берём исходное «были ли спонсоры»
    pending = await redis_cmd("GETDEL", f"verify_pending:{uid}")
    if pending is not None:
        had_sponsors = pending == "1"
        if not had_sponsors:
            sponsors_count = 0
    if had_sponsors:
        await send_sponsors_done(target)
    await reward_referrer_if_needed(bot, uid, had_sponsors, sponsors_count)
    await send_main_menu(target, user)


class AccessMiddleware(BaseMiddleware):
    """Проверки номера больше нет: только перепроверка подписки (штрафы) при заходе в бота."""

    async def __call__(self, handler, event, data):
        user = getattr(event, "from_user", None)
        if user is not None:
            await maybe_check_watch(event)
        return await handler(event, data)


router.message.outer_middleware(AccessMiddleware())
router.callback_query.outer_middleware(AccessMiddleware())


# ================= ВВОД ТЕКСТА (по состояниям) =================
@router.message()
async def any_message_handler(message: Message, state: FSMContext) -> None:
    current_state = await state.get_state()
    user_id = message.from_user.id
    text_msg = message.text.strip() if message.text else ""
    await ensure_user(message.from_user)

    # ----- вывод: своя сумма -----
    if current_state == WithdrawStates.waiting_for_custom_amount.state:
        if not text_msg.isdigit():
            await message.answer("❌ Пожалуйста, введите корректное целое число.")
            return
        amount = int(text_msg)
        if amount < 50:
            await message.answer("❌ Минимальная сумма для вывода: 50 ⭐. Введите число от 50:")
            return

        await set_wd(user_id, {"amount": amount})
        await state.set_state(WithdrawStates.waiting_for_username)
        await message.answer("Введите @username получателя:", reply_markup=back_markup("menu_withdraw"))
        return

    # ----- вывод: username -----
    if current_state == WithdrawStates.waiting_for_username.state:
        username = text_msg
        if not username.startswith("@"):
            username = "@" + username

        wd = await get_wd(user_id)
        wd["username"] = username
        await set_wd(user_id, wd)

        amount = wd.get("amount", 0)
        await state.set_state(WithdrawStates.waiting_for_confirmation)

        text = (
            f"Получатель: {username}\n"
            f"Количество: {amount} ⭐\n\n"
            f"Если данные верны нажми да 👇"
        )
        markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="Да", callback_data="confirm_withdraw"),
                    InlineKeyboardButton(text="Назад", callback_data="menu_withdraw"),
                ]
            ]
        )
        await message.answer(text, reply_markup=markup)
        return

    # ----- активация промокода -----
    if current_state == PromocodeStates.waiting_for_activation_input.state:
        code = text_msg
        markup = back_markup("back_to_main")
        invalid_text = "промокод использован или недействителен"

        promo = await get_promo(code) if code else None
        if not promo:
            await message.answer(invalid_text, reply_markup=markup)
            return

        # SADD вернёт 0, если этот пользователь уже активировал промокод
        first_use = await redis_cmd("SADD", f"promo_used:{code}", user_id)
        if to_int(first_use) == 0:
            await message.answer("промокод уже был вами использован ❗", reply_markup=markup)
            return

        left = await redis_cmd("HINCRBY", f"promo:{code}", "activations", -1)
        if left is None or to_int(left) < 0:
            # активации закончились — откатываем
            await redis_cmd("HINCRBY", f"promo:{code}", "activations", 1)
            await redis_cmd("SREM", f"promo_used:{code}", user_id)
            await message.answer(invalid_text, reply_markup=markup)
            return

        await add_balance(user_id, promo["price"])
        if to_int(left) <= 0:
            await delete_promo(code)

        await state.clear()
        await message.answer(f"на ваш баланс зачислено {promo['price']} ⭐", reply_markup=markup)
        return

    # ===== ниже только для админа =====
    if user_id != ADMIN_ID:
        return

    # ----- рассылка -----
    if current_state == AdminBroadcastStates.waiting_for_text.state:
        await state.update_data(broadcast_text=text_msg)
        await state.set_state(AdminBroadcastStates.confirm)

        count_users = to_int(await redis_cmd("SCARD", "users"))
        text_out = f"пользователей увидят рассылку: {count_users}\n\nтекст рассылки:\n\n{text_msg}"
        markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="да, отправить✅", callback_data="admin_bc_send"),
                    InlineKeyboardButton(text="изменить", callback_data="admin_bc_edit"),
                ],
                [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_panel")],
            ]
        )
        await message.answer(text_out, reply_markup=markup)
        return

    # ----- создание задания -----
    if current_state == AdminTaskStates.text.state:
        await state.update_data(task_text=text_msg)
        await state.set_state(AdminTaskStates.link)
        await message.answer("введите ссылку на канал:", reply_markup=back_markup("admin_panel"))
        return

    if current_state == AdminTaskStates.link.state:
        link = normalize_task_link(text_msg)
        if not link:
            await message.answer(
                "❌ Введите ссылку вида https://t.me/channel или @channel",
                reply_markup=back_markup("admin_panel"),
            )
            return

        chat_ref = chat_ref_from_link(link)
        if chat_ref is None:
            # закрытая ссылка-приглашение: по ней канал не определить, нужен его ID
            await state.update_data(task_link=link)
            await state.set_state(AdminTaskStates.chat_id)
            await message.answer(
                "🔒 Это закрытая ссылка. Чтобы проверять подписки, пришлите ID канала "
                "(выглядит как -1001234567890).\n\n"
                "ID можно узнать, переслав любой пост из канала боту @getidsbot. "
                "Бот должен быть администратором канала.",
                reply_markup=back_markup("admin_panel"),
            )
            return

        chat_id, title, err = await validate_task_chat(message.bot, chat_ref)
        if err:
            await message.answer(err, reply_markup=back_markup("admin_panel"))
            return

        await state.update_data(task_link=link, task_chat=chat_id, task_chat_title=title)
        await state.set_state(AdminTaskStates.reward)
        await message.answer("введите награду (⭐) за задание:", reply_markup=back_markup("admin_panel"))
        return

    if current_state == AdminTaskStates.chat_id.state:
        if not re.fullmatch(r"-100\d{5,}", text_msg):
            await message.answer(
                "❌ ID должен выглядеть как -1001234567890. Попробуйте ещё раз.",
                reply_markup=back_markup("admin_panel"),
            )
            return
        chat_id, title, err = await validate_task_chat(message.bot, int(text_msg))
        if err:
            await message.answer(err, reply_markup=back_markup("admin_panel"))
            return
        await state.update_data(task_chat=chat_id, task_chat_title=title)
        await state.set_state(AdminTaskStates.reward)
        await message.answer("введите награду (⭐) за задание:", reply_markup=back_markup("admin_panel"))
        return

    if current_state == AdminTaskStates.reward.state:
        try:
            reward = float(text_msg.replace(",", "."))
        except ValueError:
            await message.answer("❌ Введите число.")
            return
        await state.update_data(task_reward=reward)
        await state.set_state(AdminTaskStates.max_completions)
        await message.answer("введите максимальное количество выполнений:", reply_markup=back_markup("admin_panel"))
        return

    if current_state == AdminTaskStates.max_completions.state:
        if not text_msg.isdigit() or int(text_msg) <= 0:
            await message.answer("❌ Введите целое число больше нуля.")
            return
        await state.update_data(task_max_completions=int(text_msg))
        data = await state.get_data()
        await state.set_state(AdminTaskStates.preview)

        text = (
            "предпросмотр задания:\n\n"
            f"текст: {data.get('task_text')}\n"
            f"ссылка: {data.get('task_link')}\n"
            f"канал: {data.get('task_chat_title')}\n"
            f"награда: {data.get('task_reward')}⭐\n"
            f"макс. выполнений: {data.get('task_max_completions')}"
        )
        markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="✅ Опубликовать", callback_data="admin_task_pub")],
                [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_panel")],
            ]
        )
        await message.answer(text, reply_markup=markup, **no_preview())
        return

    # ----- создание промокода -----
    if current_state == PromocodeStates.admin_name.state:
        await state.update_data(promo_name=text_msg)
        await state.set_state(PromocodeStates.admin_activations)
        await message.answer("введите количество активаций:", reply_markup=back_markup("admin_panel", "Назад"))
        return

    if current_state == PromocodeStates.admin_activations.state:
        if not text_msg.isdigit() or int(text_msg) <= 0:
            await message.answer("❌ Введите целое число больше нуля.")
            return
        await state.update_data(promo_activations=int(text_msg))
        await state.set_state(PromocodeStates.admin_price)
        await message.answer("введите цену промокода (⭐):", reply_markup=back_markup("admin_panel", "Назад"))
        return

    if current_state == PromocodeStates.admin_price.state:
        try:
            price = float(text_msg.replace(",", "."))
        except ValueError:
            await message.answer("❌ Введите число.")
            return
        data = await state.get_data()
        name = data.get("promo_name")
        await create_promo(name, data.get("promo_activations", 1), price)
        await state.clear()
        await message.answer(
            f"✅ Промокод {name} создан",
            reply_markup=back_markup("admin_promo_menu", "Назад"),
        )
        return

    # ----- награда за реферала -----
    if current_state == AdminRewardStates.waiting_for_new_value.state:
        try:
            new_val = float(text_msg.replace(",", "."))
        except ValueError:
            await message.answer("❌ Введите число.")
            return
        await state.update_data(new_reward=new_val)
        await state.set_state(AdminRewardStates.confirm)
        markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="применить ✅", callback_data="admin_reward_apply"),
                    InlineKeyboardButton(text="назад", callback_data="admin_panel"),
                ]
            ]
        )
        await message.answer(f"новая цена за реферала: {new_val} ⭐", reply_markup=markup)
        return


# ================= СПОНСОРЫ =================
def build_sponsor_markup(all_links: list[str]) -> InlineKeyboardMarkup:
    buttons = []
    temp_row = []
    for link in all_links[:TARGET_COUNT]:
        temp_row.append(InlineKeyboardButton(text="✉️ Подписаться", url=link))
        if len(temp_row) == 2:
            buttons.append(temp_row)
            temp_row = []
    if temp_row:
        buttons.append(temp_row)
    buttons.append([InlineKeyboardButton(text="Проверить подписку", callback_data="check_all_tasks")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def reward_referrer_if_needed(
    bot: Bot, user_id: int, had_sponsors: bool = True, sponsors_count: int = 0
) -> None:
    """Сообщает пригласившему о результате.
    Награда выдаётся, только если реферал выполнил не меньше MIN_SPONSORS_FOR_REWARD спонсоров
    (1 наш + остальные из других сервисов). Иначе — награды нет и приходит сообщение о подозрении."""
    # GETDEL: забираем и удаляем за одну операцию — награда выдаётся только один раз
    raw = await redis_cmd("GETDEL", f"pending_ref:{user_id}")
    if raw is None:
        return
    try:
        ref_id = int(raw)
    except (TypeError, ValueError):
        return

    ref = await ensure_user_by_id(ref_id)
    me = await get_user(user_id)
    ref_username = me["username"] if me and me["username"] else f"id{user_id}"

    # ----- спонсоров меньше нужного минимума -> награды нет -----
    if not had_sponsors or sponsors_count < MIN_SPONSORS_FOR_REWARD:
        no_sponsors_msg = (
            "🚫спонсоры заподозрили что реферал не выполнил требуемые\n"
            " условия, выплаты за реферала не начислена \n"
            "доход 0.00 ⭐ \n"
            f"баланс {ref['balance']:.2f} ⭐"
        )
        try:
            await bot.send_message(chat_id=ref_id, text=no_sponsors_msg)
        except Exception:
            pass
        return

    # ----- реферал подписался на спонсоров -> награда -----
    reward = await get_referral_reward()
    await add_referral_to_top(ref_id, ref["name"] or f"id{ref_id}")
    await redis_cmd("SADD", f"refs:{ref_id}", user_id)
    await redis_cmd("SET", f"refby:{user_id}", f"{ref_id}:{reward}")
    await redis_cmd("HINCRBY", user_key(ref_id), "referrals", 1)
    total_bal = await add_balance(ref_id, reward)
    if total_bal is None:
        total_bal = ref["balance"] + reward

    success_ref_msg = (
        f"реферал: {ref_username}\n"
        f"подписался на спонсоров ваш доход {reward} ⭐\n"
        f"доход: {total_bal:.1f} ⭐"
    )
    try:
        await bot.send_message(chat_id=ref_id, text=success_ref_msg)
    except Exception:
        pass


async def show_sponsors(message: Message, user=None):
    user = user or message.from_user
    user_id = user.id
    chat_id = message.chat.id
    user_data = {
        "first_name": user.first_name,
        "username": user.username,
        "language_code": user.language_code,
        "is_premium": user.is_premium,
    }

    collected: dict[str, list] = {name: [] for name in SERVICES}

    try:
        timeout = aiohttp.ClientTimeout(total=5)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            # Все сервисы опрашиваются одновременно (быстрее для Vercel),
            # затем из ответов набирается до EXTERNAL_LIMIT спонсоров по приоритету
            results = await asyncio.gather(
                fetch_tgrass(session, user_id, user_data, EXTERNAL_LIMIT),
                fetch_traffy(session, user_id, user_data, EXTERNAL_LIMIT),
                fetch_piarflow(session, user_id, chat_id, EXTERNAL_LIMIT),
                fetch_trafsly(session, user_id, chat_id, user_data, EXTERNAL_LIMIT),
                return_exceptions=True,
            )
        collected = pick_sponsors(results)           # до 5 спонсоров по приоритету
        collected["own"] = await fetch_own(message.bot, user_id)  # + наш канал
        collected = limit_sponsors(collected)
    except Exception as e:
        print(f"show_sponsors error: {e!r}")

    # тест-режим после /woot: всегда ровно 6 спонсоров
    if await redis_cmd("GET", f"woot_test:{user_id}"):
        collected = limit_sponsors(fill_test_sponsors(collected))

    print(f"[sponsors] user={user_id} " + " ".join(f"{n}={len(collected[n])}" for n in SERVICES))

    all_links = links_of(collected)

    if all_links:
        await set_active(user_id, {
            "v": 2,
            **collected,
            "total_count": len(all_links),
            "all": {n: list(collected[n]) for n in SERVICES},
        })
        # NX: не перезаписываем, чтобы сохранилось исходное число спонсоров
        await redis_cmd("SET", f"sp_total:{user_id}", len(all_links), "NX", "EX", 604800)
    else:
        await del_active(user_id)
        # Спонсоров сейчас нет. Если раньше они были показаны (число хранится в Redis) —
        # значит, пользователь на них уже подписался.
        done_count = await pop_sponsor_total(user_id)
        await add_income(user_id, done_count)
        await record_sponsors(user_id, done_count)
        if await mark_passed(user_id):
            await start_watch(user_id, {})
        await after_sponsors(message.bot, message, user, had_sponsors=done_count > 0, sponsors_count=done_count)
        return

    markup = build_sponsor_markup(all_links)
    text = (
        "🤖 Для использования бота подпишись на каналы ниже\n\n"
        "🔻 После подписки нажимай на кнопку \"Проверить подписку\""
    )
    # Без картинки — только текст и кнопки
    await message.answer(text, reply_markup=markup)


# ================= ГЛАВНОЕ МЕНЮ =================
async def send_main_menu(message_or_callback, user=None):
    if isinstance(message_or_callback, CallbackQuery):
        message = message_or_callback.message
        user = message_or_callback.from_user
    else:
        message = message_or_callback
        user = user or message.from_user

    stats = await ensure_user(user)

    text = (
        f"🥑 Добро пожаловать в AvoStars!\n\n"
        f"Привет, {user.first_name}! Зарабатывай Звёздочки за подписки.\n\n"
        f"⭐ Баланс: {stats['balance']:.2f} звёздочек\n"
        f"👥 Рефералов: {stats['referrals']}\n\n"
        f"Выбери раздел:"
    )

    rows = [
        [
            InlineKeyboardButton(text="⭐ Задание", callback_data="menu_task"),
            InlineKeyboardButton(text="👤 Профиль", callback_data="menu_profile"),
        ],
        [
            InlineKeyboardButton(text="👥 Рефералы", callback_data="menu_referrals"),
            InlineKeyboardButton(text="🏆 Топ", callback_data="menu_top"),
        ],
        [
            InlineKeyboardButton(text="📥 Вывести звёзды", callback_data="menu_withdraw"),
            InlineKeyboardButton(text="🎁 Промокод", callback_data="menu_promocode"),
        ],
    ]
    if user.id == ADMIN_ID:
        rows.append([InlineKeyboardButton(text="👑 Админ панель", callback_data="admin_panel")])

    markup = InlineKeyboardMarkup(inline_keyboard=rows)

    try:
        try:
            await message.answer_photo(photo=PHOTO_URL, caption=text, reply_markup=markup)
        except Exception:
            await message.answer(text, reply_markup=markup)
        if isinstance(message_or_callback, CallbackQuery):
            try:
                await message_or_callback.message.delete()
            except Exception:
                pass
    except Exception as ex:
        print(f"Критическая ошибка главного меню: {ex}")


# ================= ЗАДАНИЯ =================
NO_TASKS_TEXT = "Сейчас нет доступных заданий"


async def show_task(callback: CallbackQuery, repeat: bool = True) -> bool:
    """Источники по порядку: задания админа -> Flyer.
    Сначала ищем непропущенное задание в любом источнике (так «пропустить» реально переходит к другому),
    и только если всё пропущено — показываем пропущенные заново."""
    uid = callback.from_user.id
    for reset in ((False, True) if repeat else (False,)):
        try:
            task = await pick_task(uid, reset)
            if task and await send_admin_task(callback, task):
                return True
        except Exception as e:
            print(f"[tasks] admin source error: {e!r}")
        try:
            if await show_flyer_task(callback, reset):
                return True
        except Exception as e:
            print(f"[tasks] flyer source error: {e!r}")
    await replace_message(callback, NO_TASKS_TEXT, back_markup("back_to_main"))
    return False


async def send_admin_task(callback: CallbackQuery, task: dict) -> bool:
    task_id = task["id"]
    link = html.escape(str(task["link"]))
    reward = html.escape(str(task["reward"]))
    number = await task_number(callback.from_user.id)
    text = (
        f"<b>🎯 Задание №{number}</b>\n\n"
        f"<b>• Подпишитесь на канал —> {link}</b>\n"
        f"<b>• Награда: {reward}⭐️</b>"
    )
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Выполнить", url=task["link"])],
            [InlineKeyboardButton(text="пропустить задание", callback_data=f"skip_task_{task_id}")],
            [InlineKeyboardButton(text="✅ Я выполнил", callback_data=f"done_task_{task_id}")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_main")],
        ]
    )
    return await replace_message(callback, text, markup, parse_mode="HTML", disable_preview=True)


@router.callback_query(F.data == "menu_task")
async def task_menu_handler(callback: CallbackQuery, state: FSMContext) -> None:
    await safe_answer(callback)
    await state.clear()
    await ensure_user(callback.from_user)
    await show_task(callback)


@router.callback_query(F.data.startswith("done_task_"))
async def done_task_handler(callback: CallbackQuery) -> None:
    task_id = int(callback.data.split("_")[2])
    user_id = callback.from_user.id
    await ensure_user(callback.from_user)

    tasks = await load_tasks()
    task = next((t for t in tasks if t["id"] == task_id), None)
    if not task:
        await safe_answer(callback, "Задание не найдено.")
        return

    # Реальная проверка подписки через get_chat_member
    chat_ref = task.get("chat_id") or chat_ref_from_link(task.get("link", ""))
    if chat_ref is None:
        await safe_answer(
            callback,
            "⚠️ Это задание настроено неверно, проверить подписку нельзя. Сообщите администратору.",
            show_alert=True,
        )
        return

    subscribed = await check_subscription(callback.bot, chat_ref, user_id)
    if subscribed is None:
        await safe_answer(
            callback,
            "⚠️ Не удалось проверить подписку. Попробуйте чуть позже.",
            show_alert=True,
        )
        return
    if not subscribed:
        await safe_answer(callback, "вы не подписались на канал ❗", show_alert=True)
        return

    added = await redis_cmd("SADD", f"user_done:{user_id}", task_id)
    if to_int(added) == 0:
        await safe_answer(callback, "Вы уже выполнили это задание!")
        return

    await add_balance(user_id, to_float(task["reward"]))

    count = to_int(await redis_cmd("INCR", f"task_cnt:{task_id}"))
    if count >= to_int(task.get("max_completions"), 1):
        fresh = await load_tasks()
        await save_tasks([t for t in fresh if t["id"] != task_id])
        await redis_cmd("DEL", f"task_cnt:{task_id}")

    await safe_answer(callback)

    text = f"✅ На ваш баланс было зачислено {task['reward']}⭐"
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="следующее задание", callback_data="menu_task")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_main")],
        ]
    )
    await replace_message(callback, text, markup)


@router.callback_query(F.data.startswith("skip_task_"))
async def skip_task_handler(callback: CallbackQuery) -> None:
    task_id = int(callback.data.split("_")[2])
    user_id = callback.from_user.id
    # пропуск действует только для этого пользователя
    await redis_cmd("SADD", f"user_skip:{user_id}", task_id)
    await redis_cmd("EXPIRE", f"user_skip:{user_id}", 86400)
    # если других заданий нет, show_task сам заменит сообщение на «Сейчас нет доступных заданий»
    await show_task(callback, repeat=False)
    await safe_answer(callback, "⏭ Задание пропущено")


# ================= НОМЕР ЗАДАНИЯ =================
async def task_number(uid: int) -> int:
    """Порядковый номер задания для пользователя: сколько он уже выполнил + 1.
    Считаются задания админа и Flyer, поэтому нумерация идёт подряд (№1, №2, №3...),
    а новое задание после выполненных получает следующий номер."""
    done = 0
    for key in (f"user_done:{uid}", f"flyer_done:{uid}"):
        done += to_int(await redis_cmd("SCARD", key))
    return done + 1


# ================= ЗАДАНИЯ FLYER =================
# API: POST /get_tasks (список заданий пользователя) и POST /check_task (статус по signature).
# Ключ должен быть типа «tasks» (см. POST /get_me), иначе будет ошибка "Prohibited method for a bot type".
FLYER_TASK_TEXT = {
    "start bot": "Запустите бота",
    "subscribe channel": "Подпишитесь на канал",
    "give boost": "Отдайте буст каналу",
    "follow link": "Перейдите по ссылке",
    "perform action": "Выполните действие",
    "view posts": "Посмотрите посты",
}


async def flyer_post(method: str, payload: dict) -> Optional[dict]:
    """POST к Flyer API. Возвращает JSON-словарь или None при сбое."""
    body = {"key": FLYER_API_KEY, **payload}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=6)) as session:
        _, data, _ = await api_post(
            session, f"flyer-{method}", f"{FLYER_API_URL}/{method}", body,
            {"Content-Type": "application/json"},
        )
    return data if isinstance(data, dict) else None


async def pick_flyer_task(user, reset: bool = True) -> Optional[dict]:
    """Первое задание Flyer, которое пользователь ещё не выполнял и не пропускал."""
    if not key_filled(FLYER_API_KEY):
        return None
    payload = {"user_id": user.id, "limit": FLYER_TASKS_LIMIT}
    if user.language_code:
        payload["language_code"] = user.language_code
    data = await flyer_post("get_tasks", payload)
    if not data:
        return None
    if data.get("error"):
        print(f"[flyer] get_tasks error: {data.get('error')}")
        return None
    tasks = data.get("result")
    if not isinstance(tasks, list):
        return None

    done = {str(x) for x in (await redis_cmd("SMEMBERS", f"flyer_done:{user.id}") or [])}
    done |= {str(x) for x in (await redis_cmd("SMEMBERS", f"flyer_dead:{user.id}") or [])}
    candidates = [
        t for t in tasks
        if isinstance(t, dict)
        and t.get("signature")
        and t.get("links")
        and t.get("status") not in ("unavailable", "abort")
        and str(t["signature"]) not in done
    ]
    if not candidates:
        return None
    skipped = {str(x) for x in (await redis_cmd("SMEMBERS", f"flyer_skip:{user.id}") or [])}
    fresh = [t for t in candidates if str(t["signature"]) not in skipped]
    if not fresh:
        if not reset:
            return None
        await redis_cmd("DEL", f"flyer_skip:{user.id}")
        fresh = candidates
    # Показываем то, что отдал Flyer. Если при проверке окажется unavailable —
    # задание помечается «мёртвым» (flyer_dead) и больше не показывается.
    return fresh[0]


def flyer_button_url(raw) -> Optional[str]:
    """Приводит ссылку Flyer к виду, который принимает Telegram для url-кнопки."""
    link = str(raw or "").strip()
    if link.startswith(("https://", "http://", "tg://")):
        return link
    if re.match(r"^(?:t|telegram)\.me/", link, re.I):
        return "https://" + link
    return None


async def show_flyer_task(callback: CallbackQuery, reset: bool = True) -> bool:
    """Показывает следующее задание Flyer. False — заданий нет."""
    user = callback.from_user
    task = await pick_flyer_task(user, reset)
    if not task:
        return False
    link = flyer_button_url(task["links"][0])
    if not link:
        print(f"[flyer] неподходящая ссылка у задания {task.get('signature')}: {task['links'][0]!r}")
        await redis_cmd("SADD", f"flyer_skip:{user.id}", str(task["signature"]))
        return False
    # signature не влезает в callback_data надёжно — храним текущее задание в Redis
    await redis_cmd("SET", f"flyer_cur:{user.id}", str(task["signature"]), "EX", 86400)
    action = FLYER_TASK_TEXT.get(task.get("task"), "Выполните задание")
    number = await task_number(user.id)
    text = (
        f"<b>🎯 Задание №{number}</b>\n\n"
        f"<b>• {action}</b>\n"
        f"<b>• Награда: {FLYER_REWARD:g}⭐️</b>"
    )
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Выполнить", url=link)],
            [InlineKeyboardButton(text="пропустить задание", callback_data="fl_skip")],
            [InlineKeyboardButton(text="✅ Я выполнил", callback_data="fl_done")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_main")],
        ]
    )
    if not await replace_message(callback, text, markup, parse_mode="HTML", disable_preview=True):
        # Telegram не принял сообщение — не зацикливаемся на нём, переходим к следующему
        await redis_cmd("SADD", f"flyer_skip:{user.id}", str(task["signature"]))
        return False
    return True


@router.callback_query(F.data == "fl_done")
async def flyer_done_handler(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id
    await ensure_user(callback.from_user)
    signature = await redis_cmd("GET", f"flyer_cur:{user_id}")
    if not signature:
        await safe_answer(callback, "Задание устарело. Откройте раздел заданий заново.", show_alert=True)
        return
    signature = str(signature)

    if to_int(await redis_cmd("SISMEMBER", f"flyer_done:{user_id}", signature)):
        await safe_answer(callback, "Вы уже выполнили это задание!")
        return

    data = await flyer_post("check_task", {"signature": signature})
    status = data.get("result") if data and not data.get("error") else None
    if status is None:
        await safe_answer(callback, "⚠️ Не удалось проверить задание. Попробуйте чуть позже.", show_alert=True)
        return
    if status == "unavailable":
        await redis_cmd("SADD", f"flyer_dead:{user_id}", signature)
        await redis_cmd("EXPIRE", f"flyer_dead:{user_id}", 21600)
        await safe_answer(callback, "Это задание больше не актуально, показываю другое.", show_alert=True)
        await show_task(callback)
        return
    # complete — выполнено и оплачено, waiting — выполнено, оплата Flyer придёт в течение 24 ч
    if status not in ("complete", "waiting"):
        await safe_answer(callback, "вы не выполнили задание ❗", show_alert=True)
        return

    # SADD атомарно: награда выдаётся только один раз за задание
    if to_int(await redis_cmd("SADD", f"flyer_done:{user_id}", signature)) == 0:
        await safe_answer(callback, "Вы уже выполнили это задание!")
        return
    await redis_cmd("DEL", f"flyer_cur:{user_id}")
    await add_balance(user_id, FLYER_REWARD)

    await safe_answer(callback)
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="следующее задание", callback_data="menu_task")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_main")],
        ]
    )
    await replace_message(callback, f"✅ На ваш баланс было зачислено {FLYER_REWARD:g}⭐", markup)


@router.callback_query(F.data == "fl_skip")
async def flyer_skip_handler(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id
    signature = await redis_cmd("GET", f"flyer_cur:{user_id}")
    if signature:
        await redis_cmd("SADD", f"flyer_skip:{user_id}", str(signature))
        await redis_cmd("EXPIRE", f"flyer_skip:{user_id}", 86400)
    # если других заданий нет, show_task сам заменит сообщение на «Сейчас нет доступных заданий»
    await show_task(callback, repeat=False)
    await safe_answer(callback, "⏭ Задание пропущено")


@router.message(Command("taskdiag"))
async def taskdiag_handler(message: Message) -> None:
    """Для админов: откуда берутся задания и что у вас выполнено/пропущено."""
    if message.from_user.id not in WOOT_IDS:
        return
    uid = message.from_user.id
    tasks = await load_tasks()
    lines = ["🔧 ЗАДАНИЯ", ""]
    lines.append(f"АДМИН: всего {len(tasks)}, выполнено {to_int(await redis_cmd('SCARD', f'user_done:{uid}'))}, "
                 f"пропущено {to_int(await redis_cmd('SCARD', f'user_skip:{uid}'))}")
    lines.append(f"FLYER: выполнено {to_int(await redis_cmd('SCARD', f'flyer_done:{uid}'))}, "
                 f"пропущено {to_int(await redis_cmd('SCARD', f'flyer_skip:{uid}'))}")
    payload = {"user_id": uid, "limit": FLYER_TASKS_LIMIT}
    if message.from_user.language_code:
        payload["language_code"] = message.from_user.language_code
    data = await flyer_post("get_tasks", payload)
    if not data:
        lines.append("FLYER get_tasks: нет ответа")
    elif data.get("error"):
        lines.append(f"FLYER get_tasks ошибка: {data.get('error')}")
    else:
        for t in (data.get("result") or []):
            if isinstance(t, dict):
                chk = await flyer_post("check_task", {"signature": t.get("signature")})
                lines.append(
                    f"FLYER: {t.get('task')} | get_tasks={t.get('status')} | check_task={(chk or {}).get('result') or (chk or {}).get('error')} "
                    f"| {t.get('links')}"
                )
        if not data.get("result"):
            lines.append("FLYER get_tasks: заданий для вас нет")
    nxt = await pick_task(uid, False)
    lines.append(f"Следующее админское: {nxt['id'] if nxt else 'нет'}")
    await message.answer("\n".join(lines)[:3900], **no_preview())


# ================= ПРОФИЛЬ =================
@router.callback_query(F.data == "menu_profile")
async def profile_handler(callback: CallbackQuery, state: FSMContext) -> None:
    await safe_answer(callback)
    await state.clear()

    user = callback.from_user
    stats = await ensure_user(user)

    text = (
        f"👤 Имя: {user.first_name}\n"
        f"🆔 ID: {user.id}\n"
        f"💰 Баланс: {stats['balance']:.2f}⭐️\n"
        f"👥 Приглашено друзей: {stats['referrals']}"
    )
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🆘 Поддержка", url="https://t.me/durovsor")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_main")],
        ]
    )
    await replace_with_photo(callback, PROFILE_PHOTO_URL, text, markup)


# ================= РЕФЕРАЛЫ =================
@router.callback_query(F.data == "menu_referrals")
async def referrals_handler(callback: CallbackQuery, state: FSMContext) -> None:
    await safe_answer(callback)
    await state.clear()

    bot_info = await callback.bot.get_me()
    user_id = callback.from_user.id
    referral_link = f"https://t.me/{bot_info.username}?start=ref_{user_id}"
    reward = await get_referral_reward()

    text = (
        f"За каждого друга, который перейдет по твоей ссылке, ты получаешь +{reward}⭐️!\n\n"
        f"🔗 Твоя реферальная ссылка:\n"
        f"{referral_link}\n\n"
        f"🎉 Приглашай по этой ссылке своих друзей, отправляй её во все чаты и зарабатывай Звёзды!"
    )
    await replace_with_photo(callback, REFERRAL_PHOTO_URL, text, back_markup("back_to_main"))


# ================= ТОП =================
@router.callback_query(F.data == "menu_top")
async def top_handler(callback: CallbackQuery, state: FSMContext) -> None:
    await safe_answer(callback)
    await state.clear()

    user_id = callback.from_user.id
    await ensure_user(callback.from_user)

    top = await get_top(5)
    medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]

    lines = []
    for i in range(5):
        if i < len(top):
            _, name, refs = top[i]
            lines.append(f"{medals[i]} {name} — рефералов: {refs}")
        else:
            lines.append(f"{medals[i]} —")

    my_refs, my_rank = await get_user_refs_and_rank(user_id)
    if my_rank is not None and my_rank <= 5:
        footer = f"🏆 Ты на {my_rank} месте! Рефералов: {my_refs}"
    elif my_rank is not None:
        footer = f"📍 Твоё место: {my_rank} | Рефералов: {my_refs}"
    else:
        footer = f"🚫 Ты не попал в топ! | {my_refs} рефералов."

    text = "🏆 Топ 5 по приглашениям:\n\n" + "\n".join(lines) + f"\n\n{footer}"
    await replace_with_photo(callback, TOP_PHOTO_URL, text, back_markup("back_to_main"))


# ================= ВЫВОД =================
@router.callback_query(F.data == "menu_withdraw")
async def withdraw_handler(callback: CallbackQuery, state: FSMContext) -> None:
    await safe_answer(callback)
    await state.clear()

    text = "Выбери количество звёзд, которое хочешь вывести:"
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="15 ⭐", callback_data="withdraw_15"),
                InlineKeyboardButton(text="25 ⭐", callback_data="withdraw_25"),
            ],
            [
                InlineKeyboardButton(text="50 ⭐", callback_data="withdraw_50"),
                InlineKeyboardButton(text="100 ⭐", callback_data="withdraw_100"),
            ],
            [
                InlineKeyboardButton(text="Другое количество", callback_data="withdraw_custom"),
                InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_main"),
            ],
        ]
    )
    await replace_with_photo(callback, WITHDRAW_PHOTO_URL, text, markup)


@router.callback_query(F.data.startswith("withdraw_") & ~F.data.in_({"withdraw_custom"}))
async def withdraw_preset_handler(callback: CallbackQuery, state: FSMContext) -> None:
    amount = int(callback.data.split("_")[1])
    user_id = callback.from_user.id
    await set_wd(user_id, {"amount": amount})

    await state.set_state(WithdrawStates.waiting_for_username)
    await safe_answer(callback)
    await replace_message(callback, "Введите @username получателя:", back_markup("menu_withdraw"))


@router.callback_query(F.data == "withdraw_custom")
async def withdraw_custom_handler(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(WithdrawStates.waiting_for_custom_amount)
    await safe_answer(callback)
    await replace_message(callback, "Введите любое целое число от 50 ⭐", back_markup("menu_withdraw"))


@router.callback_query(F.data == "confirm_withdraw")
async def confirm_withdraw_handler(callback: CallbackQuery, state: FSMContext) -> None:
    user_id = callback.from_user.id
    await ensure_user(callback.from_user)
    temp_data = await get_wd(user_id)

    amount = to_int(temp_data.get("amount"))
    username = temp_data.get("username", "@unknown")

    if amount <= 0:
        await safe_answer(callback, "❌ Данные заявки потеряны, начните заново.", show_alert=True)
        return

    # Сначала списываем, затем проверяем остаток — так нельзя вывести дважды одни и те же звёзды
    new_balance = await add_balance(user_id, -amount)
    if new_balance is None:
        await safe_answer(callback, "❌ Ошибка сервера, попробуйте позже.", show_alert=True)
        return
    if new_balance < 0:
        await add_balance(user_id, amount)  # возвращаем
        await safe_answer(callback, "❌ У вас недостаточно средств на балансе!", show_alert=True)
        return

    await redis_cmd("INCRBYFLOAT", "stat:withdrawals", amount)
    await state.clear()
    await del_wd(user_id)
    await safe_answer(callback)

    await replace_message(callback, f"Заявка на вывод {amount} ⭐ успешно выполнена", back_markup("back_to_main"))

    user = callback.from_user
    req_id = await create_withdraw_request(user, username, amount)
    refs = await get_referrals_info(user_id)
    subs = sum(r["sponsors"] or 0 for r in refs)
    user_tag = f"@{user.username}" if user.username else f"id{user_id}"
    title = f"📥 Заявка на вывод #{req_id}" if req_id else "📥 Заявка на вывод"
    suffix = f"_{req_id}" if req_id else ""
    admin_text = (
        f"{title}\n"
        f"Пользователь: {user.first_name or '—'} ({user_tag})\n"
        f"Получатель: {username}\n"
        f"Количество: {amount}⭐\n"
        f"Приглашено: {len(refs)} · подписок на спонсоров: {subs}"
    )
    admin_markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Выплатил ✅", callback_data=f"admin_pay_yes{suffix}"),
                InlineKeyboardButton(text="Не одобрена ❌", callback_data=f"admin_pay_no{suffix}"),
            ]
        ]
    )
    try:
        await callback.bot.send_message(chat_id=ADMIN_ID, text=admin_text, reply_markup=admin_markup)
    except Exception:
        pass


@router.callback_query(F.data.startswith("admin_pay_"))
async def admin_action_handler(callback: CallbackQuery) -> None:
    if callback.from_user.id != ADMIN_ID:
        await safe_answer(callback, "Доступ запрещен", show_alert=True)
        return
    await safe_answer(callback)
    parts = callback.data.split("_")  # admin, pay, yes|no[, id]
    is_yes = parts[2] == "yes"
    if len(parts) > 3:
        await set_wd_status(to_int(parts[3]), "paid" if is_yes else "rejected")
    action_text = "✅ Выплата отмечена как выполненная." if is_yes else "❌ Заявка отклонена."
    try:
        await callback.message.edit_text(f"{callback.message.text}\n\nСтатус: {action_text}")
    except Exception:
        pass


# ================= ШТРАФ ЗА ОТПИСКУ =================
UNSUBSCRIBE_PENALTY = 3.0     # штраф пользователю, ⭐
REFERRER_PENALTY = 3.0        # штраф пригласившему, ⭐
WATCH_DAYS = 3                # сколько дней нельзя отписываться
RECHECK_INTERVAL = 60         # как часто перепроверять одного пользователя при его заходах (сек)
CRON_RECHECK = 6 * 3600       # как часто перепроверять по расписанию (сек)
CRON_BATCH = 20               # сколько пользователей проверять за один вызов /cron


def days_text(n: int) -> str:
    n10, n100 = n % 10, n % 100
    if n10 == 1 and n100 != 11:
        return f"{n} день"
    if 2 <= n10 <= 4 and not 12 <= n100 <= 14:
        return f"{n} дня"
    return f"{n} дней"


def fmt_stars(x: float) -> str:
    """1.4 -> '1.4', 86 -> '86.0', 1.35 -> '1.35'"""
    s = f"{x:.2f}"
    return s[:-1] if s.endswith("0") else s


def sponsors_done_text() -> str:
    return (
        "<b>✅ Задание выполнено!\n\n"
        f"❗ Не отписывайся от канала {days_text(WATCH_DAYS)}, иначе получишь штраф. "
        "Совет: закинь канал в архив и отключи уведомления</b>"
    )


def penalty_user_text(balance: float) -> str:
    return (
        "<b>⚠️ Вы отписались от канала\n\n"
        f"Отписка зафиксирована раньше, чем через {days_text(WATCH_DAYS)} после выполнения задания.\n\n"
        f"💸 Штраф: -{UNSUBSCRIBE_PENALTY:g}⭐\n"
        f"💳 Баланс: {fmt_stars(balance)}⭐\n\n"
        f"Сохраняйте подписки минимум {days_text(WATCH_DAYS)} — таковы условия начисления звёзд.</b>"
    )


def penalty_referrer_text(username: str, reward: float, balance: float) -> str:
    return (
        f"<b>реферал: {html.escape(username)} отписался раньше, чем {days_text(WATCH_DAYS)}\n\n"
        f"💸 Штраф: -{reward:g} ⭐\n"
        f"доход: {fmt_stars(balance)} ⭐\n\n"
        f"Рефералы должны сохранять подписки минимум {days_text(WATCH_DAYS)} "
        "таковы условия начисления звёзд</b>"
    )


async def send_sponsors_done(target) -> None:
    message = target.message if isinstance(target, CallbackQuery) else target
    try:
        await message.answer(sponsors_done_text(), parse_mode="HTML")
    except Exception as e:
        print(f"send_sponsors_done error: {e!r}")


# ----- проверка «всё ещё подписан» -----
# Штраф выдаётся только если сервис ЯВНО ответил «не подписан». Ошибка или пустой ответ — не штраф.
# Подключены сервисы, у которых API показывает текущее состояние подписки.
async def retention_piarflow(session, uid, items):
    _, res, _ = await api_post(
        session, "piarflow-retention", f"{PIARFLOW_API_URL}/sponsors/check",
        {"user_id": uid, "links": [i["link"] for i in items]}, piarflow_headers(),
    )
    if not isinstance(res, dict):
        return []
    bad = {s.get("link") for s in (res.get("sponsors") or []) if s.get("status") == "unsubscribed"}
    return [i for i in items if i["link"] in bad]


async def retention_own(session, uid, items):
    """Штраф только если Telegram явно показал, что человек не в канале."""
    bot = _current_bot.get()
    if bot is None:
        return []
    return [
        i for i in items
        if await check_subscription(bot, i.get("chat_id", OWN_CHANNEL_ID), uid) is False
    ]


RETENTION = {"own": retention_own, "piarflow": retention_piarflow}


async def start_watch(uid: int, shown: dict) -> None:
    """Пользователь выполнил спонсоров — следим за подпиской WATCH_DAYS дней."""
    items = {n: list(shown.get(n) or []) for n in RETENTION if shown.get(n)}
    # наш канал под наблюдением всегда, даже если человек был подписан ещё до бота
    items["own"] = [{"link": OWN_CHANNEL_LINK, "chat_id": OWN_CHANNEL_ID}]
    now = time.time()
    watch = {
        "started": now,
        "expires": now + WATCH_DAYS * 86400,
        "items": items,
        "last": now,
    }
    await redis_set_json(f"watch:{uid}", watch, ex=(WATCH_DAYS + 1) * 86400)
    await redis_cmd("ZADD", "watch_list", now + RECHECK_INTERVAL, uid)


async def apply_unsubscribe_penalty(bot: Bot, uid: int) -> None:
    # SET NX: штраф выдаётся один раз на пользователя
    first = await redis_cmd("SET", f"penalized:{uid}", "1", "NX")
    await redis_cmd("DEL", f"watch:{uid}")
    await redis_cmd("ZREM", "watch_list", uid)
    if first is None:
        return

    balance = await add_balance(uid, -UNSUBSCRIBE_PENALTY)
    if balance is not None:
        try:
            await bot.send_message(chat_id=uid, text=penalty_user_text(balance), parse_mode="HTML")
        except Exception:
            pass

    # Пригласившему возвращаем (списываем) награду за этого реферала
    raw = await redis_cmd("GET", f"refby:{uid}")
    if raw:
        try:
            ref_part, reward_part = str(raw).split(":", 1)
            ref_id, reward = int(ref_part), float(reward_part)
        except ValueError:
            return
        ref_balance = await add_balance(ref_id, -REFERRER_PENALTY)
        if ref_balance is None:
            return
        me = await get_user(uid)
        username = me["username"] if me and me["username"] else f"id{uid}"
        try:
            await bot.send_message(
                chat_id=ref_id, text=penalty_referrer_text(username, REFERRER_PENALTY, ref_balance), parse_mode="HTML",
            )
        except Exception:
            pass


async def run_watch_check(bot: Bot, uid: int) -> bool:
    """Перепроверяет подписки пользователя. True — штраф выдан."""
    watch = await redis_get_json(f"watch:{uid}")
    now = time.time()
    if not isinstance(watch, dict) or now >= to_float(watch.get("expires")):
        # срок вышел или записи нет — следить больше не нужно
        await redis_cmd("DEL", f"watch:{uid}")
        await redis_cmd("ZREM", "watch_list", uid)
        return False

    items = watch.get("items") or {}
    names = [n for n in RETENTION if items.get(n)]
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as session:
        results = await asyncio.gather(
            *(RETENTION[n](session, uid, items[n]) for n in names), return_exceptions=True,
        )
    unsubscribed = False
    for name, res in zip(names, results):
        if isinstance(res, Exception):
            print(f"[{name}] ошибка проверки удержания: {res!r}")
            continue
        if res:
            unsubscribed = True

    if unsubscribed:
        await apply_unsubscribe_penalty(bot, uid)
        return True

    expires = to_float(watch.get("expires"))
    watch["last"] = now
    await redis_set_json(f"watch:{uid}", watch, ex=int(expires - now) + 86400)
    # последняя проверка — незадолго до конца срока
    await redis_cmd("ZADD", "watch_list", max(min(now + CRON_RECHECK, expires - 60), now + 60), uid)
    return False


async def maybe_check_watch(event) -> None:
    """Вызывается, когда пользователь заходит в бота: перепроверка не чаще RECHECK_INTERVAL."""
    uid = event.from_user.id
    watch = await redis_get_json(f"watch:{uid}")
    if not isinstance(watch, dict):
        return
    if time.time() - to_float(watch.get("last")) < RECHECK_INTERVAL:
        return
    try:
        await run_watch_check(event.bot, uid)
    except Exception as e:
        print(f"maybe_check_watch error: {e!r}")


@router.chat_member(F.chat.id == OWN_CHANNEL_ID)
async def own_channel_member_handler(event: ChatMemberUpdated) -> None:
    """Человек вышел из нашего канала — штраф сразу, если он ещё в 3-дневном окне."""
    if _status_str(event.new_chat_member) not in ("left", "kicked"):
        return
    uid = event.new_chat_member.user.id
    watch = await redis_get_json(f"watch:{uid}")
    if not isinstance(watch, dict) or time.time() >= to_float(watch.get("expires")):
        return
    if not (watch.get("items") or {}).get("own"):
        return
    bot = _current_bot.get() or event.bot
    await apply_unsubscribe_penalty(bot, uid)


async def process_cron() -> int:
    """Проверка подписок по расписанию. Возвращает, сколько пользователей проверено."""
    bot = Bot(token=BOT_TOKEN)
    redis_session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5))
    token = _redis_session.set(redis_session)
    bot_token = _current_bot.set(bot)
    try:
        ids = await redis_cmd("ZRANGEBYSCORE", "watch_list", "-inf", time.time(), "LIMIT", 0, CRON_BATCH) or []
        sem = asyncio.Semaphore(5)

        async def one(uid):
            async with sem:
                try:
                    await run_watch_check(bot, to_int(uid))
                except Exception as e:
                    print(f"cron check error for {uid}: {e!r}")

        await asyncio.gather(*(one(i) for i in ids))
        return len(ids)
    finally:
        _current_bot.reset(bot_token)
        _redis_session.reset(token)
        await redis_session.close()
        await bot.session.close()


# ================= ЗАЯВКИ НА ВЫВОД (админ-панель) =================
MSK = timezone(timedelta(hours=3))
WD_PAGE_SIZE = 5
WD_STATUS = {"new": "⏳ новая", "paid": "✅ выплачена", "rejected": "❌ не одобрена"}


def fmt_ts(ts) -> str:
    try:
        return datetime.fromtimestamp(float(ts), MSK).strftime("%d.%m.%Y %H:%M")
    except Exception:
        return "—"


async def record_sponsors(uid: int, count: int) -> None:
    """Запоминает, на скольких спонсоров подписался пользователь (только первый раз)."""
    await redis_cmd("HSETNX", user_key(uid), "sponsors", count)


async def get_referrals_info(uid: int) -> list[dict]:
    """Кого пригласил пользователь и сколько спонсоров выполнил каждый реферал."""
    ids = await redis_cmd("SMEMBERS", f"refs:{uid}") or []
    if not ids:
        return []
    rows = await redis_pipeline([["HMGET", user_key(to_int(i)), "username", "sponsors"] for i in ids])
    out = []
    for rid, row in zip(ids, rows):
        username, sponsors = None, None
        if isinstance(row, list) and len(row) == 2:
            username, sponsors = row
        out.append({
            "id": to_int(rid),
            "username": username or f"id{rid}",
            "sponsors": to_int(sponsors) if sponsors is not None else None,
        })
    out.sort(key=lambda r: r["id"])
    return out


async def create_withdraw_request(user, recipient: str, amount: int) -> int:
    """Сохраняет заявку на вывод. Возвращает её номер (0 — если Redis недоступен)."""
    req_id = to_int(await redis_cmd("INCR", "wd_seq"))
    if req_id <= 0:
        return 0
    now = time.time()
    req = {
        "id": req_id,
        "user_id": user.id,
        "name": user.first_name or "",
        "username": f"@{user.username}" if user.username else f"id{user.id}",
        "recipient": recipient,
        "amount": amount,
        "ts": now,
        "status": "new",
    }
    await redis_set_json(f"wdreq:{req_id}", req)
    await redis_cmd("ZADD", "wd_list", now, req_id)
    return req_id


async def get_wd_request(req_id: int) -> Optional[dict]:
    req = await redis_get_json(f"wdreq:{req_id}")
    return req if isinstance(req, dict) else None


async def set_wd_status(req_id: int, status: str) -> Optional[dict]:
    req = await get_wd_request(req_id)
    if not req:
        return None
    req["status"] = status
    await redis_set_json(f"wdreq:{req_id}", req)
    return req


async def render_wd_details(req: dict, page: int):
    refs = await get_referrals_info(req["user_id"])
    total_sponsors = sum(r["sponsors"] or 0 for r in refs)
    lines = [
        f"💸 Заявка #{req['id']}",
        f"Статус: {WD_STATUS.get(req.get('status'), req.get('status'))}",
        f"Пользователь: {req.get('name') or '—'} ({req.get('username')}, id {req['user_id']})",
        f"Получатель: {req.get('recipient')}",
        f"Сумма: {req.get('amount')} ⭐",
        f"Дата: {fmt_ts(req.get('ts'))} (МСК)",
        "",
        f"👥 Приглашено: {len(refs)} · подписок на спонсоров: {total_sponsors}",
    ]
    if refs:
        lines.append("")
        for i, r in enumerate(refs[:60], 1):
            sp = str(r["sponsors"]) if r["sponsors"] is not None else "нет данных"
            lines.append(f"{i}. {r['username']} — спонсоров: {sp}")
        if len(refs) > 60:
            lines.append(f"… и ещё {len(refs) - 60}")
    else:
        lines.append("Рефералов пока нет.")

    rows = []
    if req.get("status") == "new":
        rows.append([
            InlineKeyboardButton(text="Выплатил ✅", callback_data=f"admin_wdpay_yes_{req['id']}_{page}"),
            InlineKeyboardButton(text="Не одобрена ❌", callback_data=f"admin_wdpay_no_{req['id']}_{page}"),
        ])
    rows.append([InlineKeyboardButton(text="⬅️ К списку", callback_data=f"admin_wdlist_{page}")])
    return "\n".join(lines)[:4000], InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data.startswith("admin_wdlist_"))
async def admin_wd_list(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.from_user.id != ADMIN_ID:
        await safe_answer(callback, "Доступ запрещен", show_alert=True)
        return
    await safe_answer(callback)

    page = max(to_int(callback.data.split("_")[2]), 0)
    total = to_int(await redis_cmd("ZCARD", "wd_list"))
    pages = max(1, -(-total // WD_PAGE_SIZE))
    page = min(page, pages - 1)

    ids = await redis_cmd("ZREVRANGE", "wd_list", page * WD_PAGE_SIZE, (page + 1) * WD_PAGE_SIZE - 1) or []
    raws = await redis_pipeline([["GET", f"wdreq:{i}"] for i in ids])
    reqs = []
    for raw in raws:
        try:
            req = json.loads(raw) if raw else None
        except Exception:
            req = None
        if isinstance(req, dict):
            reqs.append(req)
    infos = await asyncio.gather(*(get_referrals_info(r["user_id"]) for r in reqs))

    rows = []
    if not reqs:
        text = "💸 Заявок на вывод пока нет."
    else:
        lines = [f"💸 Заявки на вывод — всего {total} (стр. {page + 1}/{pages})", ""]
        for req, refs in zip(reqs, infos):
            subs = sum(r["sponsors"] or 0 for r in refs)
            status = WD_STATUS.get(req.get("status"), req.get("status"))
            lines += [
                f"#{req['id']} · {req['username']} · {req['amount']} ⭐ · {status}",
                f"получатель: {req['recipient']} · {fmt_ts(req.get('ts'))}",
                f"приглашено: {len(refs)} · подписок на спонсоров: {subs}",
                "",
            ]
            rows.append([InlineKeyboardButton(
                text=f"#{req['id']} · {req['amount']}⭐ · подробнее",
                callback_data=f"admin_wdview_{req['id']}_{page}",
            )])
        lines.append("Нажмите на заявку, чтобы увидеть всех рефералов и их подписки.")
        text = "\n".join(lines)[:4000]

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"admin_wdlist_{page - 1}"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"admin_wdlist_{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_panel")])
    await replace_message(callback, text, InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("admin_wdview_"))
async def admin_wd_view(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.from_user.id != ADMIN_ID:
        await safe_answer(callback, "Доступ запрещен", show_alert=True)
        return
    parts = callback.data.split("_")  # admin, wdview, id, page
    req = await get_wd_request(to_int(parts[2]))
    if not req:
        await safe_answer(callback, "Заявка не найдена", show_alert=True)
        return
    await safe_answer(callback)
    text, markup = await render_wd_details(req, to_int(parts[3]))
    await replace_message(callback, text, markup)


@router.callback_query(F.data.startswith("admin_wdpay_"))
async def admin_wd_pay(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.from_user.id != ADMIN_ID:
        await safe_answer(callback, "Доступ запрещен", show_alert=True)
        return
    parts = callback.data.split("_")  # admin, wdpay, yes|no, id, page
    status = "paid" if parts[2] == "yes" else "rejected"
    req = await set_wd_status(to_int(parts[3]), status)
    if not req:
        await safe_answer(callback, "Заявка не найдена", show_alert=True)
        return
    await safe_answer(callback, "Отмечено")
    text, markup = await render_wd_details(req, to_int(parts[4]))
    await replace_message(callback, text, markup)


# ================= ПРОМОКОД (пользователь) =================
@router.callback_query(F.data == "menu_promocode")
async def promocode_menu_handler(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(PromocodeStates.waiting_for_activation_input)
    await safe_answer(callback)
    await replace_message(callback, "🎟 Введите промокод:", back_markup("back_to_main"))


# ================= АДМИН ПАНЕЛЬ =================
@router.callback_query(F.data == "admin_panel")
async def admin_panel_handler(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.from_user.id != ADMIN_ID:
        await safe_answer(callback, "Доступ запрещен", show_alert=True)
        return
    await safe_answer(callback)
    await state.clear()

    now = time.time()
    total_users = to_int(await redis_cmd("SCARD", "users"))

    async def joined_since(seconds: int) -> int:
        return to_int(await redis_cmd("ZCOUNT", "users_joined", now - seconds, "+inf"))

    u_24h = await joined_since(24 * 3600)
    u_7d = await joined_since(7 * 24 * 3600)
    u_30d = await joined_since(30 * 24 * 3600)

    sponsors_count = to_int(await redis_cmd("GET", "stat:sponsors_completed"))
    total_sponsor_subs = await get_income_sponsors()
    income_rub = total_sponsor_subs * INCOME_PER_SPONSOR
    withdrawals = to_float(await redis_cmd("GET", "stat:withdrawals"))

    text = (
        "панель админа:\n"
        f"всего пользователей: {total_users}\n"
        f"пользователей за 24h: {u_24h}\n"
        f"пользователей за 7 дней: {u_7d}\n"
        f"пользователей за 30 дней: {u_30d}\n"
        f"пользователей подписавшись на спонсоров: {sponsors_count}\n"
        f"подписок на спонсоров всего: {total_sponsor_subs}\n"
        f"доход: {income_rub:g} руб\n"
        f"выводы: {withdrawals:g} звезд"
    )
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🚀 Рассылка", callback_data="admin_broadcast"),
                InlineKeyboardButton(text="⭐ Задание", callback_data="admin_tasks_menu"),
            ],
            [
                InlineKeyboardButton(text="🎁 Промокоды", callback_data="admin_promo_menu"),
                InlineKeyboardButton(text="✨ Вознаграждение", callback_data="admin_reward_menu"),
            ],
            [InlineKeyboardButton(text="💸 Выводы", callback_data="admin_wdlist_0")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_main")],
        ]
    )
    await replace_message(callback, text, markup)


@router.callback_query(F.data == "admin_broadcast")
async def admin_bc_start(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.from_user.id != ADMIN_ID:
        await safe_answer(callback, "Доступ запрещен", show_alert=True)
        return
    await state.set_state(AdminBroadcastStates.waiting_for_text)
    await safe_answer(callback)

    count_users = to_int(await redis_cmd("SCARD", "users"))
    text = f"пользователей увидят рассылку: {count_users}\n\nвведите текст рассылки"
    await replace_message(callback, text, back_markup("admin_panel"))


@router.callback_query(F.data == "admin_bc_send")
async def admin_bc_send(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.from_user.id != ADMIN_ID:
        await safe_answer(callback, "Доступ запрещен", show_alert=True)
        return
    data = await state.get_data()
    bc_text = data.get("broadcast_text", "")
    await safe_answer(callback)
    await state.clear()

    user_ids = await redis_cmd("SMEMBERS", "users") or []
    sent_count = 0
    for uid in user_ids:
        try:
            await callback.bot.send_message(chat_id=int(uid), text=bc_text)
            sent_count += 1
            await asyncio.sleep(0.05)
        except Exception:
            pass

    await replace_message(callback, f"отправлено пользователям: {sent_count}", back_markup("admin_panel"))


@router.callback_query(F.data == "admin_bc_edit")
async def admin_bc_edit(callback: CallbackQuery, state: FSMContext) -> None:
    await admin_bc_start(callback, state)


@router.callback_query(F.data == "admin_tasks_menu")
async def admin_tasks_menu(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.from_user.id != ADMIN_ID:
        await safe_answer(callback, "Доступ запрещен", show_alert=True)
        return
    await safe_answer(callback)

    tasks = await load_tasks()
    keyboard_rows = []
    if not tasks:
        text = "У вас нет активных заданий."
    else:
        text = "Активные задания (нажмите, чтобы удалить):"
        for t in tasks:
            keyboard_rows.append([
                InlineKeyboardButton(
                    text=f"❌ Удалить №{t['id']} ({t['link']})",
                    callback_data=f"admin_del_task_{t['id']}",
                )
            ])

    keyboard_rows.append([InlineKeyboardButton(text="➕ Создать задание", callback_data="admin_task_create")])
    keyboard_rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_panel")])
    await replace_message(callback, text, InlineKeyboardMarkup(inline_keyboard=keyboard_rows))


@router.callback_query(F.data == "admin_task_create")
async def admin_task_create(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.from_user.id != ADMIN_ID:
        await safe_answer(callback, "Доступ запрещен", show_alert=True)
        return
    await state.set_state(AdminTaskStates.text)
    await safe_answer(callback)
    await replace_message(callback, "ввести текст задания", back_markup("admin_panel"))


@router.callback_query(F.data.startswith("admin_del_task_"))
async def admin_del_task(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.from_user.id != ADMIN_ID:
        await safe_answer(callback, "Доступ запрещен", show_alert=True)
        return
    task_id = int(callback.data.split("_")[3])
    tasks = await load_tasks()
    await save_tasks([t for t in tasks if t["id"] != task_id])
    await redis_cmd("DEL", f"task_cnt:{task_id}")
    await safe_answer(callback, "Задание удалено!")
    await admin_tasks_menu(callback, state)


@router.callback_query(F.data == "admin_task_pub")
async def admin_task_pub(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.from_user.id != ADMIN_ID:
        await safe_answer(callback, "Доступ запрещен", show_alert=True)
        return
    data = await state.get_data()
    await safe_answer(callback)
    await state.clear()

    # Счётчик id: номера заданий не повторяются, даже если старые удалены
    next_id = to_int(await redis_cmd("INCR", "task_seq"))
    tasks = await load_tasks()
    tasks.append({
        "id": next_id,
        "text": data.get("task_text"),
        "link": data.get("task_link"),
        "chat_id": data.get("task_chat"),
        "reward": data.get("task_reward"),
        "max_completions": data.get("task_max_completions"),
    })
    await save_tasks(tasks)
    await admin_panel_handler(callback, state)


@router.callback_query(F.data == "admin_promo_menu")
async def admin_promo_menu(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.from_user.id != ADMIN_ID:
        await safe_answer(callback, "Доступ запрещен", show_alert=True)
        return
    await safe_answer(callback)
    await state.clear()

    codes = await redis_cmd("ZRANGE", "promos", 0, -1) or []
    promos = []
    for code in codes:
        p = await get_promo(code)
        if p:
            promos.append((code, p))
        else:
            await redis_cmd("ZREM", "promos", code)  # чистим «мёртвые» записи

    if not promos:
        text = "у вас нету активных промокодов 🎁"
        markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="Создать", callback_data="admin_promo_create")],
                [InlineKeyboardButton(text="Назад", callback_data="admin_panel")],
            ]
        )
    else:
        keyboard_rows = []
        for code, _ in promos:
            keyboard_rows.append([
                InlineKeyboardButton(text=f"🗑 Удалить: {code}", callback_data=f"admin_del_promo_{code}")
            ])

        _, p = promos[-1]
        text = (
            f"{len(promos)} активный промокод ⭐\n"
            f"название: {p['name']}\n"
            f"Цена: {p['price']} звезд\n"
            f"кол во активаций: {p['activations']}"
        )
        keyboard_rows.append([InlineKeyboardButton(text="Создать", callback_data="admin_promo_create")])
        keyboard_rows.append([InlineKeyboardButton(text="Назад", callback_data="admin_panel")])
        markup = InlineKeyboardMarkup(inline_keyboard=keyboard_rows)

    await replace_message(callback, text, markup)


@router.callback_query(F.data.startswith("admin_del_promo_"))
async def admin_del_promo(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.from_user.id != ADMIN_ID:
        await safe_answer(callback, "Доступ запрещен", show_alert=True)
        return
    code = callback.data.replace("admin_del_promo_", "")
    if await get_promo(code):
        await delete_promo(code)
        await safe_answer(callback, "Промокод удален!")
    await admin_promo_menu(callback, state)


@router.callback_query(F.data == "admin_promo_create")
async def admin_promo_create(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.from_user.id != ADMIN_ID:
        await safe_answer(callback, "Доступ запрещен", show_alert=True)
        return
    await state.set_state(PromocodeStates.admin_name)
    await safe_answer(callback)
    await replace_message(callback, "введите название промокода:", back_markup("admin_panel", "Назад"))


@router.callback_query(F.data == "admin_promo_pub")
async def admin_promo_pub(callback: CallbackQuery, state: FSMContext) -> None:
    await admin_promo_menu(callback, state)


@router.callback_query(F.data == "admin_reward_menu")
async def admin_reward_menu(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.from_user.id != ADMIN_ID:
        await safe_answer(callback, "Доступ запрещен", show_alert=True)
        return
    await safe_answer(callback)

    reward = await get_referral_reward()
    text = f"цена за реферала {reward} ⭐"
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="изменить", callback_data="admin_reward_change"),
                InlineKeyboardButton(text="назад", callback_data="admin_panel"),
            ]
        ]
    )
    await replace_message(callback, text, markup)


@router.callback_query(F.data == "admin_reward_change")
async def admin_reward_change(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.from_user.id != ADMIN_ID:
        await safe_answer(callback, "Доступ запрещен", show_alert=True)
        return
    await state.set_state(AdminRewardStates.waiting_for_new_value)
    await safe_answer(callback)
    await replace_message(callback, "введите количество за реферала", back_markup("admin_panel", "назад"))


@router.callback_query(F.data == "admin_reward_apply")
async def admin_reward_apply(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.from_user.id != ADMIN_ID:
        await safe_answer(callback, "Доступ запрещен", show_alert=True)
        return
    data = await state.get_data()
    new_val = data.get("new_reward")
    if new_val is not None:
        await redis_cmd("SET", "cfg:referral_reward", new_val)
    await state.clear()
    await admin_reward_menu(callback, state)


@router.callback_query(F.data == "back_to_main")
async def back_to_main_handler(callback: CallbackQuery, state: FSMContext) -> None:
    await safe_answer(callback)
    await state.clear()
    await send_main_menu(callback)


# ================= ПРОВЕРКА ПОДПИСКИ =================
@router.callback_query(F.data == "check_all_tasks")
async def check_all_tasks_handler(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id
    await ensure_user(callback.from_user)
    tasks_data = await get_active(user_id)

    if not tasks_data or tasks_data.get("v") != 2:
        # Данных о спонсорах нет (или они от старой версии бота) — просто заново запрашиваем
        await safe_answer(callback)
        await show_sponsors(callback.message, callback.from_user)
        return

    user = callback.from_user
    names = [n for n in SERVICES if tasks_data.get(n)]
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as session:
        results = await asyncio.gather(
            *(CHECKERS[n](session, user, tasks_data[n]) for n in names),
            return_exceptions=True,
        )
    for name, res in zip(names, results):
        if isinstance(res, Exception):
            print(f"[{name}] ошибка проверки: {res!r}")  # список этого сервиса оставляем как был
            continue
        tasks_data[name] = res

    limit_sponsors(tasks_data)
    all_links = links_of(tasks_data)

    if not all_links:
        await del_active(user_id)
        done_count = await pop_sponsor_total(user_id, tasks_data.get("total_count", 0))
        await add_income(user_id, done_count)
        await record_sponsors(user_id, done_count)
        await start_watch(user_id, tasks_data.get("all") or {})
        await mark_passed(user_id)

        await safe_answer(callback)
        await after_sponsors(callback.bot, callback, callback.from_user, had_sponsors=True, sponsors_count=done_count)
    else:
        await set_active(user_id, tasks_data)
        try:
            await callback.message.edit_reply_markup(reply_markup=build_sponsor_markup(all_links))
        except Exception:
            pass
        await safe_answer(callback, "❌ Вы подписались не на все каналы. Проверьте оставшиеся подписки.")


# ================= FLASK WEBHOOK (ДЛЯ VERCEL) =================
app = Flask(__name__)
dp.include_router(router)


async def process_update(json_data: dict) -> None:
    # Новый Bot на каждый апдейт: aiohttp-сессия привязана к event loop,
    # а asyncio.run() каждый раз создаёт и закрывает новый loop.
    bot = Bot(token=BOT_TOKEN)
    # Одна сессия для всех запросов к Redis в рамках этого апдейта
    redis_session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5))
    token = _redis_session.set(redis_session)
    bot_token = _current_bot.set(bot)
    try:
        update = Update.model_validate(json_data, context={"bot": bot})
        await dp.feed_update(bot, update)
    except Exception as e:
        print(f"Webhook error: {e!r}")
    finally:
        _current_bot.reset(bot_token)
        _redis_session.reset(token)
        await redis_session.close()
        await bot.session.close()


@app.route("/", methods=["POST"])
def webhook():
    if "application/json" in (request.headers.get("content-type") or ""):
        json_data = request.get_json(force=True)
        asyncio.run(process_update(json_data))
        return "OK", 200
    return "Error", 403


@app.route("/", methods=["GET"])
def index():
    return "Bot is running on Vercel!", 200


@app.route("/setwebhook", methods=["GET"])
def set_webhook_route():
    """Один раз открыть: https://ВАШ-ДОМЕН/setwebhook?key=<CRON_SECRET>
    Включает получение событий выхода из канала (chat_member) для мгновенных штрафов."""
    secret = os.environ.get("CRON_SECRET")
    given = request.args.get("key", "")
    if not secret or not hmac.compare_digest(given.encode(), secret.encode()):
        return "Forbidden", 403

    async def _go():
        bot = Bot(token=BOT_TOKEN)
        try:
            return await bot.set_webhook(
                url=f"https://{request.host}/",
                allowed_updates=["message", "callback_query", "chat_member"],
            )
        finally:
            await bot.session.close()

    return f"webhook set: {asyncio.run(_go())}", 200


@app.route("/cron", methods=["GET", "POST"])
def cron():
    """Проверка подписок по расписанию. Доступ только с CRON_SECRET
    (заголовок Authorization: Bearer <секрет> — так шлёт Vercel Cron, или ?key=<секрет>)."""
    secret = os.environ.get("CRON_SECRET")
    given = (request.headers.get("Authorization") or "").removeprefix("Bearer ").strip() or request.args.get("key", "")
    if not secret or not hmac.compare_digest(given.encode(), secret.encode()):
        return "Forbidden", 403
    checked = asyncio.run(process_cron())
    return f"checked: {checked}", 200
