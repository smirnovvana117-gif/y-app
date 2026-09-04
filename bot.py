import asyncio
import json
import logging
import os
from contextlib import suppress

from aiocryptopay import AioCryptoPay, Networks
from aiogram import Bot, Dispatcher, F, types
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice, PreCheckoutQuery

# ==================== НАСТРОЙКИ (ВСТАВЛЯТЬ СЮДА) ====================
BOT_TOKEN = "8907718308:AAFa1AAPnOIsPYKrBPUPeqCnIFpR_04UuWc"  # Токен вашего бота из @BotFather
CRYPTO_PAY_TOKEN = "626375:AARpLhqDWfhdoJ6QaVT7bhbBLD6qdUQG0Ux"  # API Token из @CryptoBot
ADMIN_ID = 356334220  # Ваш Telegram ID из @userinfobot
ADMIN_USERNAME = "koshikton"  # Ваш юзернейм без @
STARS_RATE = 1  # Курс: 1 Telegram Star = 1 Рубль
REF_PERCENT = 10  # Реферальный процент (10%)
MIN_WITHDRAWAL_RUB = 150  # Минимальная сумма вывода
# ====================================================================

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

crypto: AioCryptoPay = None

# --- Работа с хранением данных (JSON) ---
PRODUCTS_FILE = "products.json"


def load_products():
    if os.path.exists(PRODUCTS_FILE):
        try:
            with open(PRODUCTS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return {int(k): v for k, v in data.items()}
        except Exception as e:
            logging.error(f"Ошибка чтения products.json: {e}")
            return {}
    return {}


def save_products(products_data):
    try:
        with open(PRODUCTS_FILE, "w", encoding="utf-8") as f:
            json.dump(products_data, f, ensure_ascii=False, indent=4)
    except Exception as e:
        logging.error(f"Ошибка сохранения products.json: {e}")


# Загружаем товары из файла
products = load_products()
users = {}


# --- Состояния ---
class AddProduct(StatesGroup):
    name = State()
    price = State()
    content = State()


class DuplicateProduct(StatesGroup):
    waiting_for_content = State()


class WithdrawStates(StatesGroup):
    choosing_method = State()
    entering_details = State()


# --- Вспомогательные функции ---
def get_user(user_id: int):
    if user_id not in users:
        users[user_id] = {
            "referrer": None,
            "balance": 0.0,
            "ref_count": 0,
        }
    return users[user_id]


def format_user_mention(user: types.User) -> str:
    if user.username:
        return f"@{user.username} (ID: `{user.id}`)"
    return f"[{user.full_name}](tg://user?id={user.id}) (ID: `{user.id}`)"


# --- Клавиатуры ---
def main_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🛒 Купить", callback_data="user_buy")],
            [InlineKeyboardButton(text="💰 Продать", url=f"https://t.me/{ADMIN_USERNAME}")],
            [InlineKeyboardButton(text="👥 Реферальная система", callback_data="user_ref")],
            [InlineKeyboardButton(text="👤 Профиль", callback_data="user_profile")],
            [InlineKeyboardButton(text="💬 Поддержка", url=f"https://t.me/{ADMIN_USERNAME}")],
        ]
    )


def products_keyboard():
    kb = []
    for p_id, p_data in products.items():
        kb.append(
            [
                InlineKeyboardButton(
                    text=f"{p_data['name']} — {p_data['price']}₽",
                    callback_data=f"buy_prod_{p_id}",
                )
            ]
        )
    kb.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="to_main")])
    return InlineKeyboardMarkup(inline_keyboard=kb)


def payment_methods_keyboard(p_id):
    product = products.get(p_id)
    stars_price = max(1, round(product["price"] / STARS_RATE)) if product else 1
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🤖 Crypto Bot (TON / RUB)",
                    callback_data=f"pay_crypto_{p_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text=f"⭐️ Telegram Stars ({stars_price} XTR)",
                    callback_data=f"pay_stars_{p_id}",
                )
            ],
            [InlineKeyboardButton(text="💳 Карта", callback_data=f"pay_card_{p_id}")],
            [InlineKeyboardButton(text="⬅️ Назад к товарам", callback_data="user_buy")],
        ]
    )


