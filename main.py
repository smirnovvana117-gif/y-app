import os
import asyncio
import aiohttp
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandStart, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Message,
    Update,
)
from flask import Flask, request

BOT_TOKEN = "8707492747:AAFdthVShZttIT44JeKU2kcebr_xUDJhHHE"
ADMIN_ID = 619112100  # Ваш ID администратора

# Настройки сервисов
PIARFLOW_API_KEY = "FNDvblihCM-RXnMX1U_UrErt_KuD8ujv"
PIARFLOW_API_URL = "https://piarflow.com/v1"

TRAFSLY_API_KEY = "at_99ddd0618296db80b905c92ff9adaa8c"
TRAFSLY_API_URL = "https://api.trafsly.com"

TGRASS_API_KEY = "6d6e9e9701e7440a81fe099d360710d5"
TGRASS_API_URL = "https://tgrass.space"

# Изображения
PHOTO_URL = "https://i.postimg.cc/XvPS1pB2/5883351235946024467.jpg"
REFERRAL_PHOTO_URL = "https://i.postimg.cc/TYbwVvFM/6219881422739149359.jpg"
PROFILE_PHOTO_URL = "https://i.postimg.cc/bvcFZkbf/6219881422739149361.jpg"
TOP_PHOTO_URL = "https://i.postimg.cc/tJfCWpFM/6219881422739149363.jpg"
WITHDRAW_PHOTO_URL = "https://i.postimg.cc/RVdQKYYf/6219881422739149362.jpg"

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
router = Router()

# Flask приложение для Vercel (требуется переменная app)
app = Flask(__name__)

# Состояния для FSM
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

# Хранилища данных
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
    "referral_reward": 4.5
}

TARGET_COUNT = 10

CIS_PHONE_PREFIXES = (
    "+7", "+375", "+380", "+998", "+996", 
    "+374", "+994", "+373", "+992", "+993", "+995"
)


async def fetch_tgrass(session, user_id, user_data, limit):
    try:
        payload = {
            "tg_user_id": int(user_id),
            "tg_login": user_data.get("username"),
            "lang": user_data.get("language_code") or "ru",
            "is_premium": bool(user_data.get("is_premium", False)),
            "offers_limit": limit
        }
        async with session.post(
            f"{TGRASS_API_URL}/offers",
            json=payload,
            headers={"Auth": TGRASS_API_KEY, "Content-Type": "application/json", "accept": "application/json"}
        ) as resp:
            data = await resp.json(content_type=None)
            if data.get("status") == "ok":
                return []
            offers = data.get("offers", [])
            return [o for o in offers if not o.get("subscribed", True)][:limit]
    except:
        return []


async def fetch_piarflow(session, user_id, chat_id, limit):
    try:
        async with session.post(
            f"{PIARFLOW_API_URL}/sponsors",
            json={"user_id": user_id, "chat_id": chat_id, "max_sponsors": limit},
            headers={"Authorization": f"Bearer {PIARFLOW_API_KEY}", "Content-Type": "application/json"}
        ) as resp:
            data = await resp.json(content_type=None)
            sponsors = data.get("sponsors", [])
            unsubscribed = [{"link": s["link"], "type": "piarflow"} for s in sponsors if s.get("status") == "unsubscribed"]
            return unsubscribed[:limit]
    except:
        return []


async def fetch_trafsly(session, user_id, chat_id, user_data, limit):
    try:
        payload = {
            "user_id": user_id,
            "chat_id": chat_id,
            "first_name": user_data.get("first_name", ""),
            "username": user_data.get("username", ""),
            "language_code": user_data.get("language_code", "ru"),
            "is_premium": bool(user_data.get("is_premium", False)),
            "max_sponsors": limit,
        }
        async with session.post(
            f"{TRAFSLY_API_URL}/api/v1/get-sponsors",
            json=payload,
            headers={"Auth": TRAFSLY_API_KEY, "Content-Type": "application/json"}
        ) as resp:
            data = await resp.json(content_type=None)
            sponsors = data.get("sponsors", [])
            return [{"link": s["link"], "ads_id": s["ads_id"], "type": "trafsly"} for s in sponsors][:limit]
    except:
        return []


