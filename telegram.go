package main

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"mime/multipart"
	"net/http"
	"path/filepath"
	"time"
)

type user struct {
	ID       int64  `json:"id"`
	First    string `json:"first_name"`
	Last     string `json:"last_name"`
	Username string `json:"username"`
	Bot      bool   `json:"is_bot"`
}
type message struct {
	ID   int `json:"message_id"`
	Chat struct {
		ID   int64  `json:"id"`
		Type string `json:"type"`
	} `json:"chat"`
	From  user     `json:"from"`
	Text  string   `json:"text"`
	Reply *message `json:"reply_to_message"`
}
type callback struct {
	ID      string   `json:"id"`
	From    user     `json:"from"`
	Message *message `json:"message"`
	Data    string   `json:"data"`
}
type update struct {
	ID       int64     `json:"update_id"`
	Message  *message  `json:"message"`
	Callback *callback `json:"callback_query"`
}
type button struct {
	Text string `json:"text"`
	Data string `json:"callback_data,omitempty"`
	URL  string `json:"url,omitempty"`
}
type fields map[string]any

type telegram struct {
	base   string
	client *http.Client
}

func newTelegram(token string) *telegram {
	return &telegram{"https://api.telegram.org/bot" + token + "/", &http.Client{Timeout: 40 * time.Second}}
}

func (t *telegram) call(ctx context.Context, method string, f fields, fileField, name string, dst any) error {
	var body bytes.Buffer
	var contentType string
	if name == "" {
		contentType = "application/json"
		if err := json.NewEncoder(&body).Encode(f); err != nil {
			return err
		}
	} else {
		w := multipart.NewWriter(&body)
		contentType = w.FormDataContentType()
		for key, value := range f {
			var text string
			if str, ok := value.(string); ok {
				text = str
			} else {
				v, err := json.Marshal(value)
				if err != nil {
					return err
				}
				text = string(v)
			}
			if err := w.WriteField(key, text); err != nil {
				return err
			}
		}
		data, err := art.ReadFile("art/" + name)
		if err != nil {
			return fmt.Errorf("asset %s: %w", name, err)
		}
		part, err := w.CreateFormFile(fileField, filepath.Base(name))
		if err != nil {
			return err
		}
		if _, err = part.Write(data); err != nil {
			return err
		}
		if err = w.Close(); err != nil {
			return err
		}
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, t.base+method, &body)
	if err != nil {
		return fmt.Errorf("%s: invalid request", method)
	}
	req.Header.Set("Content-Type", contentType)
	res, err := t.client.Do(req)
	if err != nil {
		if e := ctx.Err(); e != nil {
			return e
		}
		// HTTP errors can include the token in the request URL.
		return fmt.Errorf("%s: network request failed", method)
	}
	defer res.Body.Close()
	var response struct {
		OK          bool            `json:"ok"`
		Code        int             `json:"error_code"`
		Result      json.RawMessage `json:"result"`
		Description string          `json:"description"`
	}
	if err = json.NewDecoder(io.LimitReader(res.Body, 4<<20)).Decode(&response); err != nil {
		return fmt.Errorf("%s: invalid Telegram response (HTTP %d)", method, res.StatusCode)
	}
	if !response.OK {
		return fmt.Errorf("%s: Telegram %d: %s", method, response.Code, response.Description)
	}
	if dst != nil {
		return json.Unmarshal(response.Result, dst)
	}
	return nil
}

func (b *bot) send(chat int64, text string, buttons [][]button, media string, protect bool) (int, error) {
	f := fields{"chat_id": chat, "protect_content": protect}
	if len(buttons) > 0 {
		f["reply_markup"] = fields{"inline_keyboard": buttons}
	}
	method, fileField := "sendMessage", ""
	if media != "" {
		f["caption"] = text
		method, fileField = "sendPhoto", "photo"
		if filepath.Ext(media) == ".mp4" {
			method, fileField = "sendAnimation", "animation"
		}
	} else {
		f["text"] = text
	}
	var m message
	if err := b.api.call(b.ctx, method, f, fileField, media, &m); err != nil {
		if media == "" {
			return 0, err
		}
		b.logger.Printf("media %s: %v; using text", media, err)
		delete(f, "caption")
		f["text"] = text
		if err = b.api.call(b.ctx, "sendMessage", f, "", "", &m); err != nil {
			return 0, err
		}
	}
	return m.ID, nil
}
