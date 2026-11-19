package main

import (
	"context"
	"embed"
	"log"
	"os"
	"os/signal"
	"strings"
	"syscall"
)

//go:embed art/*.mp4 art/guide.jpg art/location-*.jpg art/spy-role.jpg
var art embed.FS

func main() {
	token := strings.TrimSpace(os.Getenv("BOT_TOKEN"))
	if token == "" {
		log.Fatal("set BOT_TOKEN")
	}
	api := newTelegram(token)
	if len(os.Args) == 2 && os.Args[1] == "--check" {
		var me user
		if err := api.call(context.Background(), "getMe", nil, "", "", &me); err != nil {
			log.Fatal(err)
		}
		log.Printf("Telegram API OK: @%s", me.Username)
		return
	}
	path := os.Getenv("SPY_STATE")
	if path == "" {
		path = "data/rooms.json"
	}
	state, err := loadState(path)
	if err != nil {
		log.Fatalf("load state: %v", err)
	}
	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer cancel()
	app := &bot{ctx: ctx, api: api, logger: log.Default(), state: state, checks: map[string]check{}}
	if err := app.run(); err != nil {
		log.Fatal(err)
	}
}