@router.message(CommandStart())
async def start_handler(message: Message, command: CommandObject) -> None:
    user_id = message.from_user.id
    name = message.from_user.first_name
    username = message.from_user.username
    username_str = f"@{username}" if username else f"id{user_id}"
    now = datetime.now()

    if user_id not in user_data_db:
        user_data_db[user_id] = {
            "name": name,
            "username": username_str,
            "balance": 0.00,
            "referrals": 0,
            "join_date": now
        }
    else:
        user_data_db[user_id]["name"] = name
        user_data_db[user_id]["username"] = username_str

    args = command.args
    if args and args.startswith("ref_"):
        try:
            referrer_id = int(args.split("_")[1])
            if referrer_id != user_id and user_id not in pending_referrals:
                pending_referrals[user_id] = referrer_id
                if referrer_id in user_data_db:
                    ref_msg = (
                        f"реферал: {username_str}\n"
                        f"доход: 0.00 ⭐\n"
                        f"как только реферал подпишется на спонсоров, вы получите 4.5⭐"
                    )
                    try:
                        await message.bot.send_message(chat_id=referrer_id, text=ref_msg)
                    except:
                        pass
        except ValueError:
            pass

    if user_id in fully_passed_users:
        await send_main_menu(message)
        return

    if user_id in verified_users:
        async with aiohttp.ClientSession() as session:
            await show_sponsors(message, session)
        return

    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Пройти проверку 🤝", request_contact=True)]
        ],
        resize_keyboard=True,
        one_time_keyboard=True
    )
    
    text = (
        "Пройти проверку на бота 👇🏻\n\n"
        "Чтобы избежать массовой накрутки со стороны нечестных пользователей, мы вынуждены установить данную проверку. 🤝\n\n"
        "Спасибо за понимание! 🫂"
    )
    await message.answer(text, reply_markup=keyboard)


@router.message(F.contact)
async def contact_handler(message: Message) -> None:
    user_id = message.from_user.id
    contact = message.contact

    if contact.user_id != user_id:
        await message.answer("⚠️ Пожалуйста, поделитесь именно вашим контактом с помощью кнопки ниже.")
        return

    phone_number = contact.phone_number
    if not phone_number.startswith("+"):
        phone_number = "+" + phone_number

    is_cis = any(phone_number.startswith(prefix) for prefix in CIS_PHONE_PREFIXES)

    if not is_cis:
        await message.answer(
            "❌ Доступ разрешен только для пользователей из стран СНГ.",
            reply_markup=ReplyKeyboardRemove()
        )
        return

    verified_users.add(user_id)
    await message.answer("✅ Проверка номера успешно пройдена!", reply_markup=ReplyKeyboardRemove())
    
    async with aiohttp.ClientSession() as session:
        await show_sponsors(message, session)


