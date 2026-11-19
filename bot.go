package main

import (
	"context"
	"errors"
	"fmt"
	"log"
	"strconv"
	"strings"
	"time"
	"unicode/utf8"
)

type check struct {
	Group    int64
	UID      int64
	Answer   int
	Deadline time.Time
}
type bot struct {
	ctx    context.Context
	api    *telegram
	logger *log.Logger
	me     user
	state  *state
	checks map[string]check
}

func (b *bot) tell(chat int64, text string, buttons [][]button, media string) {
	if _, err := b.send(chat, text, buttons, media, false); err != nil {
		b.logger.Print(err)
	}
}
func (b *bot) announce(r *room, text, media string) {
	id, err := b.send(r.Group, text, nil, media, false)
	if err != nil {
		b.logger.Print(err)
		return
	}
	if id > 0 {
		r.GroupMessages = append(r.GroupMessages, id)
	}
}
func row(text, data string) [][]button { return [][]button{{{Text: text, Data: data}}} }
func (b *bot) groupLink() string       { return "https://t.me/" + b.me.Username + "?startgroup=group" }
func (b *bot) joinLink(r *room) string {
	return "https://t.me/" + b.me.Username + "?start=join_" + r.Code
}
func (b *bot) menu(uid int64) {
	b.tell(uid, "🕵️ ШПИОН\n\nСобери 3–12 друзей в приватной группе. Сыграйте в общем чате, а секретные карточки и голоса получите в личке.\n\nСоздай группу в Telegram и добавь туда бота.", [][]button{
		{{Text: "➕ Добавить в группу", URL: b.groupLink()}},
		{{Text: "📖 Как играть", Data: "help"}, {Text: "👥 Моя комната", Data: "room"}},
	}, "welcome.mp4")
}
func (b *bot) rules(uid int64) {
	b.tell(uid, "📖 КАК ИГРАТЬ\n\n1. Войди по кнопке из группы и выбери карточку.\n2. Мирные видят локацию. Шпион должен её угадать.\n3. В группе задавайте вопросы по очереди, не называя место.\n4. Выбери подозреваемого в личке. Если исключили шпиона, мирные победили.", [][]button{{{Text: "➕ Добавить в группу", URL: b.groupLink()}}}, "guide.jpg")
}
func (b *bot) lobby(r *room) {
	var text strings.Builder
	fmt.Fprintf(&text, "👥 НАБОР В ИГРУ · %d/12\n\n", len(r.Players))
	for _, p := range r.Players {
		mark := "✅"
		if !p.Ready {
			mark = "⏳"
		}
		if p.ID == r.Host {
			mark += " 👑"
		}
		fmt.Fprintf(&text, "%s %s\n", mark, p.Name)
	}
	text.WriteString("\nКаждый, включая создателя, нажимает «Войти в игру». Когда готовы хотя бы трое, создатель нажимает «Начать игру».")
	b.tell(r.Group, text.String(), b.lobbyButtons(r), "lobby.mp4")
}
func (b *bot) lobbyButtons(r *room) [][]button {
	label := "🚀 Начать игру"
	if r.Round > 0 {
		label = "🔄 Новый раунд"
	}
	return [][]button{
		{{Text: "🔑 Войти в игру", URL: b.joinLink(r)}},
		{{Text: label, Data: fmt.Sprintf("play:%s:%d", r.Code, r.Round)}},
	}
}
func playerName(u user) string {
	name := strings.TrimSpace(strings.Join(strings.Fields(u.First+" "+u.Last), " "))
	if name == "" {
		name = "Игрок"
	}
	runes := []rune(name)
	if len(runes) > 40 {
		return string(runes[:40])
	}
	return name
}
func command(text string) (string, string) {
	first, rest, _ := strings.Cut(strings.TrimSpace(text), " ")
	name, _, _ := strings.Cut(first, "@")
	return strings.ToLower(name), strings.TrimSpace(rest)
}