def admin_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ Добавить товар", callback_data="adm_add")],
            [InlineKeyboardButton(text="🗑 Удалить товар", callback_data="adm_del")],
            [InlineKeyboardButton(text="📋 Список товаров", callback_data="adm_list")],
        ]
    )


# --- Пользовательский функционал ---
@dp.message(CommandStart())
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    user_data = get_user(user_id)

    args = message.text.split()
    if len(args) > 1 and args[1].startswith("ref_"):
        with suppress(ValueError):
            ref_id = int(args[1].replace("ref_", ""))
            if ref_id != user_id and user_data["referrer"] is None and ref_id in users:
                user_data["referrer"] = ref_id
                users[ref_id]["ref_count"] += 1
                with suppress(Exception):
                    await bot.send_message(
                        ref_id,
                        f"🎉 По вашей ссылке зарегистрировался новый пользователь {format_user_mention(message.from_user)}!",
                        parse_mode="Markdown",
                    )

    await message.answer("👋 Выберите нужный раздел:", reply_markup=main_keyboard())


@dp.callback_query(F.data == "to_main")
async def back_main(call: types.CallbackQuery, state: FSMContext):
    await state.clear()
    with suppress(TelegramBadRequest, TelegramForbiddenError):
        await call.message.edit_text("👋 Главное меню:", reply_markup=main_keyboard())
    await call.answer()


@dp.callback_query(F.data == "user_buy")
async def show_buy(call: types.CallbackQuery):
    if not products:
        with suppress(TelegramBadRequest, TelegramForbiddenError):
            await call.message.edit_text(
                "🛒 Товаров пока нет в наличии.", reply_markup=main_keyboard()
            )
        await call.answer()
        return

    with suppress(TelegramBadRequest, TelegramForbiddenError):
        await call.message.edit_text(
            "🛒 Выберите товар:", reply_markup=products_keyboard()
        )
    await call.answer()


@dp.callback_query(F.data.startswith("buy_prod_"))
async def process_buy(call: types.CallbackQuery):
    p_id = int(call.data.split("_")[2])
    product = products.get(p_id)
    if product:
        with suppress(TelegramBadRequest, TelegramForbiddenError):
            await call.message.edit_text(
                f"🛒 Товар: {product['name']}\n"
                f"💰 Цена: {product['price']} ₽\n\n"
                f"Выберите удобный способ оплаты:",
                reply_markup=payment_methods_keyboard(p_id),
            )
    else:
        await call.answer("Товар не найден или уже продан!", show_alert=True)
    await call.answer()


# --- РЕФЕРАЛЬНАЯ СИСТЕМА ---
@dp.callback_query(F.data == "user_ref")
async def show_ref_system(call: types.CallbackQuery):
    user_id = call.from_user.id
    u_data = get_user(user_id)
    bot_user = await bot.get_me()
    ref_link = f"https://t.me/{bot_user.username}?start=ref_{user_id}"

    text = (
        f"👥 **Реферальная программа (10%)**\n\n"
        f"Приглашайте друзей и получайте **10%** с каждой их покупки в боте!\n\n"
        f"🔗 Ваша реферальная ссылка:\n`{ref_link}`\n\n"
        f"📊 Начислено бонусов: `{u_data['balance']:.2f}`₽\n"
        f"👥 Всего рефералов: `{u_data['ref_count']}`\n\n"
        f"⚠️ *Вывод средств осуществляется в USDT (от {MIN_WITHDRAWAL_RUB}₽)*"
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💸 Вывести средства (USDT)", callback_data="user_withdraw")],
            [InlineKeyboardButton(text="⬅️ Главное меню", callback_data="to_main")],
        ]
    )
    with suppress(TelegramBadRequest, TelegramForbiddenError):
        await call.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
    await call.answer()