@router.message()
async def any_message_handler(message: Message, state: FSMContext) -> None:
    current_state = await state.get_state()
    user_id = message.from_user.id
    text_msg = message.text.strip() if message.text else ""

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
        
        markup = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="menu_withdraw")]]
        )
        await message.answer("Введите @username получателя:", reply_markup=markup)
        return

    if current_state == WithdrawStates.waiting_for_username.state:
        username = text_msg
        if not username.startswith("@"):
            username = "@" + username
            
        if user_id not in withdraw_temp_data:
            withdraw_temp_data[user_id] = {}
        withdraw_temp_data[user_id]["username"] = username
        
        amount = withdraw_temp_data[user_id]["amount"]
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
                    InlineKeyboardButton(text="Назад", callback_data="menu_withdraw")
                ]
            ]
        )
        await message.answer(text, reply_markup=markup)
        return

    if current_state == PromocodeStates.waiting_for_activation_input.state:
        code = text_msg
        markup = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_main")]]
        )
        
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

    if current_state == AdminBroadcastStates.waiting_for_text.state and user_id == ADMIN_ID:
        await state.update_data(broadcast_text=text_msg)
        await state.set_state(AdminBroadcastStates.confirm)
        
        preview = f"**текст рассылки:**\n\n{text_msg}"
        count_users = len(user_data_db)
        text_out = f"пользователей увидят рассылку: {count_users}\n\n{preview}"
        
        markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="да, отправить✅", callback_data="admin_bc_send"),
                    InlineKeyboardButton(text="изменить", callback_data="admin_bc_edit")
                ],
                [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_panel")]
            ]
        )
        await message.answer(text_out, reply_markup=markup, parse_mode="Markdown")
        return

    if user_id == ADMIN_ID:
        if current_state == AdminTaskStates.text.state:
            await state.update_data(task_text=text_msg)
            await state.set_state(AdminTaskStates.link)
            markup = InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_panel")]]
            )
            await message.answer("отправьте ссылку на спонсора", reply_markup=markup)
            return

        if current_state == AdminTaskStates.link.state:
            await state.update_data(task_link=text_msg)
            await state.set_state(AdminTaskStates.reward)
            markup = InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_panel")]]
            )
            await message.answer("введите вознаграждение", reply_markup=markup)
            return

        if current_state == AdminTaskStates.reward.state:
            try:
                reward_val = float(text_msg.replace(",", "."))
            except ValueError:
                await message.answer("❌ Введите корректное число для вознаграждения:")
                return
            await state.update_data(task_reward=reward_val)
            await state.set_state(AdminTaskStates.max_completions)
            markup = InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_panel")]]
            )
            await message.answer("введите лимит выполнений (целое число):", reply_markup=markup)
            return

        if current_state == AdminTaskStates.max_completions.state:
            if not text_msg.isdigit():
                await message.answer("❌ Введите целое число для лимита выполнений:")
                return
            await state.update_data(task_max_completions=int(text_msg))
            await state.set_state(AdminTaskStates.preview)
            
            data = await state.get_data()
            t_link = data.get("task_link")
            reward_val = data.get("task_reward")
            max_comp = data.get("task_max_completions")
            
            preview_text = (
                f"🎯 Доступно задание №{len(admin_tasks_db) + 1}!\n"
                f"- Подпишитесь на канал —>\n {t_link}\n-\n"
                f"Награда: {reward_val}⭐\n"
                f"Лимит выполнений: {max_comp}"
            )
            markup = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(text="опубликовать", callback_data="admin_task_pub"),
                        InlineKeyboardButton(text="отмена", callback_data="admin_panel")
                    ]
                ]
            )
            await message.answer(preview_text, reply_markup=markup)
            return

        if current_state == PromocodeStates.admin_name.state:
            await state.update_data(p_name=text_msg)
            await state.set_state(PromocodeStates.admin_activations)
            markup = InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="Назад", callback_data="admin_panel")]]
            )
            await message.answer("введите количество активаций:", reply_markup=markup)
            return

        if current_state == PromocodeStates.admin_activations.state:
            if not text_msg.isdigit():
                await message.answer("❌ Введите число:")
                return
            await state.update_data(p_activations=int(text_msg))
            await state.set_state(PromocodeStates.admin_price)
            markup = InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="Назад", callback_data="admin_panel")]]
            )
            await message.answer("введите цену промокоду:", reply_markup=markup)
            return

        if current_state == PromocodeStates.admin_price.state:
            try:
                price_val = float(text_msg.replace(",", "."))
            except ValueError:
                await message.answer("❌ Введите корректную цену:")
                return
            data = await state.get_data()
            p_name = data.get("p_name")
            p_act = data.get("p_activations")
            
            promocodes_db[p_name] = {
                "name": p_name,
                "activations": p_act,
                "price": price_val,
                "used_by": set()
            }
            await state.clear()
            
            text_out = (
                f"промокод на {p_act}\n"
                f"название: {p_name}\n"
                f"цена: {price_val}"
            )
            markup = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="опубликовать", callback_data="admin_promo_pub")],
                    [InlineKeyboardButton(text="назад", callback_data="admin_promo_menu")]
                ]
            )
            await message.answer(text_out, reply_markup=markup)
            return

        if current_state == AdminRewardStates.waiting_for_new_value.state:
            try:
                new_val = float(text_msg.replace(",", "."))
            except ValueError:
                await message.answer("❌ Введите число:")
                return
            await state.update_data(new_reward=new_val)
            await state.set_state(AdminRewardStates.confirm)
            
            text_out = f"изменить вознаграждение за реферала на {new_val} ⭐"
            markup = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="да, изменить", callback_data="admin_reward_apply")],
                    [InlineKeyboardButton(text="Назад", callback_data="admin_reward_menu")]
                ]
            )
            await message.answer(text_out, reply_markup=markup)
            return

    if user_id not in verified_users:
        await message.answer("⚠️ Пожалуйста, нажмите на кнопку «Пройти проверку 🤝» внизу, чтобы получить доступ к боту.")
        return


async def show_sponsors(message: Message, session: aiohttp.ClientSession):
    user = message.from_user
    user_id = user.id
    chat_id = message.chat.id
    user_data = {
        "first_name": user.first_name,
        "username": user.username,
        "language_code": user.language_code,
        "is_premium": user.is_premium,
    }

    collected_tgrass = []
    collected_piarflow = []
    collected_trafsly = []

    remaining = TARGET_COUNT
    tg_offers = await fetch_tgrass(session, user_id, user_data, remaining)
    collected_tgrass.extend(tg_offers)
    remaining -= len(collected_tgrass)

    if remaining > 0:
        pf_links = await fetch_piarflow(session, user_id, chat_id, remaining)
        collected_piarflow.extend(pf_links)
        remaining -= len(collected_piarflow)

    if remaining > 0:
        tf_sponsors = await fetch_trafsly(session, user_id, chat_id, user_data, remaining)
        collected_trafsly.extend(tf_sponsors)

    user_active_tasks[user_id] = {
        "tgrass": collected_tgrass,
        "piarflow": collected_piarflow,
        "trafsly": collected_trafsly,
    }

    all_links = []
    for offer in collected_tgrass:
        all_links.append(offer["link"])
    for item in collected_piarflow:
        all_links.append(item["link"])
    for sponsor in collected_trafsly:
        all_links.append(sponsor["link"])

    buttons = []
    temp_row = []
    for link in all_links:
        temp_row.append(InlineKeyboardButton(text="✉️ Подписаться", url=link))
        if len(temp_row) == 2:
            buttons.append(temp_row)
            temp_row = []
    if temp_row:
        buttons.append(temp_row)

    if not all_links:
        if user_id not in fully_passed_users:
            global_stats["total_sponsors_completed"] += 1
        fully_passed_users.add(user_id)
        if user_id in pending_referrals:
            ref_id = pending_referrals.pop(user_id)
            if ref_id in user_data_db:
                user_data_db[ref_id]["balance"] += global_stats["referral_reward"]
                user_data_db[ref_id]["referrals"] += 1
                
                ref_username = user_data_db[user_id]["username"]
                total_bal = user_data_db[ref_id]["balance"]
                success_ref_msg = (
                    f"реферал: {ref_username}\n"
                    f"подписался на спонсоров ваш доход 4.5 ⭐\n"
                    f"доход: {total_bal:.1f} ⭐"
                )
                try:
                    await bot.send_message(chat_id=ref_id, text=success_ref_msg)
                except:
                    pass

        await send_main_menu(message)
        return

    buttons.append([InlineKeyboardButton(text='Проверить подписку', callback_data="check_all_tasks")])
    markup = InlineKeyboardMarkup(inline_keyboard=buttons)

    text = (
        "🤖 Для использования бота подпишись на каналы ниже\n\n"
        "🔻 После подписки нажимай на кнопку \"Проверить подписку\""
    )
    await message.answer(text, reply_markup=markup)


