import asyncio
import json
import os
import re
import sqlite3
import secrets
from decimal import Decimal, InvalidOperation
from pathlib import Path

from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, Update
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv('BOT_TOKEN')
BASE_URL = os.getenv('RENDER_EXTERNAL_URL', '').rstrip('/')
PORT = int(os.getenv('PORT', '10000'))
WEBHOOK_SECRET = os.getenv('WEBHOOK_SECRET') or secrets.token_urlsafe(32)
WEBHOOK_PATH = f'/telegram/{WEBHOOK_SECRET}'
DB = Path(__file__).with_name('p2p.sqlite3')
D = lambda s: Decimal(str(s).replace(',', '.'))

MENU = ReplyKeyboardMarkup(keyboard=[
    [KeyboardButton(text='▶️ Почати сесію'), KeyboardButton(text='📊 Поточна сесія')],
    [KeyboardButton(text='🟢 Купівля'), KeyboardButton(text='🔴 Продаж')],
    [KeyboardButton(text='🧮 Калькулятор'), KeyboardButton(text='⏹ Завершити сесію')],
], resize_keyboard=True)

class Flow(StatesGroup):
    opening = State()
    trade = State()
    calc = State()


def connect():
    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row
    return db


def init_db():
    with connect() as db:
        db.executescript('''
        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, started_at TEXT DEFAULT CURRENT_TIMESTAMP,
            ended_at TEXT, opening_pln TEXT NOT NULL, opening_usdt TEXT NOT NULL,
            opening_cost TEXT NOT NULL, pln TEXT NOT NULL, usdt TEXT NOT NULL,
            inventory_cost TEXT NOT NULL, realized TEXT NOT NULL DEFAULT '0');
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY, session_id INTEGER NOT NULL, side TEXT NOT NULL,
            qty TEXT NOT NULL, rate TEXT NOT NULL, fee TEXT NOT NULL,
            fee_currency TEXT NOT NULL, realized TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        ''')


def active(db, uid):
    return db.execute('SELECT * FROM sessions WHERE user_id=? AND ended_at IS NULL ORDER BY id DESC LIMIT 1', (uid,)).fetchone()


def fmt(v):
    return f'{D(v):.4f}'.rstrip('0').rstrip('.')


def nums(text, count):
    parts = text.replace(',', '.').split()
    if len(parts) != count:
        raise ValueError(f'Очікую {count} значень, розділених пробілами.')
    try:
        return [D(p) for p in parts]
    except InvalidOperation:
        raise ValueError('Введи числа, наприклад 90 3.91 0.09 USDT')


def parse_trade(text):
    match = re.fullmatch(r'\s*(купив|купила|купівля|купити|продав|продала|продаж)\s+(\d+(?:[.,]\d+)?)\s*USDT\s+по\s+(\d+(?:[.,]\d+)?)\s+комісія\s+(\d+(?:[.,]\d+)?)\s*(USDT|PLN)?\s*', text, re.I)
    if not match:
        return None
    side = 'buy' if match.group(1).lower().startswith('куп') else 'sell'
    fee = D(match.group(4))
    if fee and not match.group(5):
        raise ValueError('Вкажи валюту комісії: USDT або PLN.')
    return side, D(match.group(2)), D(match.group(3)), fee, (match.group(5) or 'USDT').upper()


def record(uid, side, qty, rate, fee, fee_currency):
    if qty <= 0 or rate <= 0 or fee < 0 or fee_currency not in ('USDT', 'PLN'):
        raise ValueError('Кількість і курс мають бути додатними; комісія — невід’ємною.')
    with connect() as db:
        s = active(db, uid)
        if not s:
            raise ValueError('Спочатку почни сесію.')
        pln, usdt, cost, realized = map(D, (s['pln'], s['usdt'], s['inventory_cost'], s['realized']))
        trade_pnl = D('0')
        if side == 'buy':
            net = qty - (fee if fee_currency == 'USDT' else 0)
            expense = qty * rate + (fee if fee_currency == 'PLN' else 0)
            if net <= 0 or expense > pln:
                raise ValueError('Недостатньо PLN або комісія перевищує отримані USDT.')
            pln -= expense
            usdt += net
            cost += expense
        else:
            out = qty + (fee if fee_currency == 'USDT' else 0)
            proceeds = qty * rate - (fee if fee_currency == 'PLN' else 0)
            if out > usdt or proceeds < 0:
                raise ValueError('Недостатньо USDT або комісія перевищує виручку.')
            basis = cost * out / usdt
            trade_pnl = proceeds - basis
            pln += proceeds
            usdt -= out
            cost -= basis
            realized += trade_pnl
            if usdt == 0:
                cost = D('0')
        db.execute('UPDATE sessions SET pln=?, usdt=?, inventory_cost=?, realized=? WHERE id=?',
                   tuple(map(str, (pln, usdt, cost, realized))) + (s['id'],))
        db.execute('INSERT INTO trades(session_id,side,qty,rate,fee,fee_currency,realized) VALUES(?,?,?,?,?,?,?)',
                   (s['id'], side, str(qty), str(rate), str(fee), fee_currency, str(trade_pnl)))
        return pln, usdt, trade_pnl


