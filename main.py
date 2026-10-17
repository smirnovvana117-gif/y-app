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

TGRASS_API_KEY = "6d6e9e9701e7440a81fe099d360710d5"
PIARFLOW_API_KEY = "FNDvblihCM-RXnMX1U_UrErt_KuD8ujv"
TRAFSLY_API_KEY = "at_99ddd0618296db80b905c92ff9adaa8c"
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
user_data_db: dict[int, dict] = {}
pending_referrals: dict[int, int] = {}
withdraw_temp_data: dict[int, dict] = {}

promocodes_db: dict[str, dict] = {}
admin_tasks_db: list[dict] = []
user_completed_tasks: dict[int, set[int]] = {}

global_stats = {
    "total_sponsors_completed": 0,
    "total_withdrawals_stars": 0.0,
    "referral_reward": 4.5,
}

TARGET_COUNT = 10


# ================= ХЕЛПЕРЫ =================
async def safe_answer(callback: CallbackQuery, *args, **kwargs) -> None:
    """Повторный callback.answer() вызывает ошибку, поэтому глушим её."""
    try:
        await callback.answer(*args, **kwargs)
    except Exception:
        pass


def ensure_user(user) -> dict:
    user_id = user.id
    username_str = f"@{user.username}" if user.username else f"id{user_id}"
    if user_id not in user_data_db:
        user_data_db[user_id] = {
            "name": user.first_name,
            "username": username_str,
            "balance": 0.00,
            "referrals": 0,
            "join_date": datetime.now(),
        }
    return user_data_db[user_id]


def back_markup(cb: str = "back_to_main", text: str = "⬅️ Назад") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=text, callback_data=cb)]])


async def replace_message(callback: CallbackQuery, text: str, markup=None) -> None:
    """Отправляет новое сообщение и удаляет старое."""
    try:
        await callback.message.answer(text, reply_markup=markup)
    except Exception as e:
        print(f"replace_message error: {e}")
        return
    try:
        await callback.message.delete()
    except Exception:
        pass


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