async def send_main_menu(message_or_callback):
    if isinstance(message_or_callback, CallbackQuery):
        message = message_or_callback.message
        user = message_or_callback.from_user
    else:
        message = message_or_callback
        user = message.from_user

    stats = user_data_db.get(user.id, {"balance": 0.00, "referrals": 0})

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

    main_menu_markup = InlineKeyboardMarkup(inline_keyboard=rows)

    try:
        if isinstance(message_or_callback, CallbackQuery):
            await message_or_callback.message.edit_media(
                media=InputMediaPhoto(media=PHOTO_URL, caption=text),
                reply_markup=main_menu_markup
            )
        else:
            await message.answer_photo(photo=PHOTO_URL, caption=text, reply_markup=main_menu_markup)
    except Exception as e:
        print(f"Ошибка при отправке главного меню: {e}")
        try:
            if isinstance(message_or_callback, CallbackQuery):
                await message_or_callback.message.answer_photo(photo=PHOTO_URL, caption=text, reply_markup=main_menu_markup)
                try:
                    await message_or_callback.message.delete()
                except:
                    pass
            else:
                await message.answer_photo(photo=PHOTO_URL, caption=text, reply_markup=main_menu_markup)
        except Exception as ex:
            print(f"Критическая ошибка главного меню: {ex}")


@router.callback_query(F.data == "menu_task")
async def task_menu_handler(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer()
    
    if not admin_tasks_db:
        text = "нет активных заданий для выполнения"
        markup = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_main")]]
        )
        try:
            await callback.message.answer(text, reply_markup=markup)
            await callback.message.delete()
        except:
            pass
        return

    task = admin_tasks_db[0]
    task_id = task["id"]
    user_id = callback.from_user.id
    
    if task_id not in user_completed_tasks:
        user_completed_tasks[task_id] = set()

    text = (
        f"🎯 Доступно задание №{task_id}!\n\n"
        f"- Подпишитесь на канал —>\n {task['link']}\n-\n"
        f"Награда: {task['reward']}⭐"
    )
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Выполнить", url=task["link"])],
            [InlineKeyboardButton(text="припустить задание", callback_data=f"skip_task_{task_id}")],
            [InlineKeyboardButton(text="✅ Я выполнил", callback_data=f"done_task_{task_id}")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_main")]
        ]
    )
    try:
        await callback.message.edit_caption(caption=text, reply_markup=markup)
    except:
        await callback.message.answer(text, reply_markup=markup)


@router.callback_query(F.data.startswith("done_task_"))
async def done_task_handler(callback: CallbackQuery) -> None:
    task_id = int(callback.data.split("_")[2])
    user_id = callback.from_user.id
    
    task = next((t for t in admin_tasks_db if t["id"] == task_id), None)
    if not task:
        await callback.answer("Задание не найдено.")
        return

    is_subscribed = True 
    if not is_subscribed:
        await callback.answer("вы не подписались на канал ❗", show_alert=True)
        return

    if user_id in user_completed_tasks.get(task_id, set()):
        await callback.answer("Вы уже выполнили это задание!")
        return

    user_completed_tasks[task_id].add(user_id)
    user_data_db[user_id]["balance"] += task["reward"]
    task["completions_count"] += 1

    if task["completions_count"] >= task["max_completions"]:
        admin_tasks_db.remove(task)

    await callback.answer()

    text = f"✅ На ваш баланс было зачислено {task['reward']}⭐"
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="следующие задание", callback_data="menu_task")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_main")]
        ]
    )
    try:
        await callback.message.edit_caption(caption=text, reply_markup=markup)
    except:
        await callback.message.answer(text, reply_markup=markup)


@router.callback_query(F.data.startswith("skip_task_"))
async def skip_task_handler(callback: CallbackQuery) -> None:
    await callback.answer()
    if admin_tasks_db:
        admin_tasks_db.pop(0)
    await task_menu_handler(callback, FSMContext(None, None))


