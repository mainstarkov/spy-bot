import asyncio
import logging
import os
import time
from pathlib import Path
from types import SimpleNamespace

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message, CallbackQuery, ReplyKeyboardMarkup, KeyboardButton
from game import Game, LOCATIONS

BASE = Path(__file__).resolve().parent
store = Game(str(BASE / 'spy.sqlite3'))
dp = Dispatcher()
lock = asyncio.Lock()
log = logging.getLogger(__name__)
joining = set()
RULES = ('🕵️ Как играть\n\n3–12 игроков. Все, кроме шпиона, знают одну локацию. '
         'Задавайте друг другу вопросы: например, «Что здесь обычно делают?» Не называйте локацию напрямую!\n\n'
         'Мирные ищут шпиона. Шпион старается не выдать себя и понять, где все находятся. '
         'У него одна попытка угадать локацию.\n\n'
         'Обсуждение — 5 минут, затем голосование — 1 минута. '
         'Если больше всего голосов получил только шпион — побеждают мирные. При ничьей шпион ускользает.\n\n'
         '🌐 Онлайн: пишите вопросы и ответы сюда — их увидят все участники с вашим именем.\n'
         '🏫 В кабинете: разговаривайте вслух, а роли и голосование смотрите в телефоне.')
ACTIONS = {'🚀 Начать игру': '/play', '🔄 Новый раунд': '/play', '👥 Моя комната': '/room',
           '🚪 Выйти из комнаты': '/leave', '🗳 Начать голосование': '/vote', '⏹ Остановить раунд': '/stop',
           '📍 Список локаций': '/locations'}


def controls(room=None, uid=None):
    if room is None:
        rows = [['🌐 Создать онлайн', '🏫 Создать в кабинете'], ['🔑 Войти по коду'], ['📖 Как играть']]
    else:
        host = str(uid) == room['host']
        rows = [['👥 Моя комната', '📖 Как играть']]
        if room['state'] == 'lobby':
            if host:
                rows.insert(0, ['🔄 Новый раунд' if room['round'] else '🚀 Начать игру'])
            rows.append(['🚪 Выйти из комнаты'])
        else:
            rows.append(['🎭 Моя роль', '📍 Список локаций'])
            if room['state'] == 'playing':
                if str(uid) == room['spy']:
                    rows.append(['🕵️ Угадать локацию'])
                if host:
                    rows.append(['🗳 Начать голосование'])
            if host:
                rows.append(['⏹ Остановить раунд'])
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=t) for t in row] for row in rows], resize_keyboard=True)



def keyboard(rows):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=text, callback_data=data) for text, data in row] for row in rows])


def menu():
    return controls()


def role_text(room, uid):
    if str(uid) == room['spy']:
        return "🕵️ Ты — шпион! Постарайся узнать локацию."
    return f"📍 Локация: {room['location']}. Вычисли шпиона!"


async def invitation(bot, room):
    if room["state"] != "lobby":
        return lobby(room)
    username = (await bot.me()).username
    return lobby(room) + f"\n\n🔗 Перешли друзьям ссылку, чтобы войти без кода:\nhttps://t.me/{username}?start=join_{room['code']}"


def lobby(room):
    names = '\n'.join(f'• {name}' + (' 👑' if uid == room['host'] else '') for uid, name in room['players'].items())
    mode = 'онлайн' if room['mode'] == 'online' else 'в кабинете'
    status = {'lobby': 'Ждём игроков', 'playing': 'Идёт обсуждение', 'voting': 'Выбираем шпиона'}[room['state']]
    hint = ('Передай друзьям код. Пусть нажмут «Войти по коду» и отправят его боту.\n'
            + ('Когда соберутся хотя бы 3 игрока, создатель может начать раунд кнопкой внизу.' if len(room['players']) < 3 else 'Все готовы? Создатель может начать раунд кнопкой внизу.')) if room['state'] == 'lobby' else ('Задавайте вопросы друг другу.' if room['state'] == 'playing' else 'Выбери подозреваемого в сообщении с голосованием.')
    return f"👥 Комната {room['code']} · {mode}\n{status}\n\nИгроки ({len(room['players'])}/12):\n{names}\n\n{hint}"



async def broadcast(bot, room, text, reply_markup=None):
    failed = []
    for uid in room['players']:
        try:
            await bot.send_message(int(uid), text, reply_markup=reply_markup if reply_markup is not None else controls(room, uid))
        except TelegramAPIError:
            log.warning('Не удалось доставить игровое сообщение участнику')
            failed.append(uid)
    return failed


async def finish(bot, room, result):
    text = f"{result}\nШпион: {room['players'][room['spy']]}\nЛокация: {room['location']}\n\nЧтобы сыграть ещё раз, создатель нажимает «Новый раунд»."
    store.finish(room)
    await broadcast(bot, room, text)


def ballot(room, uid):
    return keyboard([[(name, f"vote:{room['code']}:{room['round']}:{player}")] for player, name in room['players'].items() if player != str(uid)])


