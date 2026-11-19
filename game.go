package main

import (
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"math/big"
	"os"
	"path/filepath"
	"time"
)

var locations = []string{"Школа", "Больница", "Самолёт", "Поезд", "Ресторан", "Пляж", "Кинотеатр", "Банк", "Космическая станция", "Отель", "Музей", "Цирк", "Зоопарк", "Библиотека", "Корабль"}

type player struct {
	ID    int64
	Name  string
	Ready bool
}
type room struct {
	Group         int64
	Code          string
	Host          int64
	Players       []player
	Phase         string
	Round         int
	Deadline      time.Time
	Count         int
	Location      int
	SpyCard       int
	Spy           int64
	Cards         map[int64]int
	Roles         map[int64]int
	Votes         map[int64]int64
	Turn          int
	Answer        bool
	GroupMessages []int
}

func random(n int) int {
	v, err := rand.Int(rand.Reader, big.NewInt(int64(n)))
	if err != nil {
		panic(err)
	}
	return int(v.Int64())
}
func code() string {
	var bytes [6]byte
	if _, err := rand.Read(bytes[:]); err != nil {
		panic(err)
	}
	return hex.EncodeToString(bytes[:])
}
func (r *room) member(id int64) *player {
	for i := range r.Players {
		if r.Players[i].ID == id {
			return &r.Players[i]
		}
	}
	return nil
}
func (r *room) start(uid int64, now time.Time) error {
	if uid != r.Host {
		return errors.New("начать раунд может только создатель")
	}
	if r.Phase != "lobby" {
		return errors.New("раунд уже идёт")
	}
	if len(r.Players) < 3 {
		return errors.New("нужно хотя бы три игрока")
	}
	for _, p := range r.Players {
		if !p.Ready {
			return fmt.Errorf("%s должен открыть личку по кнопке «Войти в игру»", p.Name)
		}
	}
	r.Phase = "cards"
	r.Round++
	r.Cards = map[int64]int{}
	r.Roles = map[int64]int{}
	r.Votes = map[int64]int64{}
	r.SpyCard = 1 + random(len(r.Players))
	r.Location = random(len(locations))
	r.Turn = 0
	r.Answer = false
	r.Deadline = now.Add(90 * time.Second)
	return nil
}
func (r *room) pick(uid int64, n int, now time.Time) error {
	if r.Phase != "cards" || !now.Before(r.Deadline) || r.member(uid) == nil {
		return errors.New("выбор карточек закрыт")
	}
	if n < 1 || n > len(r.Players) {
		return errors.New("такой карточки нет")
	}
	if r.Cards[uid] != 0 {
		return errors.New("ты уже выбрал карточку")
	}
	for _, v := range r.Cards {
		if v == n {
			return errors.New("карточку уже заняли")
		}
	}
	r.Cards[uid] = n
	return nil
}
func (r *room) assign() {
	taken := map[int]bool{}
	for _, n := range r.Cards {
		taken[n] = true
	}
	for _, p := range r.Players {
		if r.Cards[p.ID] == 0 {
			free := []int{}
			for n := 1; n <= len(r.Players); n++ {
				if !taken[n] {
					free = append(free, n)
				}
			}
			n := free[random(len(free))]
			r.Cards[p.ID] = n
			taken[n] = true
		}
		if r.Cards[p.ID] == r.SpyCard {
			r.Spy = p.ID
		}
	}
}
func (r *room) vote(uid, target int64, now time.Time) error {
	if r.Phase != "voting" || !now.Before(r.Deadline) || r.member(uid) == nil {
		return errors.New("голосование закрыто")
	}
	if r.member(target) == nil || uid == target {
		return errors.New("выбери другого игрока")
	}
	if r.Votes[uid] != 0 {
		return errors.New("голос уже принят")
	}
	r.Votes[uid] = target
	return nil
}
func (r *room) ejected() int64 {
	counts := map[int64]int{}
	for _, id := range r.Votes {
		counts[id]++
	}
	best, selected := 0, int64(0)
	for id, n := range counts {
		if n > best {
			best, selected = n, id
		} else if n == best {
			selected = 0
		}
	}
	return selected
}
func (r *room) reset() {
	r.Phase = "lobby"
	r.Deadline = time.Time{}
	r.Spy = 0
	r.Roles = nil
	r.Cards = nil
	r.Votes = nil
}

type state struct {
	Rooms map[int64]*room
	path  string
}

func loadState(path string) (*state, error) {
	s := &state{Rooms: map[int64]*room{}, path: path}
	data, err := os.ReadFile(path)
	if os.IsNotExist(err) {
		return s, nil
	}
	if err != nil {
		return nil, err
	}
	if err = json.Unmarshal(data, s); err != nil {
		return nil, err
	}
	if s.Rooms == nil {
		s.Rooms = map[int64]*room{}
	}
	return s, nil
}
func (s *state) save() error {
	data, err := json.Marshal(s)
	if err != nil {
		return err
	}
	if err = os.MkdirAll(filepath.Dir(s.path), 0700); err != nil {
		return err
	}
	tmp, err := os.CreateTemp(filepath.Dir(s.path), ".spy-*")
	if err != nil {
		return err
	}
	defer os.Remove(tmp.Name())
	_, err = tmp.Write(data)
	if err == nil {
		err = tmp.Sync()
	}
	closeErr := tmp.Close()
	if err != nil {
		return err
	}
	if closeErr != nil {
		return closeErr
	}
	return os.Rename(tmp.Name(), s.path)
}
func (s *state) byCode(value string) *room {
	for _, r := range s.Rooms {
		if r.Code == value {
			return r
		}
	}
	return nil
}
func (s *state) active(uid int64) *room {
	for _, r := range s.Rooms {
		if r.member(uid) != nil {
			return r
		}
	}
	return nil
}