# ================= /start =================
@router.message(CommandStart())
async def start_handler(message: Message, command: CommandObject, state: FSMContext) -> None:
    await state.clear()
    user_id = message.from_user.id
    username_str = f"@{message.from_user.username}" if message.from_user.username else f"id{user_id}"

    is_new = user_id not in user_data_db
    ensure_user(message.from_user)
    user_data_db[user_id]["name"] = message.from_user.first_name
    user_data_db[user_id]["username"] = username_str

    args = command.args
    if args and args.startswith("ref_") and is_new:
        try:
            referrer_id = int(args.split("_")[1])
            if referrer_id != user_id and user_id not in pending_referrals:
                pending_referrals[user_id] = referrer_id
                if referrer_id in user_data_db:
                    ref_msg = (
                        f"реферал: {username_str}\n"
                        f"доход: 0.00 ⭐\n"
                        f"как только реферал подпишется на спонсоров, вы получите "
                        f"{global_stats['referral_reward']}⭐"
                    )
                    try:
                        await message.bot.send_message(chat_id=referrer_id, text=ref_msg)
                    except Exception:
                        pass
        except ValueError:
            pass

    verified_users.add(user_id)
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

    def key_status(v: str) -> str:
        return f"заполнен ({len(v)} симв.)" if key_filled(v) else "НЕ ЗАПОЛНЕН"

    lines = [
        "🔧 ДИАГНОСТИКА",
        f"BOT_TOKEN: {'задан' if BOT_TOKEN else 'НЕ ЗАДАН'}",
        f"TGRASS_API_KEY: {key_status(TGRASS_API_KEY)}",
        f"PIARFLOW_API_KEY: {key_status(PIARFLOW_API_KEY)}",
        f"TRAFSLY_API_KEY: {key_status(TRAFSLY_API_KEY)}",
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
        lines.append(f"TRAFSLY → HTTP {st}\n{body[:500]}")

    text = "\n".join(lines)
    await message.answer(text[:4000])


# ================= ВВОД ТЕКСТА (по состояниям) =================
@router.message()
async def any_message_handler(message: Message, state: FSMContext) -> None:
    current_state = await state.get_state()
    user_id = message.from_user.id
    text_msg = message.text.strip() if message.text else ""
    ensure_user(message.from_user)

    # ----- вывод: своя сумма -----
    if current_state == WithdrawStates.waiting_for_custom_amount.state:
        if not text_msg.isdigit():
            await message.answer("❌ Пожалуйста, введите корректное целое число.")
            return
        amount = int(text_msg)
        if amount < 50:
            await message.answer("❌ Минимальная сумма для вывода: 50 ⭐. Введите число от 50:")
            return

        withdraw_temp_data[user_id] = {"amount": amount}
        await state.set_state(WithdrawStates.waiting_for_username)
        await message.answer("Введите @username получателя:", reply_markup=back_markup("menu_withdraw"))
        return

    # ----- вывод: username -----
    if current_state == WithdrawStates.waiting_for_username.state:
        username = text_msg
        if not username.startswith("@"):
            username = "@" + username

        if user_id not in withdraw_temp_data:
            withdraw_temp_data[user_id] = {}
        withdraw_temp_data[user_id]["username"] = username

        amount = withdraw_temp_data[user_id].get("amount", 0)
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

        if code in promocodes_db:
            p_data = promocodes_db[code]
            if user_id in p_data["used_by"]:
                await message.answer("промокод уже был вами использован ❗", reply_markup=markup)
            elif p_data["activations"] <= 0:
                await message.answer("промокод использован или недействителен", reply_markup=markup)
            else:
                p_data["activations"] -= 1
                p_data["used_by"].add(user_id)
                user_data_db[user_id]["balance"] += p_data["price"]

                if p_data["activations"] <= 0:
                    del promocodes_db[code]

                await state.clear()
                await message.answer(f"на ваш баланс зачислено {p_data['price']} ⭐", reply_markup=markup)
        else:
            await message.answer("промокод использован или недействителен", reply_markup=markup)
        return

    # ===== ниже только для админа =====
    if user_id != ADMIN_ID:
        return

    # ----- рассылка -----
    if current_state == AdminBroadcastStates.waiting_for_text.state:
        await state.update_data(broadcast_text=text_msg)
        await state.set_state(AdminBroadcastStates.confirm)

        count_users = len(user_data_db)
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
        await state.update_data(task_link=text_msg)
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
            f"награда: {data.get('task_reward')}⭐\n"
            f"макс. выполнений: {data.get('task_max_completions')}"
        )
        markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="✅ Опубликовать", callback_data="admin_task_pub")],
                [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_panel")],
            ]
        )
        await message.answer(text, reply_markup=markup)
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
        promocodes_db[name] = {
            "name": name,
            "activations": data.get("promo_activations", 1),
            "price": price,
            "used_by": set(),
        }
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
    for link in all_links:
        temp_row.append(InlineKeyboardButton(text="✉️ Подписаться", url=link))
        if len(temp_row) == 2:
            buttons.append(temp_row)
            temp_row = []
    if temp_row:
        buttons.append(temp_row)
    buttons.append([InlineKeyboardButton(text="Проверить подписку", callback_data="check_all_tasks")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def reward_referrer_if_needed(bot: Bot, user_id: int) -> None:
    if user_id in pending_referrals:
        ref_id = pending_referrals.pop(user_id)
        if ref_id in user_data_db:
            reward = global_stats["referral_reward"]
            user_data_db[ref_id]["balance"] += reward
            user_data_db[ref_id]["referrals"] += 1

            ref_username = user_data_db.get(user_id, {}).get("username", f"id{user_id}")
            total_bal = user_data_db[ref_id]["balance"]
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

    collected_tgrass, collected_piarflow, collected_trafsly = [], [], []

    try:
        timeout = aiohttp.ClientTimeout(total=4)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            remaining = TARGET_COUNT

            collected_tgrass = await fetch_tgrass(session, user_id, user_data, remaining)
            remaining -= len(collected_tgrass)

            if remaining > 0:
                collected_piarflow = await fetch_piarflow(session, user_id, chat_id, remaining)
                remaining -= len(collected_piarflow)

            if remaining > 0:
                collected_trafsly = await fetch_trafsly(session, user_id, chat_id, user_data, remaining)
    except Exception as e:
        print(f"show_sponsors error: {e!r}")

    print(
        f"[sponsors] user={user_id} tgrass={len(collected_tgrass)} "
        f"piarflow={len(collected_piarflow)} trafsly={len(collected_trafsly)}"
    )

    user_active_tasks[user_id] = {
        "tgrass": collected_tgrass,
        "piarflow": collected_piarflow,
        "trafsly": collected_trafsly,
    }

    all_links = (
        [o["link"] for o in collected_tgrass]
        + [i["link"] for i in collected_piarflow]
        + [s["link"] for s in collected_trafsly]
    )

    if not all_links:
        if user_id not in fully_passed_users:
            global_stats["total_sponsors_completed"] += 1
        fully_passed_users.add(user_id)
        await reward_referrer_if_needed(message.bot, user_id)
        await send_main_menu(message, user)
        return

    markup = build_sponsor_markup(all_links)
    text = (
        "🤖 Для использования бота подпишись на каналы ниже\n\n"
        "🔻 После подписки нажимай на кнопку \"Проверить подписку\""
    )
    try:
        await message.answer_photo(photo=PHOTO_URL, caption=text, reply_markup=markup)
    except Exception:
        await message.answer(text, reply_markup=markup)


# ================= ГЛАВНОЕ МЕНЮ =================
async def send_main_menu(message_or_callback, user=None):
    if isinstance(message_or_callback, CallbackQuery):
        message = message_or_callback.message
        user = message_or_callback.from_user
    else:
        message = message_or_callback
        user = user or message.from_user

    ensure_user(user)
    stats = user_data_db[user.id]

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
async def show_task(callback: CallbackQuery) -> None:
    if not admin_tasks_db:
        await replace_message(callback, "нет активных заданий для выполнения", back_markup("back_to_main"))
        return

    task = admin_tasks_db[0]
    task_id = task["id"]
    user_completed_tasks.setdefault(task_id, set())

    text = (
        f"🎯 Доступно задание №{task_id}!\n\n"
        f"- Подпишитесь на канал —>\n {task['link']}\n-\n"
        f"Награда: {task['reward']}⭐"
    )
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Выполнить", url=task["link"])],
            [InlineKeyboardButton(text="пропустить задание", callback_data=f"skip_task_{task_id}")],
            [InlineKeyboardButton(text="✅ Я выполнил", callback_data=f"done_task_{task_id}")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_main")],
        ]
    )
    await replace_message(callback, text, markup)


@router.callback_query(F.data == "menu_task")
async def task_menu_handler(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await safe_answer(callback)
    ensure_user(callback.from_user)
    await show_task(callback)


@router.callback_query(F.data.startswith("done_task_"))
async def done_task_handler(callback: CallbackQuery) -> None:
    task_id = int(callback.data.split("_")[2])
    user_id = callback.from_user.id
    ensure_user(callback.from_user)

    task = next((t for t in admin_tasks_db if t["id"] == task_id), None)
    if not task:
        await safe_answer(callback, "Задание не найдено.")
        return

    # TODO: реальная проверка подписки через bot.get_chat_member
    is_subscribed = True
    if not is_subscribed:
        await safe_answer(callback, "вы не подписались на канал ❗", show_alert=True)
        return

    completed = user_completed_tasks.setdefault(task_id, set())
    if user_id in completed:
        await safe_answer(callback, "Вы уже выполнили это задание!")
        return

    completed.add(user_id)
    user_data_db[user_id]["balance"] += task["reward"]
    task["completions_count"] += 1

    if task["completions_count"] >= task["max_completions"]:
        admin_tasks_db.remove(task)

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
    await safe_answer(callback)
    if admin_tasks_db:
        # переносим текущее задание в конец очереди, а не удаляем
        admin_tasks_db.append(admin_tasks_db.pop(0))
    await show_task(callback)


# ================= ПРОФИЛЬ =================
@router.callback_query(F.data == "menu_profile")
async def profile_handler(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await safe_answer(callback)

    user = callback.from_user
    ensure_user(user)
    stats = user_data_db[user.id]

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
    await state.clear()
    await safe_answer(callback)

    bot_info = await callback.bot.get_me()
    user_id = callback.from_user.id
    referral_link = f"https://t.me/{bot_info.username}?start=ref_{user_id}"

    text = (
        f"За каждого друга, который перейдет по твоей ссылке, ты получаешь +{global_stats['referral_reward']}⭐️!\n\n"
        f"🔗 Твоя реферальная ссылка:\n"
        f"{referral_link}\n\n"
        f"🎉 Приглашай по этой ссылке своих друзей, отправляй её во все чаты и зарабатывай Звёзды!"
    )
    await replace_with_photo(callback, REFERRAL_PHOTO_URL, text, back_markup("back_to_main"))


# ================= ТОП =================
@router.callback_query(F.data == "menu_top")
async def top_handler(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await safe_answer(callback)

    user_id = callback.from_user.id
    ensure_user(callback.from_user)
    sorted_users = sorted(user_data_db.items(), key=lambda item: item[1]["referrals"], reverse=True)

    top_lines = []
    medals = ["🥇", "🥈", "🥉", "✨", "✨"]
    for index, (u_id, data) in enumerate(sorted_users[:5]):
        if data["referrals"] > 0:
            prefix = medals[index] if index < len(medals) else "✨"
            top_lines.append(f"{prefix} {data['name']} | Рефералов: {data['referrals']}")

    if not top_lines:
        top_lines.append("📉 Список лидеров пока пуст.")

    current_user_refs = user_data_db.get(user_id, {}).get("referrals", 0)
    user_in_top = any(u_id == user_id and d["referrals"] > 0 for u_id, d in sorted_users[:5])

    if user_in_top:
        footer = "🏆 Ты входишь в топ рефералов!"
    else:
        footer = f"🚫 Ты не попал в топ! | {current_user_refs} рефералов."

    text = "Топ 5 рефералов за месяц:\n" + "\n".join(top_lines) + f"\n\n{footer}"
    await replace_with_photo(callback, TOP_PHOTO_URL, text, back_markup("back_to_main"))


# ================= ВЫВОД =================
@router.callback_query(F.data == "menu_withdraw")
async def withdraw_handler(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await safe_answer(callback)

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
    withdraw_temp_data[user_id] = {"amount": amount}

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
    ensure_user(callback.from_user)
    user_data = user_data_db[user_id]
    temp_data = withdraw_temp_data.get(user_id, {})

    amount = temp_data.get("amount", 0)
    username = temp_data.get("username", "@unknown")

    if amount <= 0:
        await safe_answer(callback, "❌ Данные заявки потеряны, начните заново.", show_alert=True)
        return

    if user_data["balance"] < amount:
        await safe_answer(callback, "❌ У вас недостаточно средств на балансе!", show_alert=True)
        return

    user_data_db[user_id]["balance"] -= amount
    global_stats["total_withdrawals_stars"] += amount
    await state.clear()
    withdraw_temp_data.pop(user_id, None)
    await safe_answer(callback)

    await replace_message(callback, f"Заявка на вывод {amount} ⭐ успешно выполнена", back_markup("back_to_main"))

    admin_text = (
        "📥 Заявка на вывод\n"
        f"Получатель: {username}\n"
        f"Количество: {amount}⭐"
    )
    admin_markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Выплатил ✅", callback_data="admin_pay_yes"),
                InlineKeyboardButton(text="Не одобрена ❌", callback_data="admin_pay_no"),
            ]
        ]
    )
    try:
        await callback.bot.send_message(chat_id=ADMIN_ID, text=admin_text, reply_markup=admin_markup)
    except Exception:
        pass


@router.callback_query(F.data.in_({"admin_pay_yes", "admin_pay_no"}))
async def admin_action_handler(callback: CallbackQuery) -> None:
    if callback.from_user.id != ADMIN_ID:
        await safe_answer(callback, "Доступ запрещен", show_alert=True)
        return
    await safe_answer(callback)
    action_text = "✅ Выплата отмечена как выполненная." if callback.data == "admin_pay_yes" else "❌ Заявка отклонена."
    try:
        await callback.message.edit_text(f"{callback.message.text}\n\nСтатус: {action_text}")
    except Exception:
        pass


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
    await state.clear()
    await safe_answer(callback)

    now = datetime.now()
    total_users = len(user_data_db)
    u_24h = sum(1 for u in user_data_db.values() if now - u["join_date"] <= timedelta(hours=24))
    u_7d = sum(1 for u in user_data_db.values() if now - u["join_date"] <= timedelta(days=7))
    u_30d = sum(1 for u in user_data_db.values() if now - u["join_date"] <= timedelta(days=30))

    sponsors_count = global_stats["total_sponsors_completed"]
    income_rub = sponsors_count * 1.0
    withdrawals = global_stats["total_withdrawals_stars"]

    text = (
        "панель админа:\n"
        f"всего пользователей: {total_users}\n"
        f"пользователей за 24h: {u_24h}\n"
        f"пользователей за 7 дней: {u_7d}\n"
        f"пользователей за 30 дней: {u_30d}\n"
        f"пользователей подписавшись на спонсоров: {sponsors_count}\n"
        f"доход: {income_rub} руб\n"
        f"выводы: {withdrawals} звезд"
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

    text = f"пользователей увидят рассылку: {len(user_data_db)}\n\nвведите текст рассылки"
    await replace_message(callback, text, back_markup("admin_panel"))


@router.callback_query(F.data == "admin_bc_send")
async def admin_bc_send(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.from_user.id != ADMIN_ID:
        await safe_answer(callback, "Доступ запрещен", show_alert=True)
        return
    data = await state.get_data()
    bc_text = data.get("broadcast_text", "")
    await state.clear()
    await safe_answer(callback)

    sent_count = 0
    for uid in list(user_data_db.keys()):
        try:
            await callback.bot.send_message(chat_id=uid, text=bc_text)
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

    keyboard_rows = []
    if not admin_tasks_db:
        text = "У вас нет активных заданий."
    else:
        text = "Активные задания (нажмите, чтобы удалить):"
        for t in admin_tasks_db:
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
    global admin_tasks_db
    admin_tasks_db = [t for t in admin_tasks_db if t["id"] != task_id]
    await safe_answer(callback, "Задание удалено!")
    await admin_tasks_menu(callback, state)


@router.callback_query(F.data == "admin_task_pub")
async def admin_task_pub(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.from_user.id != ADMIN_ID:
        await safe_answer(callback, "Доступ запрещен", show_alert=True)
        return
    data = await state.get_data()
    await state.clear()
    await safe_answer(callback)

    next_id = (max((t["id"] for t in admin_tasks_db), default=0)) + 1
    admin_tasks_db.append({
        "id": next_id,
        "text": data.get("task_text"),
        "link": data.get("task_link"),
        "reward": data.get("task_reward"),
        "max_completions": data.get("task_max_completions"),
        "completions_count": 0,
    })
    await admin_panel_handler(callback, state)


@router.callback_query(F.data == "admin_promo_menu")
async def admin_promo_menu(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.from_user.id != ADMIN_ID:
        await safe_answer(callback, "Доступ запрещен", show_alert=True)
        return
    await state.clear()
    await safe_answer(callback)

    if not promocodes_db:
        text = "у вас нету активных промокодов 🎁"
        markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="Создать", callback_data="admin_promo_create")],
                [InlineKeyboardButton(text="Назад", callback_data="admin_panel")],
            ]
        )
    else:
        keyboard_rows = []
        for code in promocodes_db:
            keyboard_rows.append([
                InlineKeyboardButton(text=f"🗑 Удалить: {code}", callback_data=f"admin_del_promo_{code}")
            ])

        active_count = len(promocodes_db)
        _, p = list(promocodes_db.items())[-1]
        text = (
            f"{active_count} активный промокод ⭐\n"
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
    if code in promocodes_db:
        del promocodes_db[code]
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

    text = f"цена за реферала {global_stats['referral_reward']} ⭐"
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
        global_stats["referral_reward"] = new_val
    await state.clear()
    await admin_reward_menu(callback, state)


@router.callback_query(F.data == "back_to_main")
async def back_to_main_handler(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await safe_answer(callback)
    await send_main_menu(callback)


# ================= ПРОВЕРКА ПОДПИСКИ =================
@router.callback_query(F.data == "check_all_tasks")
async def check_all_tasks_handler(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id
    ensure_user(callback.from_user)
    tasks_data = user_active_tasks.get(user_id)

    if not tasks_data:
        # На Vercel память могла сброситься — просто заново запрашиваем спонсоров
        await safe_answer(callback)
        await show_sponsors(callback.message, callback.from_user)
        return

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as session:
        # 1. Tgrass
        tg_offers = tasks_data.get("tgrass", [])
        if tg_offers:
            payload = {
                "tg_user_id": int(user_id),
                "tg_login": callback.from_user.username,
                "lang": callback.from_user.language_code or "ru",
                "is_premium": bool(callback.from_user.is_premium),
            }
            _, res, _ = await api_post(session, "tgrass-check", f"{TGRASS_API_URL}/offers", payload, tgrass_headers())
            if isinstance(res, dict):
                if res.get("status") == "ok":
                    tasks_data["tgrass"] = []
                else:
                    offers = res.get("offers", []) or []
                    active_links = {o["link"] for o in offers if not o.get("subscribed", True)}
                    tasks_data["tgrass"] = [o for o in tg_offers if o["link"] in active_links]

        # 2. Piarflow
        pf_links = tasks_data.get("piarflow", [])
        if pf_links:
            links_only = [item["link"] for item in pf_links]
            _, res, _ = await api_post(
                session, "piarflow-check", f"{PIARFLOW_API_URL}/sponsors/check",
                {"user_id": user_id, "links": links_only}, piarflow_headers(),
            )
            if isinstance(res, dict):
                sponsors = res.get("sponsors", []) or []
                subscribed_links = {s["link"] for s in sponsors if s.get("status") == "subscribed"}
                tasks_data["piarflow"] = [item for item in pf_links if item["link"] not in subscribed_links]

        # 3. Trafsly
        tf_sponsors = tasks_data.get("trafsly", [])
        if tf_sponsors:
            remaining_trafsly = []
            for sponsor in tf_sponsors:
                _, res, _ = await api_post(
                    session, "trafsly-check", f"{TRAFSLY_API_URL}/api/v1/confirm-subscription",
                    {"user_id": user_id, "ads_id": sponsor.get("ads_id")}, trafsly_headers(),
                )
                if not (isinstance(res, dict) and res.get("subscribed", False)):
                    remaining_trafsly.append(sponsor)
            tasks_data["trafsly"] = remaining_trafsly

    left_tgrass = tasks_data.get("tgrass", [])
    left_piarflow = tasks_data.get("piarflow", [])
    left_trafsly = tasks_data.get("trafsly", [])

    if not left_tgrass and not left_piarflow and not left_trafsly:
        user_active_tasks.pop(user_id, None)
        if user_id not in fully_passed_users:
            global_stats["total_sponsors_completed"] += 1
        fully_passed_users.add(user_id)

        await reward_referrer_if_needed(callback.bot, user_id)
        await safe_answer(callback)
        await send_main_menu(callback)  # удалит старое сообщение сам
    else:
        all_links = (
            [o["link"] for o in left_tgrass]
            + [i["link"] for i in left_piarflow]
            + [s["link"] for s in left_trafsly]
        )
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
    try:
        update = Update.model_validate(json_data, context={"bot": bot})
        await dp.feed_update(bot, update)
    except Exception as e:
        print(f"Webhook error: {e!r}")
    finally:
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