func (b *bot) handleMessage(m *message) {
	if m.From.ID <= 0 || m.From.Bot || m.Text == "" {
		return
	}
	cmd, arg := command(m.Text)
	if m.Chat.Type == "private" {
		switch cmd {
		case "/start":
			if value, ok := strings.CutPrefix(arg, "join_"); ok {
				b.join(m.From, value)
				return
			}
			b.menu(m.Chat.ID)
		case "/help":
			b.rules(m.Chat.ID)
		case "/room":
			b.playerRoom(m.From.ID)
		case "/leave":
			b.leave(m.From.ID)
		case "/guess":
			b.guessMenu(m.From.ID)
		}
		return
	}
	if m.Chat.Type != "group" && m.Chat.Type != "supergroup" {
		return
	}
	if cmd == "/spy" || cmd == "/start" && arg == "group" {
		b.openLobby(m.Chat.ID, m.From)
		return
	}
	r := b.state.Rooms[m.Chat.ID]
	if r == nil {
		return
	}
	if cmd == "/stop" && m.From.ID == r.Host && r.Phase != "lobby" {
		b.finish(r, "Раунд остановлен создателем.", false)
		return
	}
	if cmd == "/vote" && m.From.ID == r.Host && r.Phase == "playing" {
		b.openVoting(r)
		return
	}
	if r.Phase != "playing" || r.member(m.From.ID) == nil {
		return
	}
	reply := m.Reply != nil && m.Reply.From.ID == b.me.ID && !strings.HasPrefix(cmd, "/")
	if !reply && cmd != "/q" && cmd != "/a" {
		return
	}
	if reply {
		arg = strings.TrimSpace(m.Text)
	}
	if arg == "" || utf8.RuneCountInString(arg) > 1500 {
		b.tell(r.Group, "Напиши от 1 до 1500 символов.", nil, "")
		return
	}
	asker, target := r.Players[r.Turn%len(r.Players)], r.Players[(r.Turn+1)%len(r.Players)]
	expected := asker
	if r.Answer {
		expected = target
	}
	if m.From.ID != expected.ID || !reply && ((cmd == "/a") != r.Answer) {
		b.tell(r.Group, "Сейчас ходит "+expected.Name+".", nil, "")
		return
	}
	if r.Answer {
		r.Turn++
		r.Answer = false
		b.turn(r)
	} else {
		r.Answer = true
		b.announce(r, "💬 "+target.Name+", твой ответ. Ответь на это сообщение или напиши /a ответ.", "")
	}
}
func (b *bot) openLobby(group int64, u user) {
	if r := b.state.Rooms[group]; r != nil {
		if r.Phase == "lobby" {
			b.lobby(r)
		} else {
			b.tell(group, "Раунд уже идёт. Карточки и голосование находятся в личке.", nil, "")
		}
		return
	}
	if b.state.active(u.ID) != nil {
		b.tell(group, "Ты уже участвуешь в другой комнате. Между раундами можно выйти командой /leave в личке.", nil, "")
		return
	}
	now := time.Now()
	for key, c := range b.checks {
		if !now.Before(c.Deadline) || c.Group == group && c.UID == u.ID {
			delete(b.checks, key)
		}
	}
	left, right := 2+random(8), 2+random(8)
	correct := left + right
	nonce := code()
	b.checks[nonce] = check{group, u.ID, correct, now.Add(90 * time.Second)}
	nums := []int{correct - 1, correct, correct + 1, correct + 2}
	for i := len(nums) - 1; i > 0; i-- {
		j := random(i + 1)
		nums[i], nums[j] = nums[j], nums[i]
	}
	choices := []button{}
	for _, n := range nums {
		choices = append(choices, button{Text: strconv.Itoa(n), Data: fmt.Sprintf("check:%s:%d", nonce, n)})
	}
	b.tell(group, fmt.Sprintf("🔒 ПРОВЕРКА СОЗДАТЕЛЯ\n\nСколько будет %d + %d? Нажми ответ в течение 90 секунд.", left, right), [][]button{choices}, "verify.mp4")
}
func (b *bot) join(u user, key string) {
	r := b.state.byCode(key)
	if r == nil {
		b.tell(u.ID, "Этого приглашения больше нет. Открой новое сообщение в группе.", nil, "")
		return
	}
	if current := b.state.active(u.ID); current != nil && current != r {
		b.tell(u.ID, "Ты в другой комнате. Выйди через /leave между раундами.", nil, "")
		return
	}
	if p := r.member(u.ID); p != nil {
		if !p.Ready {
			p.Ready = true
			b.lobby(r)
		}
		b.playerRoom(u.ID)
		return
	}
	if r.Phase != "lobby" || len(r.Players) >= 12 {
		b.tell(u.ID, "Набор закрыт или комната заполнена.", nil, "")
		return
	}
	r.Players = append(r.Players, player{u.ID, playerName(u), true})
	b.tell(u.ID, "✅ Ты в игре. Вернись в группу и дождись запуска раунда.", row("👥 Моя комната", "room"), "")
	b.lobby(r)
}
func (b *bot) playerRoom(uid int64) {
	r := b.state.active(uid)
	if r == nil {
		b.menu(uid)
		return
	}
	switch r.Phase {
	case "lobby":
		b.tell(uid, fmt.Sprintf("✅ Ты в комнате %s. Старт и обсуждение проходят в группе.", r.Code), row("🚪 Выйти из комнаты", "leave"), "")
	case "cards":
		b.cardMenu(r, uid)
	case "voting":
		if r.Votes[uid] == 0 {
			b.ballot(r, uid)
		} else {
			b.tell(uid, "✅ Голос уже принят.", nil, "")
		}
	default:
		b.tell(uid, "Раунд идёт в группе. Если ты шпион, список локаций: /guess.", nil, "")
	}
}
func (b *bot) leave(uid int64) {
	r := b.state.active(uid)
	if r == nil {
		b.menu(uid)
		return
	}
	if r.Phase != "lobby" {
		b.tell(uid, "Выйти можно между раундами.", nil, "")
		return
	}
	for i, p := range r.Players {
		if p.ID == uid {
			r.Players = append(r.Players[:i], r.Players[i+1:]...)
			break
		}
	}
	if len(r.Players) == 0 {
		delete(b.state.Rooms, r.Group)
	} else {
		if r.Host == uid {
			r.Host = r.Players[0].ID
		}
		b.lobby(r)
	}
	b.menu(uid)
}