@router.callback_query(F.data == "menu_profile")
async def profile_handler(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer()
    
    user = callback.from_user
    user_id = user.id
    
    stats = user_data_db.get(user_id, {"balance": 0.00, "referrals": 0})
    
    text = (
        f"👤 Имя: {user.first_name}\n"
        f"🆔 ID: {user_id}\n"
        f"💰 Баланс: {stats['balance']:.2f}⭐️\n"
        f"👥 Приглашенно Друзей: {stats['referrals']}"
    )

    profile_markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🆘 Поддержка", url="https://t.me/durovsor")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_main")]
        ]
    )

    try:
        await callback.message.answer_photo(photo=PROFILE_PHOTO_URL, caption=text, reply_markup=profile_markup)
        await callback.message.delete()
    except Exception as e:
        print(f"⚠️ Ошибка отправки фото профиля: {e}")
        try:
            await callback.message.answer(text, reply_markup=profile_markup)
            await callback.message.delete()
        except:
            pass


@router.callback_query(F.data == "menu_referrals")
async def referrals_handler(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer()
    
    bot_info = await callback.bot.get_me()
    user_id = callback.from_user.id
    referral_link = f"https://t.me/{bot_info.username}?start=ref_{user_id}"

    text = (
        f"За каждого друга, который перейдет по твоей ссылке, ты получаешь +{global_stats['referral_reward']}⭐️!\n\n"
        f"🔗 Твоя реферальная ссылка:\n"
        f"{referral_link}\n\n"
        f"🎉 Приглашай по этой ссылке своих друзей, отправляй её во все чаты и зарабатывай Звёзды!"
    )

    referral_markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_main")]
        ]
    )

    try:
        await callback.message.answer_photo(photo=REFERRAL_PHOTO_URL, caption=text, reply_markup=referral_markup)
        await callback.message.delete()
    except:
        try:
            await callback.message.answer(text, reply_markup=referral_markup)
            await callback.message.delete()
        except:
            pass


@router.callback_query(F.data == "menu_top")
async def top_handler(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer()

    user_id = callback.from_user.id
    sorted_users = sorted(
        user_data_db.items(),
        key=lambda item: item[1]["referrals"],
        reverse=True
    )

    top_lines = []
    medals = ["🥇", "🥈", "🥉", "✨", "✨"]
    
    for index, (u_id, data) in enumerate(sorted_users[:5]):
        if data["referrals"] > 0:
            prefix = medals[index] if index < len(medals) else "✨"
            top_lines.append(f"{prefix} {data['name']} | Рефералов: {data['referrals']}")

    if not top_lines:
        top_lines.append("📉 Список лидеров пока пуст.")

    current_user_refs = user_data_db.get(user_id, {}).get("referrals", 0)
    user_in_top = any(u_id == user_id for u_id, _ in sorted_users[:5])

    if user_in_top:
        footer = "🏆 Ты входишь в топ рефералов!"
    else:
        footer = f"🚫 Ты не попал в топ! | {current_user_refs} рефералов."

    text = "Топ 5 рефералов за месяц:\n" + "\n".join(top_lines) + f"\n\n{footer}"
    top_markup = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_main")]]
    )

    try:
        await callback.message.edit_media(
            media=InputMediaPhoto(media=TOP_PHOTO_URL, caption=text),
            reply_markup=top_markup
        )
    except:
        try:
            await callback.message.answer_photo(photo=TOP_PHOTO_URL, caption=text, reply_markup=top_markup)
            await callback.message.delete()
        except:
            pass


@router.callback_query(F.data == "menu_withdraw")
async def withdraw_handler(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer()

    text = "Выбери количество звёзд, которое хочешь вывести:"
    withdraw_markup = InlineKeyboardMarkup(
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

    try:
        await callback.message.edit_media(
            media=InputMediaPhoto(media=WITHDRAW_PHOTO_URL, caption=text),
            reply_markup=withdraw_markup
        )
    except:
        try:
            await callback.message.answer_photo(photo=WITHDRAW_PHOTO_URL, caption=text, reply_markup=withdraw_markup)
            await callback.message.delete()
        except:
            pass


@router.callback_query(F.data.startswith("withdraw_") & ~F.data.in_({"withdraw_custom"}))
async def withdraw_preset_handler(callback: CallbackQuery, state: FSMContext) -> None:
    amount = int(callback.data.split("_")[1])
    user_id = callback.from_user.id
    withdraw_temp_data[user_id] = {"amount": amount}

    await state.set_state(WithdrawStates.waiting_for_username)
    await callback.answer()

    markup = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="menu_withdraw")]]
    )
    await callback.message.answer("Введите @username получателя:", reply_markup=markup)
    try:
        await callback.message.delete()
    except:
        pass