async def open_voting(bot, room):
    store.voting(room)
    await broadcast(bot, room, 'Обсуждение завершено. Пора выбрать шпиона!')
    for uid in room['players']:
        try:
            await bot.send_message(int(uid), '🗳 Кто шпион? У тебя один голос и 60 секунд. Нажми на имя подозреваемого.', reply_markup=ballot(room, uid))
        except TelegramAPIError:
            log.warning('Не удалось доставить кнопки голосования')


@dp.message(Command('start', 'help'))
async def welcome(message: Message, bot: Bot):
    if message.chat.type != 'private':
        await message.answer('Откройте личный чат со мной: комнаты и роли находятся там.')
        return
    parts = message.text.split(maxsplit=1)
    if parts[0].split('@')[0] == '/start' and len(parts) == 2 and parts[1].startswith('join_'):
        async with lock:
            uid = message.from_user.id
            code = parts[1].removeprefix('join_').upper()
            room = store.current(uid)
            if room and room['code'] == code:
                await message.answer(lobby(room), reply_markup=controls(room, uid))
                return
            try:
                room = store.join(code, uid, message.from_user.full_name)
            except ValueError as exc:
                await message.answer(str(exc), reply_markup=controls(room, uid))
                return
            joining.discard(uid)
            await broadcast(bot, room, lobby(room))
        return
    joining.discard(message.from_user.id)
    room = store.current(message.from_user.id)
    await message.answer(lobby(room) if room else '🕵️ Шпион\n\nСобери друзей и найди шпиона!\n\n🌐 Онлайн — вопросы и ответы через бота.\n🏫 В кабинете — обсуждение вслух.\n\nСоздай комнату или войди по коду от друга. Кнопки находятся под полем ввода.', reply_markup=controls(room, message.from_user.id))



@dp.callback_query(F.data.startswith('create:'))
async def create(query: CallbackQuery, bot: Bot):
    await query.answer()
    async with lock:
        try:
            room = store.create(query.from_user.id, query.from_user.full_name, query.data.split(':')[1])
            await bot.send_message(query.from_user.id, await invitation(bot, room), reply_markup=controls(room, query.from_user.id))
        except ValueError as exc:
            await bot.send_message(query.from_user.id, str(exc))


@dp.callback_query(F.data.startswith('vote:'))
async def vote(query: CallbackQuery, bot: Bot):
    await query.answer()
    async with lock:
        try:
            _, code, round_number, target = query.data.split(':')
            if store.member(query.from_user.id)['code'] != code:
                raise ValueError('Эта кнопка из другой комнаты.')
            room = store.vote(query.from_user.id, target, int(round_number))
            await bot.send_message(query.from_user.id, 'Голос принят.')
            if len(room['votes']) == len(room['players']):
                await finish(bot, room, store.result(room))
        except ValueError as exc:
            await bot.send_message(query.from_user.id, str(exc))


@dp.callback_query(F.data.startswith('guess:'))
async def guess(query: CallbackQuery, bot: Bot):
    await query.answer()
    async with lock:
        try:
            _, code, number, index = query.data.split(':')
            room = store.member(query.from_user.id)
            if room['code'] != code or room['round'] != int(number) or room['state'] != 'playing' or room['spy'] != str(query.from_user.id):
                raise ValueError('Эта попытка угадать уже недоступна.')
            if not 0 <= int(index) < len(LOCATIONS):
                raise ValueError('Неизвестная локация.')
            location = LOCATIONS[int(index)]
            message = SimpleNamespace(from_user=query.from_user, text='/guess ' + location)
            await handle(message, bot)
        except (ValueError, IndexError) as exc:
            await bot.send_message(query.from_user.id, str(exc))


@dp.message(F.text)
async def messages(message: Message, bot: Bot):
    if message.chat.type != 'private':
        return
    async with lock:
        try:
            await handle(message, bot)
        except ValueError as exc:
            await message.answer(str(exc))


