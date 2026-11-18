"""Проверки сценариев без подключения к Telegram."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from aiogram.exceptions import TelegramForbiddenError
from aiogram.methods import SendMessage
import bot
from game import Game

class BotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.original = bot.store
        bot.store = Game(':memory:')
        self.api = SimpleNamespace(send_message=AsyncMock(), send_photo=AsyncMock(), me=AsyncMock(return_value=SimpleNamespace(username="test_bot")))

    async def asyncTearDown(self):
        bot.store.db.close()
        bot.store = self.original
        bot.joining.clear()
        bot.challenges.clear()

    def message(self, uid, text):
        return SimpleNamespace(from_user=SimpleNamespace(id=uid, full_name=f'Игрок {uid}'), text=text, answer=AsyncMock())

    async def verify(self, user):
        _, nonce, expected, chat, _ = bot.challenges[user.id]
        message = SimpleNamespace(chat=SimpleNamespace(id=chat))
        wrong_user = SimpleNamespace(id=99)
        await bot.verify(SimpleNamespace(from_user=wrong_user, message=message,
                         data=f'verify:{user.id}:{nonce}:{expected}', answer=AsyncMock()), self.api)
        self.assertIsNone(bot.store.current(user.id))
        await bot.verify(SimpleNamespace(from_user=user, message=message,
                         data=f'verify:{user.id}:{nonce}:{expected}', answer=AsyncMock()), self.api)
        self.assertNotIn(user.id, bot.challenges)

    async def complete_countdown(self):
        room = bot.store.member(1)
        self.assertEqual(room['state'], 'countdown')
        for _ in range(5):
            await bot.countdown(self.api, room)

    def room(self, mode):
        room = bot.store.create(1, 'Аня', mode)
        for uid in (2, 3):
            bot.store.join(room['code'], uid, str(uid))
        return room

    async def test_online_and_offline(self):
        self.room('online')
        await bot.handle(self.message(1, '/play'), self.api)
        self.assertEqual(self.api.send_photo.await_count, 3)
        await bot.begin_round(self.api, bot.store.member(1))
        await self.complete_countdown()
        self.api.send_message.reset_mock()
        with self.assertRaises(ValueError):
            await bot.handle(self.message(2, 'Тут шумно?'), self.api)
        await bot.handle(self.message(1, 'Тут шумно?'), self.api)
        self.assertTrue(bot.store.member(1)['question'])
        await bot.handle(self.message(2, 'Иногда.'), self.api)
        self.assertEqual(bot.store.member(1)['turn'], 1)
        room = bot.store.member(1)
        await bot.open_voting(self.api, room)
        await bot.finish(self.api, room, bot.store.result(room))
        self.assertEqual(bot.store.member(1)['state'], 'lobby')
        room = bot.store.member(1)
        room['mode'] = 'offline'
        bot.store.save(room)
        await bot.handle(self.message(1, '/play'), self.api)
        await bot.begin_round(self.api, bot.store.member(1))
        await self.complete_countdown()
        self.api.send_message.reset_mock()
        message = self.message(2, 'Вопрос')
        await bot.handle(message, self.api)
        self.api.send_message.assert_not_awaited()
        message.answer.assert_awaited_once()

    async def test_button_room_flow(self):
        create = self.message(1, '🌐 Создать онлайн')
        await bot.handle(create, self.api)
        await self.verify(create.from_user)
        code = bot.store.member(1)['code']
        await bot.handle(self.message(2, '🔑 Войти по коду'), self.api)
        with self.assertRaises(ValueError):
            await bot.handle(self.message(2, 'WRONG'), self.api)
        self.assertIn(2, bot.joining)
        await bot.handle(self.message(2, code.lower()), self.api)
        self.assertNotIn(2, bot.joining)
        await bot.handle(self.message(3, '🔑 Войти по коду'), self.api)
        await bot.handle(self.message(3, code), self.api)
        with self.assertRaises(ValueError):
            await bot.handle(self.message(2, '🚀 Начать игру'), self.api)
        await bot.handle(self.message(1, '🚀 Начать игру'), self.api)
        room = bot.store.member(1)
        code, number = room['code'], room['round']
        bot.store.pick(1, code, number, 1)
        with self.assertRaisesRegex(ValueError, 'занята'):
            bot.store.pick(2, code, number, 1)
        await bot.begin_round(self.api, bot.store.member(1))
        await self.complete_countdown()
        room = bot.store.member(1)
        self.assertEqual(len(set(room['picks'].values())), 3)
        for uid in room['players']:
            labels = [b.text for row in bot.controls(room, uid).keyboard for b in row]
            self.assertEqual('🕵️ Угадать локацию' in labels, uid == room['spy'])
            self.assertEqual('🗳 Начать голосование' in labels, uid == room['host'])
        await bot.handle(self.message(1, '🗳 Начать голосование'), self.api)
        room = bot.store.member(1)
        self.assertEqual(room['state'], 'voting')
        for uid in room['players']:
            targets = [b.callback_data.split(':')[-1] for row in bot.ballot(room, uid).inline_keyboard for b in row]
            self.assertNotIn(uid, targets)
            self.assertEqual(len(targets), 2)
        await bot.handle(self.message(1, '⏹ Остановить раунд'), self.api)
        await bot.handle(self.message(1, '🔄 Новый раунд'), self.api)
        self.assertEqual(bot.store.member(1)['round'], 2)

    async def test_invitation_and_role(self):
        message = self.message(1, '🏫 Создать в кабинете')
        await bot.handle(message, self.api)
        await self.verify(message.from_user)
        code = bot.store.member(1)['code']
        self.assertIn(f'https://t.me/test_bot?start=join_{code}', self.api.send_message.call_args.args[1])
        guest = self.message(2, f'/start join_{code}')
        guest.chat = SimpleNamespace(type='private')
        await bot.welcome(guest, self.api)
        await bot.welcome(guest, self.api)
        self.assertEqual(len(bot.store.member(1)['players']), 2)
        bot.store.join(code, 3, 'Игрок 3')
        room = bot.store.start(1)
        for player in room['players']:
            message = self.message(int(player), '🎭 Моя роль')
            await bot.handle(message, self.api)
            self.assertEqual(room['location'] in self.api.send_photo.call_args.kwargs['caption'], player != room['spy'])

    async def test_group_game_keeps_roles_private(self):
        message = self.message(1, '/spy')
        message.from_user.is_bot = False
        message.chat = SimpleNamespace(id=-100)
        await bot.group_game(message, self.api)
        await self.verify(message.from_user)
        room = bot.store.member(1)
        self.assertEqual(room['group'], -100)
        self.assertIn(room['code'], self.api.send_message.call_args.kwargs['reply_markup'].inline_keyboard[0][0].url)
        for uid in (2, 3):
            bot.store.join(room['code'], uid, str(uid))
        await bot.handle(self.message(1, '🚀 Начать игру'), self.api)
        await bot.begin_round(self.api, bot.store.member(1))
        await self.complete_countdown()
        group_messages = [call.args[1] for call in self.api.send_message.call_args_list if call.args[0] == -100]
        self.assertIn('До начала обсуждения: 5', '\n'.join(group_messages))
        self.assertIn('задаёт вопрос', group_messages[-1])
        self.assertNotIn(bot.store.member(1)['location'], group_messages[-1])

    async def test_guess_button_and_stale_callback(self):
        self.room('offline')
        room = bot.store.start(1)
        spy = int(room['spy'])
        message = self.message(spy, '🕵️ Угадать локацию')
        await bot.handle(message, self.api)
        buttons = message.answer.call_args.kwargs['reply_markup'].inline_keyboard
        selected = next(row[0] for row in buttons if row[0].text == room['location'])
        query = SimpleNamespace(from_user=SimpleNamespace(id=spy), data=selected.callback_data, answer=AsyncMock())
        await bot.guess(query, self.api)
        self.assertEqual(bot.store.member(1)['state'], 'lobby')
        await bot.guess(query, self.api)
        self.assertIn('недоступна', self.api.send_message.call_args.args[1])

    async def test_role_delivery_failure_cancels_round(self):
        self.room('online')
        async def send(uid, text, **kwargs):
            if uid == 2:
                raise TelegramForbiddenError(method=SendMessage(chat_id=uid, text='test'), message='blocked')
        self.api.send_message.side_effect = send
        self.api.send_photo.side_effect = send
        await bot.handle(self.message(1, '/play'), self.api)
        await bot.begin_round(self.api, bot.store.member(1))
        self.assertEqual(bot.store.member(1)['state'], 'lobby')
        self.assertNotIn('spy', bot.store.member(1))

    async def test_guess_finishes_round(self):
        self.room('online')
        room = bot.store.start(1)
        await bot.handle(self.message(int(room['spy']), '/guess ' + room['location'].upper()), self.api)
        self.assertEqual(bot.store.member(1)['state'], 'lobby')
        self.assertIn('угадал', self.api.send_photo.call_args.kwargs['caption'])

if __name__ == '__main__':
    unittest.main()