func (b *bot) answer(c *callback, text string) {
	f := fields{"callback_query_id": c.ID}
	if text != "" {
		f["text"] = text
	}
	if err := b.api.call(b.ctx, "answerCallbackQuery", f, "", "", nil); err != nil {
		b.logger.Print(err)
	}
}
func (b *bot) handleCallback(c *callback) {
	if c.Message == nil {
		return
	}
	chat, uid := c.Message.Chat.ID, c.From.ID
	parts := strings.Split(c.Data, ":")
	if chat == uid && len(parts) == 1 {
		b.answer(c, "")
		switch parts[0] {
		case "help":
			b.rules(uid)
		case "room":
			b.playerRoom(uid)
		case "leave":
			b.leave(uid)
		}
		return
	}
	if len(parts) == 3 && parts[0] == "check" {
		v, ok := b.checks[parts[1]]
		if !ok || v.UID != uid || v.Group != chat {
			b.answer(c, "Эта проверка недоступна.")
			return
		}
		delete(b.checks, parts[1])
		if time.Now().After(v.Deadline) || parts[2] != strconv.Itoa(v.Answer) {
			b.answer(c, "Проверка не пройдена. Отправь /spy ещё раз.")
			return
		}
		if b.state.Rooms[chat] != nil || b.state.active(uid) != nil {
			b.answer(c, "Комната уже существует или ты в другой игре.")
			return
		}
		r := &room{Group: chat, Code: code(), Host: uid, Phase: "lobby", Players: []player{{uid, playerName(c.From), false}}}
		b.state.Rooms[chat] = r
		b.answer(c, "Набор открыт!")
		b.lobby(r)
		return
	}
	if len(parts) < 3 {
		b.answer(c, "Кнопка устарела.")
		return
	}
	r := b.state.byCode(parts[1])
	if r == nil || r.Round != mustInt(parts[2]) || r.member(uid) == nil {
		b.answer(c, "Кнопка устарела.")
		return
	}
	action := parts[0]
	if action == "play" && len(parts) == 3 && chat == r.Group {
		if err := r.start(uid, time.Now()); err != nil {
			b.answer(c, err.Error())
			return
		}
		b.answer(c, "Раунд начался!")
		b.cleanupGroup(r)
		b.announce(r, "🃏 ВЫБОР КАРТОЧЕК\n\nОткрой личку с ботом и выбери номер. Есть 90 секунд.", "cards.mp4")
		for _, p := range r.Players {
			b.cardMenu(r, p.ID)
		}
		return
	}
	if chat != uid || len(parts) != 4 {
		b.answer(c, "Эта кнопка недоступна.")
		return
	}
	switch action {
	case "card":
		if err := r.pick(uid, mustInt(parts[3]), time.Now()); err != nil {
			b.answer(c, err.Error())
			if r.Phase == "cards" {
				b.cardMenu(r, uid)
			}
			return
		}
		b.answer(c, "Карточка твоя!")
		if len(r.Cards) == len(r.Players) {
			b.roles(r)
		} else {
			b.tell(uid, "✅ Карточка выбрана. Ждём остальных.", nil, "")
		}
	case "vote":
		target, _ := strconv.ParseInt(parts[3], 10, 64)
		if err := r.vote(uid, target, time.Now()); err != nil {
			b.answer(c, err.Error())
			return
		}
		b.answer(c, "Голос принят.")
		if len(r.Votes) == len(r.Players) {
			b.finish(r, "", true)
		}
	case "guess":
		n := mustInt(parts[3])
		if r.Phase != "playing" || r.Spy != uid || n < 0 || n >= len(locations) {
			b.answer(c, "Угадывать может только шпион во время обсуждения.")
			return
		}
		b.answer(c, "Ответ принят.")
		if n == r.Location {
			b.finish(r, "Шпион угадал локацию и победил!", false)
		} else {
			b.finish(r, "Шпион ошибся. Мирные победили!", false)
		}
	default:
		b.answer(c, "Кнопка устарела.")
	}
}
func mustInt(text string) int {
	n, err := strconv.Atoi(text)
	if err != nil {
		return -1
	}
	return n
}

