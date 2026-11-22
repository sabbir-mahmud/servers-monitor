package api

import (
	"encoding/hex"
	"encoding/json"
	"errors"
	"strings"
	"testing"
	"time"

	"github.com/sabbir-mahmud/servers-monitor/device-agent/internal/relay/relaytest"
)

// A dashboard on the other side of a fake relay: it answers pairing requests for code
// K7PQ3MXA the way bridge.py `handle_pairing` does.
func fakeDashboard(t *testing.T, seen *[]map[string]any) (*relaytest.Server, string) {
	t.Helper()
	srv := relaytest.New(t)
	srv.AddUser("sm-pc", "pairpw", "sm:pair:")
	srv.AddUser("sm-d-1", "devpw", "sm:d:1:")
	srv.OnPush = func(key, value string) {
		if key != "sm:pair:in" {
			return
		}
		var msg struct{ T, Body string }
		var body map[string]any
		if json.Unmarshal([]byte(value), &msg) != nil || json.Unmarshal([]byte(msg.Body), &body) != nil {
			t.Errorf("unreadable pairing request %q", value)
			return
		}
		*seen = append(*seen, body)
		answer := `{"state":"error","message":"That code isn't valid."}`
		switch {
		case body["code"] != "K7PQ3MXA":
		case body["preview"] == true:
			answer = `{"state":"preview","disclosure":"The Rahmans will see...","device":{"name":"Sara's Mac","account":"The Rahmans"}}`
		default:
			answer = `{"state":"paired","checkin_seconds":300,"device":{"name":"Sara's Mac","account":"The Rahmans"},` +
				`"relay":{"url":"` + srv.URL("sm-d-1") + `","inbox":"sm:d:1:in","config":"sm:d:1:cfg","blocklist":"sm:d:1:bl"}}`
		}
		srv.Set("sm:pair:out:"+body["nonce"].(string), answer)
	}
	return srv, srv.URL("sm-pc") + "/?prefix=sm"
}

func TestPreviewThenPairThenCheckin(t *testing.T) {
	var seen []map[string]any
	srv, pairURL := fakeDashboard(t, &seen)
	info := map[string]any{"platform": "macos"}

	p, err := Pair(pairURL, "K7PQ3MXA", info, true)
	if err != nil || p.Disclosure == "" || p.Relay.URL != "" || p.Key != "" {
		t.Fatalf("preview: %+v %v", p, err)
	}
	r, err := Pair(pairURL, "K7PQ3MXA", info, false)
	if err != nil || r.Relay.Inbox != "sm:d:1:in" || r.Device.Account != "The Rahmans" || len(r.Key) != 64 {
		t.Fatalf("pair: %+v %v", r, err)
	}
	if seen[0]["preview"] != true || seen[0]["secret"] != nil || seen[1]["preview"] != nil ||
		seen[1]["secret"] != r.Key || seen[1]["info"].(map[string]any)["platform"] != "macos" {
		t.Fatalf("bodies: %v", seen)
	}

	c, err := New(r.Relay, r.Key)
	if err != nil {
		t.Fatal(err)
	}
	defer c.Close()
	if err := c.Checkin(map[string]any{"cpu": map[string]any{"percent": 5}}); err != nil {
		t.Fatal(err)
	}
	key, _ := hex.DecodeString(r.Key)
	body, ok := relaytest.Verify(key, srv.Pop("sm:d:1:in"))
	if !ok || !strings.Contains(body, `"report":{"cpu":{"percent":5}}`) || !strings.Contains(body, `"at":`) {
		t.Fatalf("check-in not signed with the key it made: %q %v", body, ok)
	}
}

func TestRefusalsKeepTheDashboardsWords(t *testing.T) {
	var seen []map[string]any
	_, pairURL := fakeDashboard(t, &seen)
	_, err := Pair(pairURL, "WRONG000", nil, true)
	var apiErr *Error
	if !errors.As(err, &apiErr) || apiErr.Message != "That code isn't valid." {
		t.Fatalf("got %v", err)
	}
}

func TestNoAnswerMeansTheRelayProcessIsDown(t *testing.T) {
	srv := relaytest.New(t)
	srv.AddUser("sm-pc", "pairpw", "sm:pair:")
	defer func(d time.Duration) { PairWait = d }(PairWait)
	PairWait = time.Second
	if _, err := Pair(srv.URL("sm-pc")+"/?prefix=sm", "K7PQ3MXA", nil, true); err == nil ||
		!strings.Contains(err.Error(), "didn't answer") {
		t.Fatalf("got %v", err)
	}
}

func TestARemovedDeviceIsUnpaired(t *testing.T) {
	srv := relaytest.New(t)
	srv.AddUser("sm-d-1", "devpw", "sm:d:1:")
	login := Login{URL: srv.URL("sm-d-1"), Inbox: "sm:d:1:in", Config: "sm:d:1:cfg"}
	srv.RemoveUser("sm-d-1") // the dashboard deleted its login
	c, _ := New(login, strings.Repeat("ab", 32))
	defer c.Close()
	if err := c.Checkin(nil); !errors.Is(err, ErrUnpaired) {
		t.Fatalf("a removed device must get ErrUnpaired, got %v", err)
	}
}

func TestSettingsAndBlocklistMustBeSignedByTheDashboard(t *testing.T) {
	srv := relaytest.New(t)
	srv.AddUser("sm-d-1", "devpw", "sm:d:1:")
	key := []byte(strings.Repeat("k", 32))
	login := Login{URL: srv.URL("sm-d-1"), Inbox: "sm:d:1:in", Config: "sm:d:1:cfg", Blocklist: "sm:d:1:bl"}
	c, _ := New(login, hex.EncodeToString(key))
	defer c.Close()

	if s, err := c.Settings(); s != nil || err != nil {
		t.Fatalf("nothing left yet must be nil, nil: %+v %v", s, err)
	}
	body := `{"checkin_seconds":600,"controls":{"blocking":"v1"},"device":{"name":"Mac","account":"A"}}`
	srv.Set("sm:d:1:cfg", relaytest.Seal(key, body))
	s, err := c.Settings()
	if err != nil || s.CheckinSeconds != 600 || s.Controls.Blocking != "v1" || len(s.Digest) != 16 {
		t.Fatalf("settings: %+v %v", s, err)
	}
	srv.Set("sm:d:1:bl", relaytest.Seal(key, relaytest.BlocklistBody("v1", "0.0.0.0 a.example", "0.0.0.0 b.example")))
	if lines, v, ok, err := c.Blocklist(); err != nil || !ok || v != "v1" || len(lines) != 2 {
		t.Fatalf("blocklist: %v %q %v %v", lines, v, ok, err)
	}
	// Someone with the relay's admin password, but not the device's key, can't steer it.
	srv.Set("sm:d:1:cfg", relaytest.Seal([]byte("not the key"), body))
	if _, err := c.Settings(); err == nil {
		t.Fatal("forged settings must be refused")
	}
}