@dp.callback_query(F.data == "user_withdraw")
async def start_withdraw(call: types.CallbackQuery, state: FSMContext):
    user_id = call.from_user.id
    u_data = get_user(user_id)

    if u_data["balance"] < MIN_WITHDRAWAL_RUB:
        await call.answer(
            f"❌ Минимальная сумма вывода — {MIN_WITHDRAWAL_RUB}₽. У вас на балансе {u_data['balance']:.2f}₽.",
            show_alert=True,
        )
        return

    text = (
        f"💸 **Вывод реферального баланса в USDT**\n\n"
        f"💰 Доступно к выводу: `{u_data['balance']:.2f}`₽\n"
        f"Выберите удобный способ получения **USDT**:"
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🤖 Чек Crypto Bot (USDT)", callback_data="withdraw_cryptobot")],
            [InlineKeyboardButton(text="💵 USDT Кошелек (TRC20 / TON)", callback_data="withdraw_usdt_address")],
            [InlineKeyboardButton(text="⬅️ Назад в реф. меню", callback_data="user_ref")],
        ]
    )
    await state.set_state(WithdrawStates.choosing_method)
    with suppress(TelegramBadRequest, TelegramForbiddenError):
        await call.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
    await call.answer()


@dp.callback_query(WithdrawStates.choosing_method, F.data.startswith("withdraw_"))
async def process_withdraw_method(call: types.CallbackQuery, state: FSMContext):
    method = call.data.replace("withdraw_", "")
    await state.update_data(withdraw_method=method)

    if method == "cryptobot":
        text = "🤖 **Вывод через Crypto Bot (USDT)**\n\nПришлите ваш Telegram Юзернейм или ID."
    else:
        text = "💵 **Вывод на USDT Кошелек**\n\nПришлите адрес вашего USDT TRC20 / TON кошелька."

    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="user_ref")]])
    await state.set_state(WithdrawStates.entering_details)
    with suppress(TelegramBadRequest, TelegramForbiddenError):
        await call.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
    await call.answer()


@dp.message(WithdrawStates.entering_details)
async def process_withdraw_details(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    u_data = get_user(user_id)

    if u_data["balance"] < MIN_WITHDRAWAL_RUB:
        await message.answer("❌ Недостаточно средств.")
        await state.clear()
        return

    data = await state.get_data()
    method = data.get("withdraw_method")
    details = message.text.strip()
    amount_rub = u_data["balance"]

    u_data["balance"] = 0.0
    await state.clear()

    method_title = "Crypto Bot (USDT Чек)" if method == "cryptobot" else "USDT Кошелек"

    await message.answer(
        f"✅ **Заявка на вывод создана!**\n\n"
        f"💰 Сумма: `{amount_rub:.2f}`₽ *(выплата в эквиваленте USDT)*\n"
        f"📍 Способ: **{method_title}**\n"
        f"📝 Реквизиты: `{details}`\n\n"
        f"Заявка передана администратору.",
        reply_markup=main_keyboard(),
        parse_mode="Markdown",
    )

    admin_msg = (
        f"📥 **НОВАЯ ЗАЯВКА НА ВЫВОД (USDT)!**\n\n"
        f"👤 **Пользователь:** {format_user_mention(message.from_user)}\n"
        f"💰 **Сумма к выплате:** `{amount_rub:.2f}`₽\n"
        f"📍 **Способ:** {method_title}\n"
        f"📝 **Реквизиты:** `{details}`"
    )
    with suppress(Exception):
        await bot.send_message(ADMIN_ID, admin_msg, parse_mode="Markdown")


# --- Обработка оплаты через Crypto Bot ---
@dp.callback_query(F.data.startswith("pay_crypto_"))
async def pay_crypto(call: types.CallbackQuery):
    p_id = int(call.data.split("_")[2])
    product = products.get(p_id)

    if not product:
        await call.answer("Товар не найден или уже продан!", show_alert=True)
        return

    try:
        invoice = await crypto.create_invoice(
            amount=float(product["price"]),
            currency_type="fiat",
            fiat="RUB",
        )

        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="🔗 Оплатить через Crypto Bot",
                        url=invoice.bot_invoice_url,
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="✅ Проверить оплату",
                        callback_data=f"check_{invoice.invoice_id}_{p_id}",
                    )
                ],
                [InlineKeyboardButton(text="⬅️ Отмена", callback_data="user_buy")],
            ]
        )

        await call.message.edit_text(
            f"🧾 Счет на оплату создан!\n\n"
            f"📦 Товар: {product['name']}\n"
            f"💵 Сумма: {product['price']} ₽\n\n"
            f"1. Нажмите «Оплатить через Crypto Bot» и завершите оплату.\n"
            f"2. После подтверждения перевода нажмите «Проверить оплату».",
            reply_markup=kb,
        )
    except Exception as e:
        logging.error(f"Ошибка CryptoBot: {e}")
        await call.answer(
            "Ошибка создания счета. Проверьте CRYPTO_PAY_TOKEN!",
            show_alert=True,
        )

    await call.answer()