func (b *bot) cardMenu(r *room, uid int64) {
	if r.Cards[uid] != 0 {
		b.tell(uid, "✅ Ты уже выбрал карточку.", nil, "")
		return
	}
	rows := [][]button{}
	taken := map[int]bool{}
	for _, n := range r.Cards {
		taken[n] = true
	}
	for n := 1; n <= len(r.Players); n++ {
		if taken[n] {
			continue
		}
		if len(rows) == 0 || len(rows[len(rows)-1]) == 3 {
			rows = append(rows, nil)
		}
		rows[len(rows)-1] = append(rows[len(rows)-1], button{Text: fmt.Sprintf("🃏 %d", n), Data: fmt.Sprintf("card:%s:%d:%d", r.Code, r.Round, n)})
	}
	b.tell(uid, "🃏 ВЫБЕРИ КАРТОЧКУ\n\nСвободные номера ниже. Каждый игрок может взять одну.", rows, "cards.mp4")
}
func (b *bot) roles(r *room) {
	r.assign()
	r.Phase = "countdown"
	r.Count = 3
	r.Deadline = time.Now().Add(time.Second)
	for _, p := range r.Players {
		media, text := fmt.Sprintf("location-%d.jpg", r.Location), "📍 Твоя локация: "+locations[r.Location]
		if p.ID == r.Spy {
			media, text = "spy-role.jpg", "🕵️ Ты шпион. Попытайся угадать локацию."
		}
		id, err := b.send(p.ID, text, nil, media, true)
		if err != nil {
			b.logger.Print(err)
			if member := r.member(p.ID); member != nil {
				member.Ready = false
			}
			b.finish(r, "Раунд отменён: "+p.Name+" должен заново открыть личку с ботом.", false)
			return
		}
		r.Roles[p.ID] = id
	}
	b.announce(r, "⏳ ДО ОБСУЖДЕНИЯ: 3\n\nЗапомни свою карточку. После отсчёта она исчезнет.", "countdown.mp4")
	r.Deadline = time.Now().Add(time.Second)
}
func (b *bot) hideRoles(r *room) {
	for uid, id := range r.Roles {
		if err := b.api.call(b.ctx, "deleteMessage", fields{"chat_id": uid, "message_id": id}, "", "", nil); err != nil {
			b.logger.Print(err)
		}
	}
	r.Roles = nil
}
func (b *bot) turn(r *room) {
	n := len(r.Players)
	i := r.Turn % n
	b.announce(r, fmt.Sprintf("🎙 №%d %s задаёт вопрос игроку №%d %s.\nОтветь на это сообщение вопросом или напиши /q вопрос.", i+1, r.Players[i].Name, (i+1)%n+1, r.Players[(i+1)%n].Name), "")
}
func (b *bot) openVoting(r *room) {
	r.Phase = "voting"
	r.Deadline = time.Now().Add(time.Minute)
	b.announce(r, "🗳 ГОЛОСОВАНИЕ\n\nВыбери подозреваемого в личке с ботом. На голосование — 60 секунд.", "vote.mp4")
	for _, p := range r.Players {
		b.ballot(r, p.ID)
	}
}
func (b *bot) ballot(r *room, uid int64) {
	buttons := [][]button{}
	for _, p := range r.Players {
		if p.ID != uid {
			buttons = append(buttons, []button{{Text: p.Name, Data: fmt.Sprintf("vote:%s:%d:%d", r.Code, r.Round, p.ID)}})
		}
	}
	b.tell(uid, "🗳 КТО ШПИОН?\n\nНажми на имя подозреваемого. Голос можно отдать только один раз.", buttons, "vote.mp4")
}
func (b *bot) guessMenu(uid int64) {
	r := b.state.active(uid)
	if r == nil || r.Phase != "playing" || r.Spy != uid {
		b.tell(uid, "Угадывать может только шпион во время обсуждения.", nil, "")
		return
	}
	rows := [][]button{}
	for i, name := range locations {
		rows = append(rows, []button{{Text: name, Data: fmt.Sprintf("guess:%s:%d:%d", r.Code, r.Round, i)}})
	}
	b.tell(uid, "📍 Угадай локацию. Выбор окончательный.", rows, "")
}
func (b *bot) finish(r *room, text string, voted bool) {
	scene := "result.mp4"
	if voted {
		switch id := r.ejected(); {
		case id == 0:
			text = "Шпион победил. Голосование никого не исключило."
		case id == r.Spy:
			scene = "eject-spy.mp4"
			text = "Мирные победили. Исключён " + r.member(id).Name + " — это шпион."
		default:
			scene = "eject-civil.mp4"
			text = "Шпион победил. Исключён " + r.member(id).Name + " — это мирный игрок."
		}
	}
	if spy := r.member(r.Spy); spy != nil && r.Phase != "cards" {
		text += "\n\nШпион: " + spy.Name + "\nЛокация: " + locations[r.Location]
	}
	b.hideRoles(r)
	r.reset()
	b.tell(r.Group, "🏁 "+text, b.lobbyButtons(r), scene)
}
func (b *bot) cleanupGroup(r *room) {
	for len(r.GroupMessages) > 0 {
		batch := min(100, len(r.GroupMessages))
		if err := b.api.call(b.ctx, "deleteMessages", fields{"chat_id": r.Group, "message_ids": r.GroupMessages[:batch]}, "", "", nil); err != nil {
			b.logger.Printf("group cleanup: %v", err)
		}
		r.GroupMessages = r.GroupMessages[batch:]
	}
}
func (b *bot) tick(now time.Time) bool {
	changed := false
	for _, r := range b.state.Rooms {
		if r.Deadline.IsZero() || now.Before(r.Deadline) {
			continue
		}
		changed = true
		switch r.Phase {
		case "cards":
			b.roles(r)
		case "countdown":
			r.Count--
			if r.Count > 0 {
				b.announce(r, fmt.Sprintf("⏳ %d", r.Count), "")
				r.Deadline = time.Now().Add(time.Second)
			} else {
				b.hideRoles(r)
				r.Phase = "playing"
				r.Deadline = time.Now().Add(5 * time.Minute)
				b.announce(r, "▶️ ОБСУЖДЕНИЕ\n\n5 минут на вопросы. Потом автоматически начнётся голосование.", "discussion.mp4")
				b.tell(r.Spy, "Можешь попытаться угадать локацию: /guess", nil, "")
				b.turn(r)
			}
		case "playing":
			b.openVoting(r)
		case "voting":
			b.finish(r, "", true)
		default:
			r.Deadline = time.Time{}
		}
	}
	return changed
}