@router.callback_query(F.data == "withdraw_custom")
async def withdraw_custom_handler(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(WithdrawStates.waiting_for_custom_amount)
    await callback.answer()

    markup = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="menu_withdraw")]]
    )
    await callback.message.answer("Введите любое целое число от 50 ⭐", reply_markup=markup)
    try:
        await callback.message.delete()
    except:
        pass


@router.callback_query(F.data == "confirm_withdraw")
async def confirm_withdraw_handler(callback: CallbackQuery, state: FSMContext) -> None:
    user_id = callback.from_user.id
    user_data = user_data_db.get(user_id, {"balance": 0.00, "referrals": 0})
    temp_data = withdraw_temp_data.get(user_id, {})

    amount = temp_data.get("amount", 0)
    username = temp_data.get("username", "@unknown")

    if user_data["balance"] < amount:
        await callback.answer("❌ У вас недостаточно средств на балансе!", show_alert=True)
        return

    user_data_db[user_id]["balance"] -= amount
    global_stats["total_withdrawals_stars"] += amount
    await state.clear()
    await callback.answer()

    success_text = f"Заявка на вывод {amount} ⭐ успешно выполнена"
    markup = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_main")]]
    )

    try:
        await callback.message.answer(success_text, reply_markup=markup)
        await callback.message.delete()
    except:
        pass

    admin_text = (
        "📥 Заявка на вывод\n"
        "Enter\n"
        f"Получатель: {username}\n"
        "enter\n"
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
        await bot.send_message(chat_id=ADMIN_ID, text=admin_text, reply_markup=admin_markup)
    except:
        pass


@router.callback_query(F.data.in_({"admin_pay_yes", "admin_pay_no"}))
async def admin_action_handler(callback: CallbackQuery) -> None:
    await callback.answer()
    action_text = "✅ Выплата отмечена как выполненная." if callback.data == "admin_pay_yes" else "❌ Заявка отклонена."
    try:
        await callback.message.edit_text(f"{callback.message.text}\n\nСтатус: {action_text}")
    except:
        pass


@router.callback_query(F.data == "menu_promocode")
async def promocode_menu_handler(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(PromocodeStates.waiting_for_activation_input)
    await callback.answer()

    text = "🎟 Введите промокод:"
    markup = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_main")]]
    )
    try:
        await callback.message.answer(text, reply_markup=markup)
        await callback.message.delete()
    except:
        pass


# ================= АДМИН ПАНЕЛЬ =================
@router.callback_query(F.data == "admin_panel")
async def admin_panel_handler(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Доступ запрещен", show_alert=True)
        return
    await state.clear()
    await callback.answer()

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
            [
                InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_main")
            ]
        ]
    )

    try:
        await callback.message.edit_caption(caption=text, reply_markup=markup)
    except:
        await callback.message.answer(text, reply_markup=markup)


@router.callback_query(F.data == "admin_broadcast")
async def admin_bc_start(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.from_user.id != ADMIN_ID:
        return
    await state.set_state(AdminBroadcastStates.waiting_for_text)
    await callback.answer()

    count_users = len(user_data_db)
    text = (
        f"пользователей увидят рассылку: {count_users}\n\n"
        "введите текст рассылки"
    )
    markup = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_panel")]]
    )
    try:
        await callback.message.edit_caption(caption=text, reply_markup=markup)
    except:
        await callback.message.answer(text, reply_markup=markup)


@router.callback_query(F.data == "admin_bc_send")
async def admin_bc_send(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    bc_text = data.get("broadcast_text", "")
    await state.clear()
    await callback.answer()

    sent_count = 0
    for uid in user_data_db.keys():
        try:
            await bot.send_message(chat_id=uid, text=bc_text)
            sent_count += 1
        except:
            pass

    text = f"отправлено пользователям: {sent_count}"
    markup = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_panel")]]
    )
    try:
        await callback.message.edit_caption(caption=text, reply_markup=markup)
    except:
        await callback.message.answer(text, reply_markup=markup)


@router.callback_query(F.data == "admin_bc_edit")
async def admin_bc_edit(callback: CallbackQuery, state: FSMContext) -> None:
    await admin_bc_start(callback, state)


@router.callback_query(F.data == "admin_tasks_menu")
async def admin_tasks_menu(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.from_user.id != ADMIN_ID:
        return
    await callback.answer()

    keyboard_rows = []
    if not admin_tasks_db:
        text = "У вас нет активных заданий."
    else:
        text = "Активные задания (нажмите, чтобы удалить):"
        for t in admin_tasks_db:
            keyboard_rows.append([
                InlineKeyboardButton(text=f"❌ Удалить №{t['id']} ({t['link']})", callback_data=f"admin_del_task_{t['id']}")
            ])

    keyboard_rows.append([InlineKeyboardButton(text="➕ Создать задание", callback_data="admin_task_create")])
    keyboard_rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_panel")])
    
    markup = InlineKeyboardMarkup(inline_keyboard=keyboard_rows)
    try:
        await callback.message.edit_caption(caption=text, reply_markup=markup)
    except:
        await callback.message.answer(text, reply_markup=markup)


@router.callback_query(F.data == "admin_task_create")
async def admin_task_create(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminTaskStates.text)
    await callback.answer()
    text = "ввести текст задания"
    markup = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_panel")]]
    )
    try:
        await callback.message.edit_caption(caption=text, reply_markup=markup)
    except:
        await callback.message.answer(text, reply_markup=markup)


