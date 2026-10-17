
import asyncio
import json
import os
import aiohttp
from datetime import datetime, timedelta
from flask import Flask, request
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    Update,
)

# ======================================================================
#                         НАСТРОЙКИ — ВСТАВЬТЕ КЛЮЧИ ЗДЕСЬ
# ======================================================================
BOT_TOKEN = os.environ.get("BOT_TOKEN")   # токен бота (из Vercel)
ADMIN_ID = 619112100                      # ID администратора

TGRASS_API_KEY = "ВСТАВЬТЕ_КЛЮЧ_TGRASS"
PIARFLOW_API_KEY = "ВСТАВЬТЕ_КЛЮЧ_PIARFLOW"
TRAFSLY_API_KEY = "ВСТАВЬТЕ_КЛЮЧ_TRAFSLY"
# ======================================================================

PIARFLOW_API_URL = "https://piarflow.com/v1"
TRAFSLY_API_URL = "https://api.trafsly.com"
TGRASS_API_URL = "https://tgrass.space"

PLACEHOLDER_MARK = "ВСТАВЬТЕ_КЛЮЧ"


def key_filled(value: str) -> bool:
    return bool(value) and PLACEHOLDER_MARK not in value


for _name, _val in (
    ("BOT_TOKEN", BOT_TOKEN),
    ("TGRASS_API_KEY", TGRASS_API_KEY),
    ("PIARFLOW_API_KEY", PIARFLOW_API_KEY),
    ("TRAFSLY_API_KEY", TRAFSLY_API_KEY),
):
    if not key_filled(_val):
        print(f"⚠️ НЕ ЗАПОЛНЕНО: {_name}")

PHOTO_URL = "https://i.postimg.cc/XvPS1pB2/5883351235946024467.jpg"
REFERRAL_PHOTO_URL = "https://i.postimg.cc/TYbwVvFM/6219881422739149359.jpg"
PROFILE_PHOTO_URL = "https://i.postimg.cc/bvcFZkbf/6219881422739149361.jpg"
TOP_PHOTO_URL = "https://i.postimg.cc/tJfCWpFM/6219881422739149363.jpg"
WITHDRAW_PHOTO_URL = "https://i.postimg.cc/RVdQKYYf/6219881422739149362.jpg"

dp = Dispatcher()
router = Router()


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
    reward = State()
    max_completions = State()
    preview = State()


class AdminRewardStates(StatesGroup):
    waiting_for_new_value = State()
    confirm = State()


# ================= ХРАНИЛИЩА (в памяти!) =================
# ВНИМАНИЕ: на Vercel память не постоянная. Для боевого бота нужна внешняя БД.
user_active_tasks: dict[int, dict] = {}
verified_users: set[int] = set()
fully_passed_users: set[int] = set()