def summary(s):
    with connect() as db:
        count = db.execute('SELECT COUNT(*) FROM trades WHERE session_id=?', (s['id'],)).fetchone()[0]
    return (f'Сесія #{s["id"]}\nОперацій: {count}\n'
            f'PLN: {fmt(s["pln"])} zł\nUSDT: {fmt(s["usdt"])}\n'
            f'Реалізований результат: {fmt(s["realized"])} zł\n'
            f'Собівартість залишку USDT: {fmt(s["inventory_cost"])} zł\n'
            'Нереалізований результат не враховано.')


def calculate(amount, buy_rate, sell_rate, fee, fills):
    if min(amount, buy_rate, sell_rate, fills) <= 0 or fee < 0 or fills != int(fills):
        raise ValueError('Перевір значення: обсяг і курси > 0, угоди — ціле число, комісія ≥ 0.')
    net = amount - fee * int(fills)
    if net <= 0:
        raise ValueError('Комісія перевищує обсяг USDT.')
    spent = amount * buy_rate
    revenue = net * sell_rate
    return f'Витрати: {fmt(spent)} zł\nОтримаєш: {fmt(net)} USDT\nПродаж: {fmt(revenue)} zł\nОчікуваний прибуток: {fmt(revenue-spent)} zł ({fmt((revenue-spent)/spent*100)}%)'

bot_dp = Dispatcher(storage=MemoryStorage())

@bot_dp.message(Command('start'))
async def start(m: Message):
    await m.answer('P2P Session Bot: PLN/USDT. Обери дію або надішли операцію текстом.', reply_markup=MENU)

@bot_dp.message(Command('cancel'))
async def cancel(m: Message, state: FSMContext):
    await state.clear()
    await m.answer('Введення скасовано.', reply_markup=MENU)

@bot_dp.message(Command('new'))
@bot_dp.message(F.text == '▶️ Почати сесію')
async def new(m: Message, state: FSMContext):
    with connect() as db:
        if active(db, m.from_user.id):
            await m.answer('Спочатку заверши поточну сесію.')
            return
    await state.set_state(Flow.opening)
    await m.answer('Введи початкові залишки: PLN USDT собівартість_1_USDT_у_PLN\nНаприклад: 351.9 0 0\nЯкщо USDT = 0, третє число має бути 0. /cancel — скасувати.')

@bot_dp.message(Flow.opening)
async def opening(m: Message, state: FSMContext):
    try:
        pln, usdt, rate = nums(m.text or '', 3)
        if min(pln, usdt, rate) < 0 or (usdt > 0 and rate == 0):
            raise ValueError('Залишки не можуть бути від’ємними; для USDT потрібна собівартість.')
        with connect() as db:
            if active(db, m.from_user.id):
                raise ValueError('Сесія вже активна.')
            db.execute('INSERT INTO sessions(user_id,opening_pln,opening_usdt,opening_cost,pln,usdt,inventory_cost) VALUES(?,?,?,?,?,?,?)',
                       (m.from_user.id, str(pln), str(usdt), str(usdt*rate), str(pln), str(usdt), str(usdt*rate)))
        await state.clear()
        await m.answer('Сесію розпочато!', reply_markup=MENU)
    except (ValueError, InvalidOperation) as e:
        await m.answer(f'Помилка: {e}')

@bot_dp.message(Command('buy'))
@bot_dp.message(F.text == '🟢 Купівля')
async def buy(m: Message, state: FSMContext):
    await state.set_state(Flow.trade)
    await state.update_data(side='buy')
    await m.answer('Введи: кількість_USDT курс комісія валюта_комісії\nНаприклад: 90 3.91 0.09 USDT')

@bot_dp.message(Command('sell'))
@bot_dp.message(F.text == '🔴 Продаж')
async def sell(m: Message, state: FSMContext):
    await state.set_state(Flow.trade)
    await state.update_data(side='sell')
    await m.answer('Введи: кількість_USDT курс комісія валюта_комісії\nНаприклад: 50 3.94 0 USDT')