@dp.callback_query(F.data.startswith("check_"))
async def check_payment(call: types.CallbackQuery):
    data_parts = call.data.split("_")
    invoice_id = int(data_parts[1])
    p_id = int(data_parts[2])

    product = products.get(p_id)

    if not product:
        await call.answer(
            "Ошибка: Данный товар больше не существует или уже был куплен.", show_alert=True
        )
        return

    try:
        invoices = await crypto.get_invoices(invoice_ids=invoice_id)

        if invoices:
            inv = invoices if not isinstance(invoices, list) else invoices[0]

            if str(inv.status).lower() == "paid":
                # Автоматически удаляем товар из базы и сохраняем
                del products[p_id]
                save_products(products)

                buyer = call.from_user
                buyer_data = get_user(buyer.id)

                await call.message.edit_text(
                    f"✅ Оплата прошла успешно!\n"
                    f"📦 Товар: {product['name']}\n\n"
                    f"Товар отправлен ниже."
                )

                await call.message.answer(
                    f"🎉 Ваш купленный товар ({product['name']}):\n\n"
                    f"{product['content']}"
                )

                # Начисление реферального бонуса
                referrer_id = buyer_data.get("referrer")
                if referrer_id and referrer_id in users:
                    ref_reward = round(product["price"] * (REF_PERCENT / 100), 2)
                    users[referrer_id]["balance"] += ref_reward
                    with suppress(Exception):
                        await bot.send_message(
                            referrer_id,
                            f"🎁 **Реферальный бонус!**\nВам зачислено `{ref_reward}`₽ (10%).",
                            parse_mode="Markdown",
                        )

                # Уведомление админу
                admin_text = (
                    f"🛒 **ПОКУПКА В CRYPTO BOT!**\n\n"
                    f"📦 **Купили:** {product['name']}\n"
                    f"👤 **Покупатель:** {format_user_mention(buyer)}\n"
                    f"💳 **Сумма:** `{product['price']}`₽"
                )
                with suppress(Exception):
                    await bot.send_message(ADMIN_ID, admin_text, parse_mode="Markdown")

                await call.answer("Спасибо за покупку!", show_alert=True)
                return
            else:
                await call.answer(
                    f"❌ Статус платежа: {inv.status}. Если вы уже оплатили, подождите 15-30 секунд.",
                    show_alert=True,
                )
                return

        await call.answer("Счет не найден в системе Crypto Bot.", show_alert=True)

    except Exception as e:
        logging.error(f"Ошибка проверки оплаты: {e}")
        await call.answer(f"Ошибка проверки: {e}", show_alert=True)


# --- Обработка оплаты через Telegram Stars ---
@dp.callback_query(F.data.startswith("pay_stars_"))
async def pay_stars(call: types.CallbackQuery):
    p_id = int(call.data.split("_")[2])
    product = products.get(p_id)

    if not product:
        await call.answer("Товар не найден или уже продан!", show_alert=True)
        return

    stars_price = max(1, round(product["price"] / STARS_RATE))
    prices = [LabeledPrice(label=product["name"], amount=stars_price)]

    try:
        await call.message.answer_invoice(
            title=f"Покупка {product['name']}",
            description=f"Оплата товара '{product['name']}' через Telegram Stars",
            payload=f"stars_pay_{p_id}",
            currency="XTR",
            prices=prices,
            provider_token="",
        )
    except Exception as e:
        logging.error(f"Ошибка создания счета Stars: {e}")
        await call.answer("Ошибка при создании счета Stars.", show_alert=True)

    await call.answer()


