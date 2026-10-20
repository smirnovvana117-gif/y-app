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
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)

# ======================================================================
#                         НАСТРОЙКИ
#  Всё задаётся через переменные окружения Vercel.
#  Если переменной нет — используется значение-заглушка.
# ======================================================================
BOT_TOKEN = os.environ.get("BOT_TOKEN")   # токен бота (из Vercel)
ADMIN_ID = 619112100                      # ID администратора

TGRASS_API_KEY = os.environ.get("TGRASS_API_KEY", "6d6e9e9701e7440a81fe099d360710d5")
PIARFLOW_API_KEY = os.environ.get("PIARFLOW_API_KEY", "FNDvblihCM-RXnMX1U_UrErt_KuD8ujv")
TRAFSLY_API_KEY = os.environ.get("TRAFSLY_API_KEY", "at_99ddd0618296db80b905c92ff9adaa8c")
TRAFFY_API_KEY = os.environ.get("TRAFFY_API_KEY", "pk_live_38u8oOUn6p7OLmAnmKGrpsTsvwUfneBu")
FLYER_API_KEY = os.environ.get("FLYER_API_KEY", "FL-XCelim-WHmkUj-vBuGgN-bbMxPy")
FLOWLY_API_KEY = os.environ.get("FLOWLY_API_KEY", "9f0c8ea7e9a664b4a6326ce95f62b222")

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
FLOWLY_API_URL = "https://flowly.guru/api/v1"

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
    ("FLOWLY_API_KEY", FLOWLY_API_KEY),
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

TARGET_COUNT = 10
INCOME_PER_SPONSOR = 1.0          # доход (руб) за одну подписку на спонсора
DEFAULT_REFERRAL_REWARD = 4.5     # награда за реферала по умолчанию
FSM_TTL = 7 * 24 * 3600           # сколько хранить состояние диалога


# ================= REDIS (Upstash REST) =================
# Сессия aiohttp на время одного апдейта — чтобы не открывать соединение на каждую команду.
_redis_session: ContextVar = ContextVar("redis_session", default=None)
