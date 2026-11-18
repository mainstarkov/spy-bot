import asyncio
from html import escape
import logging
import os
import secrets
import time
from pathlib import Path
from types import SimpleNamespace

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.methods import SendMessage, SendPhoto
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message, CallbackQuery, ReplyKeyboardMarkup, KeyboardButton, FSInputFile
from game import Game, LOCATIONS

BASE = Path(__file__).resolve().parent
store = Game(str(BASE / 'spy.sqlite3'))
dp = Dispatcher()
lock = asyncio.Lock()
log = logging.getLogger(__name__)
joining = set()
challenges = {}
sticker_id = None
RULES = ('🕵️ Как играть\n\n3–12 игроков. Выберите по одной карточке. Все, кроме шпиона, узнают локацию. '
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


async def sticker_after(make_request, bot, method):
    global sticker_id
    result = await make_request(bot, method)
    if isinstance(method, (SendMessage, SendPhoto)):
        try:
            sent = await bot.send_sticker(
                result.chat.id, sticker_id or FSInputFile(BASE / 'art' / 'eye-sticker.webp'),
                message_thread_id=result.message_thread_id, disable_notification=True,
            )
            if sent.sticker:
                sticker_id = sent.sticker.file_id
        except TelegramAPIError:
            sticker_id = None
            log.warning('Не удалось отправить стикер')
    return result


async def illustrated(bot, chat_id, art, title, body, markup=None, *, protect=False):
    try:
        return await bot.send_photo(chat_id, FSInputFile(BASE / 'art' / art),
                             caption=f'<b>{title}</b>\n\n{body}', parse_mode='HTML', reply_markup=markup, protect_content=protect)
    except TelegramAPIError:
        return await bot.send_message(chat_id, f'<b>{title}</b>\n\n{body}', parse_mode='HTML', reply_markup=markup, protect_content=protect)


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
        elif room['state'] == 'dealing':
            rows.insert(0, ['🃏 Выбрать карточку'])
            if host:
                rows.append(['⏹ Остановить раунд'])
        else:
            rows.append(['📍 Список локаций'])
            if room['state'] == 'playing':
                if str(uid) == room['spy']:
                    rows.append(['🕵️ Угадать локацию'])
                if host:
                    rows.append(['🗳 Начать голосование'])
                    if room['mode'] == 'online' and 'turn' in room:
                        rows.append(['⏭ Пропустить ход'])
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


async def send_role(bot, room, uid):
    picture = 'spy-role.jpg' if str(uid) == room['spy'] else f"location-{LOCATIONS.index(room['location'])}.jpg"
    return await illustrated(bot, int(uid), picture, '🎭 Твоя роль', role_text(room, uid), controls(room, uid), protect=True)


async def invitation(bot, room):
    if room["state"] != "lobby":
        return lobby(room)
    username = (await bot.me()).username
    return lobby(room) + f"\n\n🔗 Перешли друзьям ссылку, чтобы войти без кода:\nhttps://t.me/{username}?start=join_{room['code']}"


def lobby(room):
    names = '\n'.join(f'• {name}' + (' 👑' if uid == room['host'] else '') for uid, name in room['players'].items())
    mode = 'в группе Telegram' if room.get('group') else 'онлайн' if room['mode'] == 'online' else 'в кабинете'
    status = {'lobby': 'Ждём игроков', 'dealing': 'Выбираем карточки', 'countdown': 'Готовимся к обсуждению', 'playing': 'Идёт обсуждение', 'voting': 'Выбираем шпиона'}[room['state']]
    hint = ('Передай друзьям код. Пусть нажмут «Войти по коду» и отправят его боту.\n'
            + ('Когда соберутся хотя бы 3 игрока, создатель может начать раунд кнопкой внизу.' if len(room['players']) < 3 else 'Все готовы? Создатель может начать раунд кнопкой внизу.')) if room['state'] == 'lobby' else ('Задавайте вопросы друг другу.' if room['state'] == 'playing' else 'Выбери подозреваемого в сообщении с голосованием.')
    if room['state'] == 'countdown':
        hint = 'Роли уже в личке. Ждём первого вопроса.'
    if room['state'] == 'dealing':
        hint = 'Выбери карточку в личке с ботом.'
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


async def announce(bot, room, text):
    if room.get('group'):
        try:
            await bot.send_message(room['group'], text)
        except TelegramAPIError:
            log.warning('Не удалось отправить объявление в группу')


async def challenge(bot, chat, user, mode):
    if store.current(user.id):
        raise ValueError('Ты уже в комнате. Сначала выйди из неё через «Моя комната» → «Выйти из комнаты».')
    now = time.monotonic()
    for uid, entry in list(challenges.items()):
        if entry[0] <= now:
            del challenges[uid]
    left, right = secrets.randbelow(8)+2, secrets.randbelow(8)+2
    answer = left + right
    nonce = secrets.token_hex(5)
    challenges[user.id] = (now+90, nonce, answer, chat, mode)
    options = [answer, answer+1, answer-1, answer+2]
    secrets.SystemRandom().shuffle(options)
    await bot.send_message(chat, f'🔒 Проверка перед созданием комнаты\n\nСколько будет {left} + {right}? Есть одна попытка и 90 секунд.',
                           reply_markup=keyboard([[(str(n), f"verify:{user.id}:{nonce}:{n}") for n in options]]))


async def group_lobby(bot, room):
    username = (await bot.me()).username
    button = InlineKeyboardButton(text='Войти в игру', url=f"https://t.me/{username}?start=join_{room['code']}")
    await bot.send_message(room['group'], lobby(room) + '\n\nНажми «Войти в игру», затем «Старт» в личке. Создатель запускает раунд там же.',
                           reply_markup=InlineKeyboardMarkup(inline_keyboard=[[button]]))


@dp.message(Command('spy'), F.chat.type.in_({'group', 'supergroup'}))
async def group_game(message: Message, bot: Bot):
    async with lock:
        active = next((r for r in store.rooms() if r.get('group') == message.chat.id), None)
        if active:
            await group_lobby(bot, active)
        elif not message.from_user or message.from_user.is_bot:
            await message.answer('Для создания комнаты отправь команду от своего имени.')
        else:
            try:
                await challenge(bot, message.chat.id, message.from_user, 'group')
            except ValueError as exc:
                await message.answer(str(exc))


@dp.callback_query(F.data.startswith('verify:'))
async def verify(query: CallbackQuery, bot: Bot):
    async with lock:
        _, uid, nonce, answer = query.data.split(':')
        user = query.from_user
        entry = challenges.get(user.id)
        if uid != str(user.id) or not entry or entry[1] != nonce or not query.message or query.message.chat.id != entry[3]:
            await query.answer('Эта проверка тебе недоступна.', show_alert=True)
            return
        expires, _, expected, chat, mode = challenges.pop(user.id)
        if time.monotonic() >= expires or answer != str(expected):
            await query.answer('Проверка не пройдена. Начни создание комнаты заново.', show_alert=True)
            return
        await query.answer('Проверка пройдена!')
        try:
            if mode == 'group' and any(r.get('group') == chat for r in store.rooms()):
                raise ValueError('В группе уже есть комната. Отправь /spy, чтобы показать её.')
            room = store.create(user.id, user.full_name, 'online' if mode == 'group' else mode)
        except ValueError as exc:
            await bot.send_message(chat, str(exc))
            return
        joining.discard(user.id)
        if mode == 'group':
            room['group'] = chat
            store.save(room)
            await group_lobby(bot, room)
        else:
            await bot.send_message(chat, await invitation(bot, room), reply_markup=controls(room, user.id))


async def finish(bot, room, result):
    details = f"Шпион: {room['players'][room['spy']]}\nЛокация: {room['location']}\n\nСоздатель может нажать «Новый раунд»."
    await hide_roles(bot, room)
    store.finish(room)
    for uid in room['players']:
        try:
            await illustrated(bot, int(uid), 'result.png', escape(result), escape(details), controls(room, uid))
        except TelegramAPIError:
            log.warning('Не удалось доставить итог игроку')
    await announce(bot, room, f'{result}\n{details}')


def ballot(room, uid):
    return keyboard([[(name, f"vote:{room['code']}:{room['round']}:{player}")] for player, name in room['players'].items() if player != str(uid)])


async def open_voting(bot, room):
    store.voting(room)
    await announce(bot, room, '🗳 Голосование открыто на 60 секунд. Выберите подозреваемого в личке с ботом.')
    await broadcast(bot, room, 'Обсуждение завершено. Пора выбрать шпиона!')
    for uid in room['players']:
        try:
            await illustrated(bot, int(uid), 'vote.png', '🗳 Кто шпион?',
                              'У тебя один голос и 60 секунд. Нажми на имя подозреваемого.', ballot(room, uid))
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
            await announce(bot, room, lobby(room))
        return
    joining.discard(message.from_user.id)
    room = store.current(message.from_user.id)
    if room:
        await message.answer(lobby(room), reply_markup=controls(room, message.from_user.id))
        if room['state'] == 'dealing':
            await show_cards(bot, room, message.from_user.id)
        return
    await illustrated(bot, message.chat.id, 'welcome.png', '🕵️ Шпион',
                      'Собери 3–12 друзей и выясни, кто не знает локацию.\n\n'
                      '🌐 <b>Онлайн</b> — вопросы через бота.\n'
                      '🏫 <b>В кабинете</b> — вопросы вслух.\n'
                      '💬 <b>В группе</b> — добавь бота и отправь /spy.\n\n'
                      'Есть приглашение? Нажми «Войти по коду».', controls())



@dp.callback_query(F.data.startswith('create:'))
async def create(query: CallbackQuery, bot: Bot):
    await query.answer()
    async with lock:
        try:
            await challenge(bot, query.from_user.id, query.from_user, query.data.split(':')[1])
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
        await illustrated(bot, uid, 'welcome.png', '📖 Как играть', RULES.split('\n\n', 1)[1], controls(room, uid))
        return
    if text == '❌ Отмена':
        joining.discard(uid)
        await message.answer('Выбери действие кнопками внизу.', reply_markup=controls(room, uid))
        return
    if text in ('🌐 Создать онлайн', '🏫 Создать в кабинете'):
        await challenge(bot, uid, message.from_user, 'online' if text == '🌐 Создать онлайн' else 'offline')
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
        await announce(bot, room, lobby(room))
        return
    if text == '🎭 Моя роль':
        await message.answer('Секретная карточка видна только перед началом обсуждения.')
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
        await announce(bot, room, lobby(room))
        return
    if not store.current(uid):
        await message.answer('Чтобы играть, создай комнату или нажми «Войти по коду».', reply_markup=controls())
        return
    room = store.member(uid)
    if text == '🃏 Выбрать карточку' or (cmd == '/room' and room['state'] == 'dealing'):
        if room['state'] != 'dealing':
            raise ValueError('Сейчас карточки не выбираются.')
        await show_cards(bot, room, uid)
    elif cmd == '/room':
        await message.answer(await invitation(bot, room), reply_markup=controls(room, uid))
        if room['state'] == 'playing' and room['mode'] == 'online' and 'turn' in room:
            _, asker, target = turn_players(room)
            current = target if room['question'] else asker
            action = 'отвечает' if room['question'] else 'задаёт вопрос'
            await message.answer(f"Сейчас {action} {room['players'][current]}.")
        if room['state'] == 'voting' and str(uid) not in room['votes']:
            await message.answer('Выбери, кто шпион:', reply_markup=ballot(room, uid))
    elif cmd == '/leave':
        room = store.leave(uid)
        await message.answer('Ты вышел из комнаты.', reply_markup=menu())
        await broadcast(bot, room, lobby(room))
        await announce(bot, room, lobby(room))
    elif cmd == '/play':
        room = store.deal(uid)
        await broadcast(bot, room, '🃏 Выбери карточку в течение 90 секунд. Кто не успеет, получит одну из оставшихся.')
        await announce(bot, room, '🃏 Выбираем карточки в личке с ботом!')
        for uid in room['players']:
            try:
                await show_cards(bot, room, uid)
            except TelegramAPIError:
                log.warning('Не удалось доставить карточки')
    elif text == '⏭ Пропустить ход':
        room = store.host(uid)
        if room['state'] != 'playing' or room['mode'] != 'online' or 'turn' not in room:
            raise ValueError('Ход можно пропустить во время онлайн-обсуждения.')
        room.update(turn=room['turn']+1, question=False)
        store.save(room)
        await discussion_message(bot, room, '⏭ Создатель передал ход следующему игроку.')
        await prompt_turn(bot, room)
    elif cmd == '/vote':
        await open_voting(bot, store.host(uid))
    elif cmd == '/stop':
        room = store.host(uid)
        if room['state'] == 'lobby':
            raise ValueError('Раунд ещё не начался.')
        if room['state'] == 'dealing':
            store.finish(room)
            await broadcast(bot, room, 'Выбор карточек отменён создателем.')
            await announce(bot, room, 'Выбор карточек отменён.')
            return
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
    elif room['state'] == 'playing' and room.get('group'):
        await message.answer('Задавай вопросы в группе Telegram. Здесь доступны роль и голосование.')
    elif room['state'] == 'playing' and room['mode'] == 'online':
        if len(text) > 1500:
            raise ValueError('Сообщение слишком длинное: максимум 1500 символов.')
        if 'turn' in room:
            await exchange(bot, room, uid, text)
        else:
            await broadcast(bot, room, f"{room['players'][str(uid)]}: {text}")
    else:
        await message.answer('В этом режиме обсуждайте вслух. Кнопка «Как играть» объяснит правила.' if room['mode'] == 'offline' else 'Обсуждение доступно во время раунда. Нажми «Моя комната».')


async def hide_roles(bot, room):
    for player, message_id in room.get('role_messages', {}).items():
        try:
            await bot.delete_message(int(player), message_id)
        except TelegramAPIError:
            log.warning('Не удалось скрыть секретную карточку')
    room.pop('role_messages', None)


async def begin_round(bot, room):
    store.reveal(room)
    room.update(state='countdown', count=3, deadline=time.time()+1, turn=0, question=False, role_messages={})
    store.save(room)
    failed = []
    for player in room['players']:
        try:
            message = await send_role(bot, room, player)
            room['role_messages'][player] = message.message_id
            store.save(room)
        except TelegramAPIError:
            failed.append(player)
    if failed:
        await hide_roles(bot, room)
        store.finish(room)
        await broadcast(bot, room, 'Раунд отменён: не все получили роль. Все игроки должны разблокировать бота и нажать /start. Затем создатель сможет снова нажать «Начать игру».')
        return
    await discussion_message(bot, room, '⏳ До начала обсуждения: 3')
    room['deadline'] = time.time()+1
    store.save(room)


async def discussion_message(bot, room, text):
    if room.get('group'):
        await announce(bot, room, text)
    else:
        await broadcast(bot, room, text)


def turn_players(room):
    players = list(room['players'])
    i = room['turn'] % len(players)
    return i, players[i], players[(i+1) % len(players)]


async def prompt_turn(bot, room):
    i, asker, target = turn_players(room)
    n = len(room['players'])
    instruction = 'Задай вопрос через /q текст.' if room.get('group') else 'Напиши вопрос боту обычным сообщением.'
    await discussion_message(bot, room, f"🎙 №{i+1} {room['players'][asker]} задаёт вопрос игроку №{(i+1)%n+1} {room['players'][target]}.\n{instruction}")


async def exchange(bot, room, uid, text, answering=None):
    if not text.strip() or len(text) > 1500:
        raise ValueError('Напиши текст длиной от 1 до 1500 символов.')
    _, asker, target = turn_players(room)
    waiting_answer = room['question']
    if (answering is not None and answering != waiting_answer) or str(uid) != (target if waiting_answer else asker):
        expected = target if waiting_answer else asker
        raise ValueError(f"Сейчас {'отвечает' if waiting_answer else 'задаёт вопрос'} {room['players'][expected]}.")
    room['question'] = not waiting_answer
    if waiting_answer:
        room['turn'] += 1
    store.save(room)
    await discussion_message(bot, room, f"{'💬 Ответ' if waiting_answer else '❓ Вопрос'} · {room['players'][str(uid)]}\n{text}")
    if waiting_answer:
        await prompt_turn(bot, room)
    else:
        instruction = 'Отправь /a текст.' if room.get('group') else 'Напиши ответ боту.'
        await discussion_message(bot, room, f"Ждём ответ от {room['players'][target]}. {instruction}")


@dp.message(Command('q', 'a'), F.chat.type.in_({'group', 'supergroup'}))
async def group_exchange(message: Message, bot: Bot):
    if not message.from_user or message.from_user.is_bot:
        return
    async with lock:
        room = store.current(message.from_user.id)
        if not room or room.get('group') != message.chat.id or room['state'] != 'playing' or 'turn' not in room:
            await message.answer('Это действие доступно участникам во время обсуждения.')
            return
        parts = message.text.split(maxsplit=1)
        command, body = parts[0], parts[1] if len(parts) == 2 else ''
        try:
            await exchange(bot, room, message.from_user.id, body.strip(), command.split('@')[0].lower() == '/a')
        except ValueError as exc:
            await message.answer(str(exc))


async def countdown(bot, room):
    room['count'] -= 1
    if room['count']:
        room['deadline'] = time.time()+1
        store.save(room)
        await discussion_message(bot, room, f"⏳ До начала обсуждения: {room['count']}")
        return
    await hide_roles(bot, room)
    room.update(state='playing', deadline=time.time()+300)
    store.save(room)
    await broadcast(bot, room, '▶️ Обсуждение началось! У вас 5 минут.')
    if room['mode'] == 'offline':
        await broadcast(bot, room, 'Обсуждайте вслух и передавайте ход по кругу.')
    else:
        await prompt_turn(bot, room)


async def show_cards(bot, room, uid):
    picks = room['picks']
    if str(uid) in picks:
        await bot.send_message(int(uid), f"✅ Твоя карточка №{picks[str(uid)]}. Ждём остальных.")
        return
    buttons = [(f"🃏 №{n}", f"card:{room['code']}:{room['round']}:{n}")
               for n in range(1, len(room['players'])+1) if n not in picks.values()]
    await illustrated(bot, int(uid), 'cards.png', '🃏 Выбери карточку',
                      f"Занято <b>{len(picks)} из {len(room['players'])}</b>. Нажми на свободный номер.",
                      keyboard([buttons[i:i+3] for i in range(0, len(buttons), 3)]))


@dp.callback_query(F.data.startswith('card:'))
async def pick_card(query: CallbackQuery, bot: Bot):
    async with lock:
        try:
            _, code, number, card = query.data.split(':')
            room = store.pick(query.from_user.id, code, int(number), int(card))
        except ValueError as exc:
            await query.answer(str(exc), show_alert=True)
            room = store.current(query.from_user.id)
            if room and room['state'] == 'dealing':
                await show_cards(bot, room, query.from_user.id)
            return
        await query.answer(f'Карточка №{card} твоя!')
        if len(room['picks']) == len(room['players']):
            await begin_round(bot, room)
        else:
            await show_cards(bot, room, query.from_user.id)


async def timers(bot):
    while True:
        async with lock:
            for room in store.rooms():
                if room.get('deadline', float('inf')) > time.time():
                    continue
                try:
                    if room['state'] == 'dealing':
                        await begin_round(bot, room)
                    elif room['state'] == 'countdown':
                        await countdown(bot, room)
                    elif room['state'] == 'playing':
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
        bot.session.middleware(sticker_after)
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
