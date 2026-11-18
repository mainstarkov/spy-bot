import tempfile
import unittest
from pathlib import Path
from game import Game

class GameTests(unittest.TestCase):
    def setUp(self):
        self.game = Game(':memory:')
        self.room = self.game.create(1, 'Аня', 'online')

    def tearDown(self):
        self.game.db.close()

    def fill(self):
        for uid in (2, 3):
            self.game.join(self.room['code'], uid, str(uid))

    def test_membership_and_permissions(self):
        with self.assertRaises(ValueError):
            self.game.start(1)
        self.fill()
        with self.assertRaises(ValueError):
            self.game.create(2, 'Дубль', 'offline')
        with self.assertRaises(ValueError):
            self.game.start(2)
        self.game.leave(1)
        self.assertEqual(self.game.member(2)['host'], '2')

    def test_round_and_votes(self):
        self.fill()
        room = self.game.start(1)
        self.assertIn(room['spy'], room['players'])
        with self.assertRaises(ValueError):
            self.game.leave(2)
        with self.assertRaises(ValueError):
            self.game.join(room['code'], 4, '4')
        self.game.voting(room)
        for uid in (1, 2, 3):
            target = room['spy'] if str(uid) != room['spy'] else next(p for p in room['players'] if p != str(uid))
            room = self.game.vote(uid, target, room['round'])
        self.assertEqual(self.game.result(room), 'Мирные победили!')
        with self.assertRaises(ValueError):
            self.game.vote(1, '2', room['round'])
        self.game.finish(room)
        self.assertNotIn('spy', self.game.member(1))
        new = self.game.start(1)
        self.game.voting(new)
        with self.assertRaises(ValueError):
            self.game.vote(1, '2', new['round'] - 1)
        with self.assertRaises(ValueError):
            self.game.vote(1, '1', new['round'])

    def test_tie_and_restart(self):
        self.fill()
        room = self.game.start(1)
        self.game.voting(room)
        self.assertIn('Шпион победил', self.game.result(room))
        for uid, target in ((1, '2'), (2, '3'), (3, '1')):
            room = self.game.vote(uid, target, room['round'])
        self.assertIn('Шпион победил', self.game.result(room))
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / 'test.sqlite3')
            first = Game(path)
            first.save(room)
            first.db.close()
            second = Game(path)
            self.assertEqual(second.member(1), room)
            second.db.close()

if __name__ == '__main__':
    unittest.main()