@dp.pre_checkout_query()
async def process_pre_checkout_query(pre_checkout_query: PreCheckoutQuery):
    await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)


@dp.message(F.successful_payment)
async def process_successful_payment(message: types.Message):
    payload = message.successful_payment.invoice_payload

    if payload.startswith("stars_pay_"):
        p_id = int(payload.split("_")[2])
        product = products.get(p_id)

        if product:
            # Автоматически удаляем товар из базы и сохраняем
            del products[p_id]
            save_products(products)

            buyer = message.from_user
            buyer_data = get_user(buyer.id)

            await message.answer(
                f"✅ Оплата через Telegram Stars прошла успешно!\n\n"
                f"🎉 Ваш купленный товар ({product['name']}):\n\n"
                f"{product['content']}"
            )

            # Начисление реферального бонуса
            referrer_id = buyer_data.get("referrer")
            if referrer_id and referrer_id in users:
                ref_reward = round(product["price"] * (REF_PERCENT / 100), 2)
                users[referrer_id]["balance"] += ref_reward
                with suppress(Exception):
                    await bot.send_message(
                        referrer_id,
                        f"🎁 **Реферальный бонус!**\nВам зачислено `{ref_reward}`₽ (10%).",
                        parse_mode="Markdown",
                    )

            # Уведомление админу
            admin_text = (
                f"⭐️ **НОВАЯ ПОКУПКА ЗА STARS!**\n\n"
                f"📦 **Купили:** {product['name']}\n"
                f"👤 **Покупатель:** {format_user_mention(buyer)}\n"
                f"⭐️ **Сумма:** `{message.successful_payment.total_amount}` ⭐️ ({product['price']}₽)"
            )
            with suppress(Exception):
                await bot.send_message(ADMIN_ID, admin_text, parse_mode="Markdown")
        else:
            await message.answer("✅ Оплата получена, но товар не найден в базе. Обратитесь в поддержку.")


@dp.callback_query(F.data.startswith("pay_card_"))
async def pay_card(call: types.CallbackQuery):
    await call.answer("Оплата Картами временно недоступна.", show_alert=True)


@dp.callback_query(F.data == "user_profile")
async def show_profile(call: types.CallbackQuery):
    u_data = get_user(call.from_user.id)
    with suppress(TelegramBadRequest, TelegramForbiddenError):
        await call.message.edit_text(
            f"👤 **Профиль**\n\n"
            f"🆔 Ваш ID: `{call.from_user.id}`\n"
            f"💰 Реферальный баланс: `{u_data['balance']:.2f}` ₽\n"
            f"👥 Рефералов: `{u_data['ref_count']}`",
            reply_markup=main_keyboard(),
            parse_mode="Markdown",
        )
    await call.answer()


# --- Админ-панель (Команда /admin) ---
@dp.message(Command("admin"))
async def cmd_admin(message: types.Message, state: FSMContext):
    await state.clear()
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer("⚙️ Админ-панель", reply_markup=admin_keyboard())


@dp.callback_query(F.data == "adm_list")
async def adm_list(call: types.CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    if not products:
        await call.message.answer("Список товаров пуст.")
        await call.answer()
        return

    for p_id, data in products.items():
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="📋 Дублировать", callback_data=f"adm_dup_{p_id}")],
                [InlineKeyboardButton(text="🗑 Удалить", callback_data=f"delprod_{p_id}")]
            ]
        )
        text = f"📦 **Товар ID: `{p_id}`**\n🏷 Название: **{data['name']}**\n💰 Цена: **{data['price']}₽**\n🔗 Выдача: `{data['content']}`"
        await call.message.answer(text, reply_markup=kb, parse_mode="Markdown")
    await call.answer()