func (b *bot) run() error {
	if err := b.api.call(b.ctx, "getMe", nil, "", "", &b.me); err != nil {
		return err
	}
	var webhook struct {
		URL string `json:"url"`
	}
	if err := b.api.call(b.ctx, "getWebhookInfo", nil, "", "", &webhook); err != nil {
		return err
	}
	if webhook.URL != "" {
		return errors.New("у этого бота включён webhook; остановите прежний способ доставки обновлений")
	}
	b.logger.Printf("Telegram connected as @%s", b.me.Username)
	batches := make(chan []update)
	committed := make(chan int64)
	go func() {
		var offset int64
		for b.ctx.Err() == nil {
			var updates []update
			err := b.api.call(b.ctx, "getUpdates", fields{"offset": offset, "timeout": 25, "allowed_updates": []string{"message", "callback_query"}}, "", "", &updates)
			if err != nil {
				if b.ctx.Err() != nil {
					return
				}
				b.logger.Print(err)
				select {
				case <-time.After(3 * time.Second):
				case <-b.ctx.Done():
					return
				}
				continue
			}
			if len(updates) == 0 {
				continue
			}
			select {
			case batches <- updates:
			case <-b.ctx.Done():
				return
			}
			select {
			case offset = <-committed:
			case <-b.ctx.Done():
				return
			}
		}
	}()
	tick := time.NewTicker(time.Second)
	defer tick.Stop()
	for {
		select {
		case <-b.ctx.Done():
			return nil
		case batch := <-batches:
			for _, u := range batch {
				if u.Message != nil {
					b.handleMessage(u.Message)
				}
				if u.Callback != nil {
					b.handleCallback(u.Callback)
				}
			}
			if err := b.state.save(); err != nil {
				return err
			}
			select {
			case committed <- batch[len(batch)-1].ID + 1:
			case <-b.ctx.Done():
				return nil
			}
		case now := <-tick.C:
			if b.tick(now) {
				if err := b.state.save(); err != nil {
					return err
				}
			}
		}
	}
}