@router.callback_query(F.data.startswith("admin_del_task_"))
async def admin_del_task(callback: CallbackQuery, state: FSMContext) -> None:
    task_id = int(callback.data.split("_")[3])
    global admin_tasks_db
    admin_tasks_db = [t for t in admin_tasks_db if t["id"] != task_id]
    await callback.answer("Задание удалено!")
    await admin_tasks_menu(callback, state)


@router.callback_query(F.data == "admin_task_pub")
async def admin_task_pub(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    t_text = data.get("task_text")
    t_link = data.get("task_link")
    t_reward = data.get("task_reward")
    t_max = data.get("task_max_completions")
    await state.clear()
    await callback.answer()

    new_task = {
        "id": len(admin_tasks_db) + 1,
        "text": t_text,
        "link": t_link,
        "reward": t_reward,
        "max_completions": t_max,
        "completions_count": 0
    }
    admin_tasks_db.append(new_task)
    
    await admin_panel_handler(callback, state)


@router.callback_query(F.data == "admin_promo_menu")
async def admin_promo_menu(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.from_user.id != ADMIN_ID:
        return
    await callback.answer()

    if not promocodes_db:
        text = "у вас нету активный промокодов 🎁"
        markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="Создать", callback_data="admin_promo_create")],
                [InlineKeyboardButton(text="Назад", callback_data="admin_panel")]
            ]
        )
    else:
        keyboard_rows = []
        for code, p in promocodes_db.items():
            keyboard_rows.append([
                InlineKeyboardButton(text=f"🗑 Удалить: {code}", callback_data=f"admin_del_promo_{code}")
            ])
        
        active_count = len(promocodes_db)
        code, p = list(promocodes_db.items())[-1]
        text = (
            f"{active_count} активный промокод ⭐\n"
            f"название: {p['name']}\n"
            f"Цена: {p['price']} звезд\n"
            f"кол во активаций: {p['activations']}"
        )
        keyboard_rows.append([InlineKeyboardButton(text="Создать", callback_data="admin_promo_create")])
        keyboard_rows.append([InlineKeyboardButton(text="Назад", callback_data="admin_panel")])
        markup = InlineKeyboardMarkup(inline_keyboard=keyboard_rows)

    try:
        await callback.message.edit_caption(caption=text, reply_markup=markup)
    except:
        await callback.message.answer(text, reply_markup=markup)


@router.callback_query(F.data.startswith("admin_del_promo_"))
async def admin_del_promo(callback: CallbackQuery, state: FSMContext) -> None:
    code = callback.data.replace("admin_del_promo_", "")
    if code in promocodes_db:
        del promocodes_db[code]
        await callback.answer("Промокод удален!")
    await admin_promo_menu(callback, state)


@router.callback_query(F.data == "admin_promo_create")
async def admin_promo_create(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(PromocodeStates.admin_name)
    await callback.answer()

    text = "введите название промокода:"
    markup = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Назад", callback_data="admin_panel")]]
    )
    try:
        await callback.message.edit_caption(caption=text, reply_markup=markup)
    except:
        await callback.message.answer(text, reply_markup=markup)


@router.callback_query(F.data == "admin_promo_pub")
async def admin_promo_pub(callback: CallbackQuery, state: FSMContext) -> None:
    await admin_promo_menu(callback, state)


@router.callback_query(F.data == "admin_reward_menu")
async def admin_reward_menu(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.from_user.id != ADMIN_ID:
        return
    await callback.answer()

    text = (
        f"цена за реферала {global_stats['referral_reward']} ⭐"
    )
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="изменить", callback_data="admin_reward_change"),
                InlineKeyboardButton(text="назад", callback_data="admin_panel")
            ]
        ]
    )
    try:
        await callback.message.edit_caption(caption=text, reply_markup=markup)
    except:
        await callback.message.answer(text, reply_markup=markup)


@router.callback_query(F.data == "admin_reward_change")
async def admin_reward_change(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminRewardStates.waiting_for_new_value)
    await callback.answer()

    text = "ведите количество за реферала"
    markup = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="назад", callback_data="admin_panel")]]
    )
    try:
        await callback.message.edit_caption(caption=text, reply_markup=markup)
    except:
        await callback.message.answer(text, reply_markup=markup)