@dp.callback_query(F.data.startswith("adm_dup_"))
async def adm_dup_start(call: types.CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return

    p_id = int(call.data.split("_")[2])
    target_product = products.get(p_id)

    if not target_product:
        await call.answer("Товар не найден!", show_alert=True)
        return

    await state.update_data(
        dup_name=target_product["name"],
        dup_price=target_product["price"]
    )
    await state.set_state(DuplicateProduct.waiting_for_content)

    await call.message.answer(
        f"📋 **Создание дубликата товара:**\n"
        f"🏷 Название: **{target_product['name']}**\n"
        f"💰 Цена: **{target_product['price']}₽**\n\n"
        f"👇 Отправьте **ссылку** или **текст**, который получит покупатель:",
        parse_mode="Markdown"
    )
    await call.answer()


@dp.message(DuplicateProduct.waiting_for_content)
async def process_dup_content(message: types.Message, state: FSMContext):
    data = await state.get_data()

    new_id = max(products.keys(), default=0) + 1
    products[new_id] = {
        "name": data["dup_name"],
        "price": data["dup_price"],
        "content": message.text.strip(),
    }
    save_products(products)

    await state.clear()
    await message.answer(
        f"✅ **Дубликат товара успешно создан!**\n\n"
        f"📦 ID: `{new_id}`\n"
        f"🏷 Название: **{data['dup_name']}**\n"
        f"💰 Цена: **{data['dup_price']}₽**\n"
        f"🔗 Ссылка/Выдача: `{message.text.strip()}`",
        parse_mode="Markdown"
    )


@dp.callback_query(F.data == "adm_del")
async def adm_del_list(call: types.CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    if not products:
        await call.answer("Товаров нет.", show_alert=True)
        return

    kb = []
    for p_id, item in products.items():
        kb.append([InlineKeyboardButton(text=f"❌ Удалить {item['name']}", callback_data=f"delprod_{p_id}")])
    kb.append([InlineKeyboardButton(text="⬅️ Отмена", callback_data="to_main")])

    await call.message.edit_text(
        "🗑 **Выберите товар для удаления:**",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb),
        parse_mode="Markdown",
    )
    await call.answer()


@dp.callback_query(F.data.startswith("delprod_"))
async def adm_del_confirm(call: types.CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    p_id = int(call.data.replace("delprod_", ""))
    if p_id in products:
        deleted_item = products.pop(p_id)
        save_products(products)
        await call.answer(f"Товар «{deleted_item['name']}» удален!", show_alert=True)

    await call.message.edit_text("⚙️ Админ-панель", reply_markup=admin_keyboard())


@dp.callback_query(F.data == "adm_add")
async def adm_add_start(call: types.CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    await state.set_state(AddProduct.name)
    await call.message.answer("Введите название товара (например: 100k монет):")
    await call.answer()


@dp.message(AddProduct.name)
async def process_name(message: types.Message, state: FSMContext):
    await state.update_data(name=message.text)
    await state.set_state(AddProduct.price)
    await message.answer("Введите цену в рублях (например: 30):")


@dp.message(AddProduct.price)
async def process_price(message: types.Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("Пожалуйста, введите число!")
        return
    await state.update_data(price=int(message.text))
    await state.set_state(AddProduct.content)
    await message.answer("Введите текст или ссылку, которая автоматически выдастся покупателю после оплаты:")


@dp.message(AddProduct.content)
async def process_content(message: types.Message, state: FSMContext):
    data = await state.get_data()
    new_id = max(products.keys(), default=0) + 1
    products[new_id] = {
        "name": data["name"],
        "price": data["price"],
        "content": message.text.strip(),
    }
    save_products(products)
    await state.clear()
    await message.answer(f"✅ Товар **{data['name']}** ({data['price']}₽) успешно добавлен!", parse_mode="Markdown")


# --- Глобальная обработка ошибок Telegram ---
@dp.error(F.exception.is_instance(TelegramForbiddenError))
async def forbidden_error_handler(event: types.ErrorEvent):
    return True


@dp.error(F.exception.is_instance(TelegramBadRequest))
async def bad_request_error_handler(event: types.ErrorEvent):
    if "message is not modified" in str(event.exception):
        return True
    raise event.exception


# --- Запуск бота ---
async def main():
    global crypto
    logging.basicConfig(level=logging.INFO)

    crypto = AioCryptoPay(token=CRYPTO_PAY_TOKEN, network=Networks.MAIN_NET)

    try:
        await dp.start_polling(bot)
    finally:
        await crypto.close()


if __name__ == "__main__":
    asyncio.run(main())