@bot_dp.message(Flow.trade)
async def trade_form(m: Message, state: FSMContext):
    try:
        parts = (m.text or '').replace(',', '.').split()
        if len(parts) != 4:
            raise ValueError('Формат: 90 3.91 0.09 USDT')
        qty, rate, fee = [D(v) for v in parts[:3]]
        data = await state.get_data()
        pln, usdt, pnl = record(m.from_user.id, data['side'], qty, rate, fee, parts[3].upper())
        await state.clear()
        await m.answer(f'Записано. PLN: {fmt(pln)}; USDT: {fmt(usdt)}; результат продажу: {fmt(pnl)} zł', reply_markup=MENU)
    except (ValueError, InvalidOperation) as e:
        await m.answer(f'Помилка: {e}')

@bot_dp.message(Command('calc'))
@bot_dp.message(F.text == '🧮 Калькулятор')
async def calc(m: Message, state: FSMContext):
    await state.set_state(Flow.calc)
    await m.answer('Введи: USDT курс_купівлі курс_продажу комісія_USDT_за_угоду кількість_угод\nНаприклад: 90 3.91 3.94 0.09 3\nКомісія за купівлю, без комісії продажу.')

@bot_dp.message(Flow.calc)
async def calc_form(m: Message, state: FSMContext):
    try:
        amount, br, sr, fee, fills = nums(m.text or '', 5)
        result = calculate(amount, br, sr, fee, fills)
        await state.clear()
        await m.answer(result, reply_markup=MENU)
    except (ValueError, InvalidOperation) as e:
        await m.answer(f'Помилка: {e}')

@bot_dp.message(Command('status'))
@bot_dp.message(F.text == '📊 Поточна сесія')
async def status(m: Message):
    with connect() as db:
        s = active(db, m.from_user.id)
    await m.answer(summary(s) if s else 'Немає активної сесії.')

@bot_dp.message(Command('end'))
@bot_dp.message(F.text == '⏹ Завершити сесію')
async def end(m: Message):
    with connect() as db:
        s = active(db, m.from_user.id)
        if not s:
            await m.answer('Немає активної сесії.')
            return
        report = summary(s)
        db.execute('UPDATE sessions SET ended_at=CURRENT_TIMESTAMP WHERE id=?', (s['id'],))
    await m.answer('Сесію завершено.\n' + report, reply_markup=MENU)

@bot_dp.message(F.text)
async def plain_text(m: Message):
    try:
        parsed = parse_trade(m.text)
        if not parsed:
            await m.answer('Не розпізнав операцію. Приклад: Купив 90 USDT по 3.91 комісія 0.09 USDT')
            return
        pln, usdt, pnl = record(m.from_user.id, *parsed)
        await m.answer(f'Записано. PLN: {fmt(pln)}; USDT: {fmt(usdt)}; результат продажу: {fmt(pnl)} zł')
    except (ValueError, InvalidOperation) as e:
        await m.answer(f'Помилка: {e}')

async def health(request):
    return web.json_response({'status': 'ok', 'service': 'p2p-session-bot'})

async def telegram_webhook(request):
    if request.match_info.get('secret') != WEBHOOK_SECRET:
        raise web.HTTPForbidden(text='Forbidden')
    try:
        data = await request.json()
        update = Update.model_validate(data, context={'bot': request.app['bot']})
        await bot_dp.feed_update(request.app['bot'], update)
    except (json.JSONDecodeError, ValueError):
        raise web.HTTPBadRequest(text='Invalid update')
    return web.Response(text='OK')

async def on_startup(app):
    bot = app['bot']
    await bot.set_webhook(f'{BASE_URL}{WEBHOOK_PATH}', secret_token=os.getenv('TELEGRAM_WEBHOOK_HEADER_SECRET', WEBHOOK_SECRET), drop_pending_updates=False)

async def on_cleanup(app):
    await app['bot'].delete_webhook(drop_pending_updates=False)
    await app['bot'].session.close()

async def main():
    if not TOKEN:
        raise RuntimeError('Set BOT_TOKEN in Render Environment Variables')
    if not BASE_URL:
        raise RuntimeError('RENDER_EXTERNAL_URL is missing. Deploy this app as a Render Web Service.')
    init_db()
    bot = Bot(TOKEN)
    app = web.Application()
    app['bot'] = bot
    app.router.add_get('/', health)
    app.router.add_post('/telegram/{secret}', telegram_webhook)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()

if __name__ == '__main__':
    asyncio.run(main())