@router.callback_query(F.data == "admin_reward_apply")
async def admin_reward_apply(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    new_val = data.get("new_reward")
    if new_val is not None:
        global_stats["referral_reward"] = new_val
    await state.clear()
    await admin_reward_menu(callback, state)


@router.callback_query(F.data == "back_to_main")
async def back_to_main_handler(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    print("👉 Нажата кнопка: Назад")
    await callback.answer()
    await send_main_menu(callback)


@router.callback_query(F.data == "check_all_tasks")
async def check_all_tasks_handler(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id
    tasks_data = user_active_tasks.get(user_id)

    if not tasks_data:
        await callback.message.answer("Актуальные задания не найдены. Пожалуйста, введите /start заново.")
        await callback.answer()
        return

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
        # 1. Проверяем Tgrass
        tg_offers = tasks_data.get("tgrass", [])
        if tg_offers:
            try:
                payload = {
                    "tg_user_id": int(user_id),
                    "tg_login": callback.from_user.username,
                    "lang": callback.from_user.language_code or "ru",
                    "is_premium": bool(callback.from_user.is_premium),
                }
                async with session.post(
                    f"{TGRASS_API_URL}/offers",
                    json=payload,
                    headers={"Auth": TGRASS_API_KEY, "Content-Type": "application/json", "accept": "application/json"}
                ) as resp:
                    res = await resp.json(content_type=None)
                    if res.get("status") == "ok":
                        tasks_data["tgrass"] = []
                    else:
                        offers = res.get("offers", [])
                        active_links = {o["link"] for o in offers if not o.get("subscribed", True)}
                        tasks_data["tgrass"] = [o for o in tg_offers if o["link"] in active_links]
            except:
                pass

        # 2. Проверяем Piarflow
        pf_links = tasks_data.get("piarflow", [])
        if pf_links:
            try:
                links_only = [item["link"] for item in pf_links]
                async with session.post(
                    f"{PIARFLOW_API_URL}/sponsors/check",
                    json={"user_id": user_id, "links": links_only},
                    headers={"Authorization": f"Bearer {PIARFLOW_API_KEY}", "Content-Type": "application/json"}
                ) as resp:
                    res = await resp.json(content_type=None)
                    sponsors = res.get("sponsors", [])
                    subscribed_links = {s["link"] for s in sponsors if s.get("status") == "subscribed"}
                    tasks_data["piarflow"] = [item for item in pf_links if item["link"] not in subscribed_links]
            except:
                pass

        # 3. Проверяем Trafsly
        tf_sponsors = tasks_data.get("trafsly", [])
        if tf_sponsors:
            remaining_trafsly = []
            for sponsor in tf_sponsors:
                try:
                    async with session.post(
                        f"{TRAFSLY_API_URL}/api/v1/confirm-subscription",
                        json={"user_id": user_id, "ads_id": sponsor.get("ads_id")},
                        headers={"Auth": TRAFSLY_API_KEY, "Content-Type": "application/json"}
                    ) as resp:
                        res = await resp.json(content_type=None)
                        if not res.get("subscribed", False):
                            remaining_trafsly.append(sponsor)
                except:
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
        
        if user_id in pending_referrals:
            ref_id = pending_referrals.pop(user_id)
            if ref_id in user_data_db:
                user_data_db[ref_id]["balance"] += global_stats["referral_reward"]
                user_data_db[ref_id]["referrals"] += 1
                
                ref_username = user_data_db[user_id]["username"]
                total_bal = user_data_db[ref_id]["balance"]
                success_ref_msg = (
                    f"реферал: {ref_username}\n"
                    f"подписался на спонсоров ваш доход 4.5 ⭐\n"
                    f"доход: {total_bal:.1f} ⭐"
                )
                try:
                    await bot.send_message(chat_id=ref_id, text=success_ref_msg)
                except:
                    pass

        try:
            await callback.message.delete()
        except:
            pass
        
        await send_main_menu(callback)
    else:
        all_links = []
        for offer in left_tgrass:
            all_links.append(offer["link"])
        for item in left_piarflow:
            all_links.append(item["link"])
        for sponsor in left_trafsly:
            all_links.append(sponsor["link"])

        buttons = []
        temp_row = []
        for link in all_links:
            temp_row.append(InlineKeyboardButton(text="✉️ Подписаться", url=link))
            if len(temp_row) == 2:
                buttons.append(temp_row)
                temp_row = []
        if temp_row:
            buttons.append(temp_row)

        buttons.append([InlineKeyboardButton(text='Проверить подписку', callback_data="check_all_tasks")])
        markup = InlineKeyboardMarkup(inline_keyboard=buttons)

        try:
            await callback.message.edit_reply_markup(reply_markup=markup)
        except:
            pass
        await callback.answer("❌ Вы подписались не на все каналы. Проверьте оставшиеся подписки.")

    await callback.answer()


# Регистрация роутера в диспетчере
dp.include_router(router)


# Webhook роут для Vercel
@app.route("/", methods=["POST"])
def webhook():
    if request.headers.get("content-type") == "application/json":
        json_data = request.get_json(force=True)
        update = Update.de_json(json_data, bot)
        asyncio.run(dp.feed_update(bot, update))
        return "", 200
    return "OK", 200


@app.route("/", methods=["GET"])
def index():
    return "Bot is running on Vercel!", 200