async def handle(message, bot):
    uid = message.from_user.id
    text = message.text.strip()
    room = store.current(uid)
    if text == '📖 Как играть':
        await message.answer(RULES, reply_markup=controls(room, uid))
        return
    if text == '❌ Отмена':
        joining.discard(uid)
        await message.answer('Выбери действие кнопками внизу.', reply_markup=controls(room, uid))
        return
    if text in ('🌐 Создать онлайн', '🏫 Создать в кабинете'):
        room = store.create(uid, message.from_user.full_name, 'online' if text == '🌐 Создать онлайн' else 'offline')
        joining.discard(uid)
        await message.answer(await invitation(bot, room), reply_markup=controls(room, uid))
        return
    if text == '🔑 Войти по коду':
        if room:
            raise ValueError('Ты уже в комнате. Нажми «Моя комната» или «Выйти из комнаты».')
        joining.add(uid)
        await message.answer('Отправь код комнаты одним сообщением. Его можно узнать у создателя.', reply_markup=ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text='❌ Отмена')]], resize_keyboard=True))
        return
    if uid in joining and not text.startswith('/'):
        room = store.join(text, uid, message.from_user.full_name)
        joining.discard(uid)
        await broadcast(bot, room, lobby(room))
        return
    if text == '🎭 Моя роль':
        if not room or room['state'] == 'lobby':
            raise ValueError('Роль появится, когда начнётся раунд.')
        await message.answer(role_text(room, uid), reply_markup=controls(room, uid))
        return
    if text == '🕵️ Угадать локацию':
        room = store.member(uid)
        if room['state'] != 'playing' or room['spy'] != str(uid):
            raise ValueError('Угадывать может только шпион во время обсуждения.')
        await message.answer('🕵️ Где находятся игроки?\n\nВыбери локацию ниже. Осторожно: нажатие — окончательный ответ! У тебя одна попытка. Ошибка означает победу мирных.', reply_markup=keyboard([[(location, f"guess:{room['code']}:{room['round']}:{i}")] for i, location in enumerate(LOCATIONS)]))
        return
    text = ACTIONS.get(text, text)
    cmd, _, arg = text.partition(' ')
    cmd = cmd.split('@')[0].lower()
    if cmd == '/locations':
        await message.answer('Возможные локации:\n' + '\n'.join(LOCATIONS), reply_markup=controls(room, uid))
        return
    if cmd == '/join':
        room = store.join(arg.strip(), uid, message.from_user.full_name)
        joining.discard(uid)
        await broadcast(bot, room, lobby(room))
        return
    if not store.current(uid):
        await message.answer('Чтобы играть, создай комнату или нажми «Войти по коду».', reply_markup=controls())
        return
    room = store.member(uid)
    if cmd == '/room':
        await message.answer(await invitation(bot, room), reply_markup=controls(room, uid))
        if room['state'] == 'voting' and str(uid) not in room['votes']:
            await message.answer('Выбери, кто шпион:', reply_markup=ballot(room, uid))
    elif cmd == '/leave':
        room = store.leave(uid)
        await message.answer('Ты вышел из комнаты.', reply_markup=menu())
        await broadcast(bot, room, lobby(room))
    elif cmd == '/play':
        room = store.start(uid)
        failed = []
        for player in room['players']:
            try:
                await bot.send_message(int(player), role_text(room, player), reply_markup=controls(room, player))
            except TelegramAPIError:
                failed.append(player)
        if failed:
            store.finish(room)
            await broadcast(bot, room, 'Раунд отменён: не все получили роль. Все игроки должны разблокировать бота и нажать /start. Затем создатель сможет снова нажать «Начать игру».')
            return
        await broadcast(bot, room, 'Раунд начался! 5 минут. Задавайте друг другу вопросы, не называя локацию.\n' + ('Пишите сюда: сообщения получат все участники.' if room['mode'] == 'online' else 'Обсуждайте вслух. Телефоны нужны для голосования.'))
    elif cmd == '/vote':
        await open_voting(bot, store.host(uid))
    elif cmd == '/stop':
        room = store.host(uid)
        if room['state'] == 'lobby':
            raise ValueError('Раунд ещё не начался.')
        await finish(bot, room, 'Создатель остановил раунд. Победитель не определён.')
    elif cmd == '/guess':
        if room['state'] != 'playing' or str(uid) != room['spy']:
            raise ValueError('Угадывать может только шпион во время обсуждения.')
        if not arg.strip():
            raise ValueError('Напиши /guess название локации. Список: /locations')
        won = arg.strip().casefold() == room['location'].casefold()
        await finish(bot, room, 'Шпион угадал локацию и победил!' if won else 'Шпион ошибся. Мирные победили!')
    elif text.startswith('/'):
        await message.answer('Неизвестная команда. Список: /help')
    elif room['state'] == 'playing' and room['mode'] == 'online':
        if len(text) > 1500:
            raise ValueError('Сообщение слишком длинное: максимум 1500 символов.')
        await broadcast(bot, room, f"{room['players'][str(uid)]}: {text}")
    else:
        await message.answer('В этом режиме обсуждайте вслух. Кнопка «Как играть» объяснит правила.' if room['mode'] == 'offline' else 'Обсуждение доступно во время раунда. Нажми «Моя комната».')


async def timers(bot):
    while True:
        async with lock:
            for room in store.rooms():
                if room.get('deadline', float('inf')) > time.time():
                    continue
                try:
                    if room['state'] == 'playing':
                        await open_voting(bot, room)
                    elif room['state'] == 'voting':
                        await finish(bot, room, store.result(room))
                except TelegramAPIError:
                    log.warning('Ошибка доставки уведомления таймера')
        await asyncio.sleep(1)


async def main():
    token = os.environ.get('BOT_TOKEN')
    if not token:
        raise SystemExit('Задайте BOT_TOKEN — токен бота от @BotFather (см. README.md).')
    async with Bot(token) as bot:
        task = asyncio.create_task(timers(bot))
        try:
            await dp.start_polling(bot)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            store.db.close()


if __name__ == '__main__':
    logging.basicConfig(level=logging.WARNING)
    asyncio.run(main())
