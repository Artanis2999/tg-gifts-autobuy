from aiogram import Router, F
from aiogram.types import Message, LabeledPrice, PreCheckoutQuery, CallbackQuery
from aiogram.exceptions import TelegramBadRequest
from typing import Optional

from settings import settings
import db

router = Router(name="payments")

DEFAULT_TOPUP = 2000  # ⭐ по умолчанию

def _parse_amount_arg(text: str) -> Optional[int]:
    parts = text.strip().split()
    if len(parts) >= 2 and parts[1].isdigit():
        return int(parts[1])
    return None

async def _send_invoice(chat_id: int, amount: int, bot):
    await bot.send_invoice(
        chat_id=chat_id,
        title="Пополнение баланса",
        description=f"Зачисление {amount} ⭐ на внутренний баланс бота",
        payload=f"topup:{amount}",
        provider_token="",  # для Stars можно пустую строку
        currency=settings.STARS_CURRENCY,  # XTR
        prices=[LabeledPrice(label="Top-up", amount=amount)],
    )

@router.message(F.text.startswith("/buy"))
async def cmd_buy(message: Message):
    await db.ensure_user(message.from_user.id, message.from_user.username)
    amount = _parse_amount_arg(message.text) or DEFAULT_TOPUP
    if amount <= 0:
        return await message.answer(f"Укажи сумму больше 0. Пример: /buy {DEFAULT_TOPUP}")

    try:
        await _send_invoice(message.chat.id, amount, message.bot)
    except TelegramBadRequest as e:
        await db.log("ERROR", f"send_invoice failed: {e}")
        await message.answer("Не удалось выставить счёт. Попробуй позже.")

@router.callback_query(F.data.startswith("buy:"))
async def cb_buy_amount(c: CallbackQuery):
    try:
        _, amt = c.data.split(":", 1)
        amount = int(amt) if amt.isdigit() else DEFAULT_TOPUP
        if amount <= 0:
            amount = DEFAULT_TOPUP
        await _send_invoice(c.message.chat.id, amount, c.bot)
    except Exception as e:
        await db.log("ERROR", f"callback buy failed: {e}")
        await c.message.answer("Не удалось выставить счёт.")
    finally:
        await c.answer()

@router.pre_checkout_query()
async def on_pre_checkout(pre_q: PreCheckoutQuery):
    await pre_q.answer(ok=True)

@router.message(F.successful_payment)
async def on_successful_payment(message: Message):
    sp = message.successful_payment
    amount = int(sp.total_amount)
    user_id = message.from_user.id
    await db.add_balance(user_id, amount)
    await db.record_payment(user_id, amount, sp.invoice_payload or "")
    balance = await db.get_balance(user_id)
    await message.answer(f"✅ Зачислено: {amount} ⭐\nТекущий баланс: {balance} ⭐")
