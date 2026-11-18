"""Правила и сохранение комнат, без зависимости от Telegram."""
import json
import secrets
import sqlite3
import time

LOCATIONS = ['Школа', 'Больница', 'Самолёт', 'Поезд', 'Ресторан', 'Пляж', 'Кинотеатр', 'Банк', 'Космическая станция', 'Отель', 'Музей', 'Цирк', 'Зоопарк', 'Библиотека', 'Корабль']

class Game:
    def __init__(self, path='spy.sqlite3'):
        self.db = sqlite3.connect(path)
        self.db.execute('CREATE TABLE IF NOT EXISTS rooms (code TEXT PRIMARY KEY, data TEXT NOT NULL)')

    def rooms(self):
        return [json.loads(row[0]) for row in self.db.execute('SELECT data FROM rooms')]

    def save(self, room):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO rooms VALUES (?, ?)', (room['code'], json.dumps(room, ensure_ascii=False)))

    def room(self, code):
        row = self.db.execute('SELECT data FROM rooms WHERE code = ?', (code.upper(),)).fetchone()
        if not row:
            raise ValueError('Комната не найдена.')
        return json.loads(row[0])

    def current(self, uid):
        return next((r for r in self.rooms() if str(uid) in r['players']), None)

    def create(self, uid, name, mode):
        if self.current(uid):
            raise ValueError('Сначала выйди из текущей комнаты: /leave')
        if mode not in ('online', 'offline'):
            raise ValueError('Неизвестный режим.')
        code = secrets.token_hex(3).upper()
        while any(r['code'] == code for r in self.rooms()):
            code = secrets.token_hex(3).upper()
        room = dict(code=code, host=str(uid), mode=mode, players={str(uid): name[:64]}, state='lobby', votes={}, round=0)
        self.save(room)
        return room

    def join(self, code, uid, name):
        room = self.room(code)
        if self.current(uid):
            raise ValueError('Ты уже в комнате. Посмотреть: /room. Выйти: /leave')
        if room['state'] != 'lobby' or len(room['players']) >= 12:
            raise ValueError('Игра уже идёт или в комнате 12 игроков.')
        room['players'][str(uid)] = name[:64]
        self.save(room)
        return room

    def member(self, uid):
        room = self.current(uid)
        if not room:
            raise ValueError('Сначала создай комнату или войди по коду.')
        return room

    def host(self, uid):
        room = self.member(uid)
        if room['host'] != str(uid):
            raise ValueError('Эта кнопка доступна создателю комнаты.')
        return room

    def start(self, uid):
        room = self.host(uid)
        if room['state'] != 'lobby' or len(room['players']) < 3:
            raise ValueError('Нужны 3–12 игроков и комната в ожидании.')
        room.update(state='playing', spy=secrets.choice(list(room['players'])), location=secrets.choice(LOCATIONS), votes={}, deadline=time.time()+300, round=room['round']+1)
        self.save(room)
        return room

    def deal(self, uid):
        room = self.host(uid)
        if room['state'] != 'lobby' or len(room['players']) < 3:
            raise ValueError('Для начала нужны хотя бы 3 игрока.')
        room.update(state='dealing', picks={}, spy_card=secrets.randbelow(len(room['players']))+1,
                    location=secrets.choice(LOCATIONS), deadline=time.time()+90, round=room['round']+1)
        self.save(room)
        return room

    def pick(self, uid, code, number, card):
        room = self.member(uid)
        if room['state'] != 'dealing' or room['code'] != code or room['round'] != number or room['deadline'] <= time.time():
            raise ValueError('Выбор карточек уже закрыт.')
        if str(uid) in room['picks']:
            raise ValueError('Ты уже выбрал карточку.')
        if not 1 <= card <= len(room['players']) or card in room['picks'].values():
            raise ValueError('Карточка занята. Выбери свободную.')
        room['picks'][str(uid)] = card
        self.save(room)
        return room

    def reveal(self, room):
        free = set(range(1, len(room['players'])+1)) - set(room['picks'].values())
        for uid in room['players']:
            if uid not in room['picks']:
                card = secrets.choice(sorted(free))
                room['picks'][uid] = card
                free.remove(card)
        spy = next(uid for uid, card in room['picks'].items() if card == room['spy_card'])
        room.update(state='playing', spy=spy, votes={}, deadline=time.time()+300)
        self.save(room)

    def voting(self, room):
        if room['state'] != 'playing':
            raise ValueError('Сейчас нельзя начать голосование.')
        room.update(state='voting', deadline=time.time()+60)
        self.save(room)
        return room

    def vote(self, uid, target, round_number):
        room = self.member(uid)
        uid = str(uid)
        if room['state'] != 'voting' or room['round'] != round_number:
            raise ValueError('Это голосование уже завершено.')
        if target not in room['players'] or target == uid:
            raise ValueError('Выбери другого игрока.')
        if uid in room['votes']:
            raise ValueError('Ты уже проголосовал.')
        room['votes'][uid] = target
        self.save(room)
        return room

    def result(self, room):
        counts = {p: list(room['votes'].values()).count(p) for p in room['players']}
        top = max(counts.values())
        leaders = [p for p, n in counts.items() if n == top]
        caught = top > 0 and len(leaders) == 1 and leaders[0] == room['spy']
        return 'Мирные победили!' if caught else 'Шпион победил! При ничьей или отсутствии голосов шпион ускользает.'

    def finish(self, room):
        room.update(state='lobby', votes={})
        room.pop('spy', None)
        room.pop('location', None)
        room.pop('deadline', None)
        room.pop('picks', None)
        room.pop('spy_card', None)
        self.save(room)

    def leave(self, uid):
        room = self.member(uid)
        if room['state'] != 'lobby':
            raise ValueError('Во время раунда выход закрыт. Создатель может остановить игру: /stop')
        del room['players'][str(uid)]
        if not room['players']:
            with self.db:
                self.db.execute('DELETE FROM rooms WHERE code = ?', (room['code'],))
        else:
            if room['host'] == str(uid):
                room['host'] = next(iter(room['players']))
            self.save(room)
        return room
