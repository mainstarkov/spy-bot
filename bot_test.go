package main

import (
	"context"
	"encoding/json"
	"io"
	"log"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"
)

func TestRoundFromGroup(t *testing.T) {
	type delivery struct{ Method, Chat, Caption, Asset string }
	var sent []delivery
	next := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		method := strings.TrimPrefix(r.URL.Path, "/")
		var chat, caption, asset string
		if strings.HasPrefix(r.Header.Get("Content-Type"), "multipart/") {
			if err := r.ParseMultipartForm(3 << 20); err != nil {
				t.Error(err)
				w.WriteHeader(400)
				return
			}
			chat, caption = r.FormValue("chat_id"), r.FormValue("caption")
			for _, files := range r.MultipartForm.File {
				if len(files) > 0 {
					asset = files[0].Filename
				}
			}
		} else {
			var body map[string]json.RawMessage
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				t.Error(err)
				w.WriteHeader(400)
				return
			}
			chat = string(body["chat_id"])
		}
		sent = append(sent, delivery{method, chat, caption, asset})
		next++
		_ = json.NewEncoder(w).Encode(fields{"ok": true, "result": fields{"message_id": next}})
	}))
	defer server.Close()
	s, err := loadState(filepath.Join(t.TempDir(), "rooms.json"))
	if err != nil {
		t.Fatal(err)
	}
	b := &bot{ctx: context.Background(), api: &telegram{server.URL + "/", server.Client()}, logger: log.New(io.Discard, "", 0), me: user{ID: 99, Username: "testbot"}, state: s, checks: map[string]check{}}
	click := func(chat, uid int64, data string) {
		m := &message{}
		m.Chat.ID = chat
		b.handleCallback(&callback{ID: "test", From: user{ID: uid, First: "Игрок " + strconv.FormatInt(uid, 10)}, Message: m, Data: data})
	}
	group := int64(-123)
	b.openLobby(group, user{ID: 1, First: "Ведущий"})
	for key, c := range b.checks {
		click(group, 2, "check:"+key+":"+strconv.Itoa(c.Answer))
		if s.Rooms[group] != nil {
			t.Fatal("wrong user passed verification")
		}
		click(group, 1, "check:"+key+":"+strconv.Itoa(c.Answer))
	}
	r := s.Rooms[group]
	if r == nil || r.Phase != "lobby" {
		t.Fatal("lobby did not open")
	}
	play := "play:" + r.Code + ":0"
	click(group, 1, play)
	if r.Phase != "lobby" {
		t.Fatal("started with fewer than three players")
	}
	for uid := int64(1); uid <= 3; uid++ {
		b.join(user{ID: uid, First: "Игрок"}, r.Code)
	}
	var lobbyPosts, lobbyEdits int
	for _, d := range sent {
		if d.Asset == "lobby.mp4" {
			lobbyPosts++
		}
		if d.Method == "editMessageCaption" {
			lobbyEdits++
		}
	}
	if lobbyPosts != 1 || lobbyEdits != 3 {
		t.Fatalf("want one lobby post and three updates, got %d and %d", lobbyPosts, lobbyEdits)
	}
	if len(b.lobbyButtons(r)) != 2 {
		t.Fatal("ready lobby should show the start button")
	}
	click(group, 2, play)
	if r.Phase != "lobby" {
		t.Fatal("guest started the round")
	}
	click(-999, 1, play)
	if r.Phase != "lobby" {
		t.Fatal("other chat could start")
	}
	click(group, 1, play)
	if r.Phase != "cards" {
		t.Fatal("host start failed")
	}
	click(1, 1, "card:"+r.Code+":1:1")
	click(2, 2, "card:"+r.Code+":1:1")
	if len(r.Cards) != 1 {
		t.Fatal("duplicate card accepted")
	}
	click(2, 2, "card:"+r.Code+":1:2")
	click(3, 3, "card:"+r.Code+":1:3")
	if r.Phase != "countdown" || len(r.Roles) != 3 {
		t.Fatal("roles not delivered")
	}
	for r.Phase == "countdown" {
		b.tick(r.Deadline)
	}
	if r.Phase != "playing" || len(r.Roles) != 0 {
		t.Fatal("countdown did not hide roles")
	}
	b.tick(r.Deadline)
	if r.Phase != "voting" {
		t.Fatal("no vote")
	}
	for uid := int64(1); uid <= 3; uid++ {
		target := r.Spy
		if target == uid {
			target = uid%3 + 1
		}
		click(uid, uid, "vote:"+r.Code+":1:"+strconv.FormatInt(target, 10))
	}
	if r.Phase != "lobby" {
		t.Fatal("round not finished")
	}
	var roles, deleted, animations int
	for _, d := range sent {
		if d.Method == "sendSticker" {
			t.Fatal("unexpected sticker")
		}
		if strings.HasPrefix(d.Asset, "location-") || d.Asset == "spy-role.jpg" {
			roles++
			if d.Chat == strconv.FormatInt(group, 10) {
				t.Fatal("role leaked to group")
			}
		}
		if d.Method == "deleteMessage" {
			deleted++
		}
		if d.Asset == "vote.mp4" {
			animations++
			if d.Caption == "" {
				t.Fatal("voting media has no caption")
			}
		}
	}
	if roles != 3 || deleted != 3 || animations != 4 {
		t.Fatalf("roles %d, deleted %d, animations %d", roles, deleted, animations)
	}
	if sent[len(sent)-1].Asset != "eject-spy.mp4" {
		t.Fatal("did not reveal elected spy")
	}
	if err = s.save(); err != nil {
		t.Fatal(err)
	}
	loaded, err := loadState(s.path)
	if err != nil || loaded.Rooms[group].Round != 1 {
		t.Fatalf("state recovery: %v", err)
	}
}

func TestDeadlinesAndTie(t *testing.T) {
	now := time.Now()
	r := room{Host: 1, Phase: "lobby", Players: []player{{1, "A", true}, {2, "B", true}, {3, "C", true}}}
	if err := r.start(1, now); err != nil {
		t.Fatal(err)
	}
	if err := r.pick(1, 1, r.Deadline); err == nil {
		t.Fatal("accepted expired pick")
	}
	r.assign()
	if r.Spy == 0 || len(r.Cards) != 3 {
		t.Fatal("assignment incomplete")
	}
	r.Phase = "voting"
	if err := r.vote(1, 1, now); err == nil {
		t.Fatal("accepted self vote")
	}
	for id := int64(1); id <= 3; id++ {
		if err := r.vote(id, id%3+1, now); err != nil {
			t.Fatal(err)
		}
	}
	if r.ejected() != 0 {
		t.Fatal("ejected anyone in a tie")
	}
}
