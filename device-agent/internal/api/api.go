// Package api is how the agent talks to its dashboard: through the dashboard's Redis relay,
// never to the dashboard itself (see main/apps/agent_api/relay.py, "devices" and
// "pairing", and bridge.py `handle_pairing` and `handle_device`).
//
//	pairing   RPUSH <prefix>:pair:in {"t":"pair","body":"…"}, then GET <prefix>:pair:out:<nonce>
//	check-in  RPUSH <prefix>:d:<id>:in {"t":"ci","body":"{\"at\":…,\"report\":{…}}","sig":"…"}
//	settings  GET   <prefix>:d:<id>:cfg  {"body":"{\"checkin_seconds\":…,\"controls\":…}","sig":"…"}
//	blocklist GET   <prefix>:d:<id>:bl   {"body":"{\"version\":…,\"gz\":…}","sig":"…"}
//
// Everything after pairing is signed with HMAC-SHA256 under a key this computer made and
// sent while pairing; Redis never sees it.
package api

import (
	"bytes"
	"compress/gzip"
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"strings"
	"time"

	"github.com/sabbir-mahmud/servers-monitor/device-agent/internal/relay"
)

// ErrUnpaired: the relay no longer accepts this computer's login (the device was removed
// on the dashboard, or paired again elsewhere). Stop reporting; it can be paired again.
var ErrUnpaired = errors.New("the dashboard removed this device")

// Error is a refusal from the dashboard, in its own words ("That code isn't valid").
type Error struct{ Message string }

func (e *Error) Error() string { return e.Message }

// PairWait: how long to wait for the dashboard to answer a pairing request.
var PairWait = 60 * time.Second

type Device struct {
	Name    string `json:"name"`
	Account string `json:"account"`
}

// Login is this computer's own relay login, from pairing.
type Login struct {
	URL       string `json:"url"`
	Inbox     string `json:"inbox"`
	Config    string `json:"config"`
	Blocklist string `json:"blocklist"`
}

type PairResult struct {
	Disclosure     string `json:"disclosure"`
	Device         Device `json:"device"`
	CheckinSeconds int    `json:"checkin_seconds"`
	Relay          Login  `json:"relay"`
	Key            string `json:"-"` // hex: the signing key this computer made
}

// Pair asks the dashboard through the relay. With preview it only shows who will see this
// computer and the code stays valid; without, it pairs and returns this computer's login.
func Pair(pairURL, code string, info map[string]any, preview bool) (*PairResult, error) {
	addr, err := relay.Parse(pairURL)
	if err != nil {
		return nil, err
	}
	if addr.Prefix == "" {
		return nil, errors.New("the relay address has no ?prefix=: copy the whole line from the dashboard")
	}
	conn, err := relay.Dial(pairURL)
	if err != nil {
		if relay.Denied(err) {
			return nil, errors.New("the relay refused this pairing address: get a new line from the dashboard")
		}
		return nil, err
	}
	defer conn.Close()

	nonce := randomHex(16)
	body := map[string]any{"at": time.Now().Unix(), "nonce": nonce, "code": code, "info": info}
	key := ""
	if preview {
		body["preview"] = true
	} else {
		key = randomHex(32)
		body["secret"] = key
	}
	text, _ := json.Marshal(body)
	msg, _ := json.Marshal(map[string]any{"t": "pair", "body": string(text)})
	if err := conn.Push(addr.Prefix+":pair:in", string(msg)); err != nil {
		return nil, err
	}

	answerKey := addr.Prefix + ":pair:out:" + nonce
	deadline := time.Now().Add(PairWait)
	for {
		raw, ok, err := conn.Get(answerKey)
		if err != nil {
			return nil, err
		}
		if ok {
			var a struct {
				PairResult
				State   string `json:"state"`
				Message string `json:"message"`
			}
			if err := json.Unmarshal([]byte(raw), &a); err != nil {
				return nil, fmt.Errorf("the dashboard sent an unexpected answer: %w", err)
			}
			switch {
			case a.State == "error":
				return nil, &Error{Message: a.Message}
			case preview && a.State == "preview":
				return &a.PairResult, nil
			case !preview && a.State == "paired" && a.Relay.URL != "" && a.Relay.Inbox != "":
				a.PairResult.Key = key
				return &a.PairResult, nil
			}
			return nil, errors.New("the dashboard sent an unexpected answer")
		}
		if time.Now().After(deadline) {
			return nil, errors.New("the dashboard didn't answer through the relay. Is its relay process " +
				"running? The code still works: try again in a minute")
		}
		time.Sleep(500 * time.Millisecond)
	}
}

// Controls: what the dashboard wants beyond health. Nil means none for this computer
// (not a child's or staff computer): stop collecting and remove any blocking.
type Controls struct {
	ScreenTime  bool   `json:"screen_time"`
	WebActivity bool   `json:"web_activity"`
	Location    bool   `json:"location"`
	Blocking    string `json:"blocking"` // blocklist version; "" = nothing blocked
}

// Settings the dashboard left for this computer.
type Settings struct {
	CheckinSeconds int       `json:"checkin_seconds"`
	Controls       *Controls `json:"controls"`
	Device         Device    `json:"device"`
	Digest         string    `json:"-"` // echoed back in check-ins as "config"
}

// Client is a paired computer's connection to the relay.
type Client struct {
	login Login
	key   []byte
	conn  *relay.Conn
}

func New(login Login, keyHex string) (*Client, error) {
	key, err := hex.DecodeString(keyHex)
	if err != nil || len(key) != 32 {
		return nil, errors.New("the saved signing key is damaged: pair again")
	}
	return &Client{login: login, key: key}, nil
}

func (c *Client) Close() {
	if c.conn != nil {
		_ = c.conn.Close()
		c.conn = nil
	}
}

func (c *Client) dial() (*relay.Conn, error) {
	if c.conn != nil {
		return c.conn, nil
	}
	conn, err := relay.Dial(c.login.URL)
	if err != nil {
		return nil, c.translate(err)
	}
	c.conn = conn
	return conn, nil
}

func (c *Client) translate(err error) error {
	if relay.Denied(err) {
		return ErrUnpaired
	}
	return err
}

// Checkin sends one report, signed.
func (c *Client) Checkin(report map[string]any) error {
	conn, err := c.dial()
	if err != nil {
		return err
	}
	body, err := json.Marshal(map[string]any{"at": time.Now().Unix(), "report": report})
	if err != nil {
		return err
	}
	msg, _ := json.Marshal(map[string]any{"t": "ci", "body": string(body), "sig": c.sign(string(body))})
	if err := conn.Push(c.login.Inbox, string(msg)); err != nil {
		c.Close()
		return c.translate(err)
	}
	return nil
}

// Settings reads what the dashboard left for this computer; nil, nil when it has left
// nothing yet (keep what you have).
func (c *Client) Settings() (*Settings, error) {
	body, ok, err := c.read(c.login.Config)
	if err != nil || !ok {
		return nil, err
	}
	var s Settings
	if err := json.Unmarshal([]byte(body), &s); err != nil {
		return nil, fmt.Errorf("unreadable settings from the dashboard: %w", err)
	}
	sum := sha256.Sum256([]byte(body))
	s.Digest = hex.EncodeToString(sum[:])[:16]
	return &s, nil
}

// Blocklist reads the hosts lines ("address name") the dashboard left for this computer
// and their version. ok is false when there is none yet.
func (c *Client) Blocklist() (lines []string, version string, ok bool, err error) {
	body, ok, err := c.read(c.login.Blocklist)
	if err != nil || !ok {
		return nil, "", false, err
	}
	var b struct {
		Version string `json:"version"`
		GZ      string `json:"gz"`
	}
	if err := json.Unmarshal([]byte(body), &b); err != nil {
		return nil, "", false, fmt.Errorf("unreadable blocklist: %w", err)
	}
	packed, err := base64.StdEncoding.DecodeString(b.GZ)
	if err != nil {
		return nil, "", false, fmt.Errorf("unreadable blocklist: %w", err)
	}
	zr, err := gzip.NewReader(bytes.NewReader(packed))
	if err != nil {
		return nil, "", false, fmt.Errorf("unreadable blocklist: %w", err)
	}
	raw, err := io.ReadAll(io.LimitReader(zr, 32<<20))
	if err != nil {
		return nil, "", false, fmt.Errorf("unreadable blocklist: %w", err)
	}
	text := strings.TrimSpace(string(raw))
	if text == "" {
		return []string{}, b.Version, true, nil
	}
	return strings.Split(text, "\n"), b.Version, true, nil
}

// read GETs a signed value and returns its body once the signature checks out.
func (c *Client) read(key string) (string, bool, error) {
	if key == "" {
		return "", false, nil
	}
	conn, err := c.dial()
	if err != nil {
		return "", false, err
	}
	raw, ok, err := conn.Get(key)
	if err != nil {
		c.Close()
		return "", false, c.translate(err)
	}
	if !ok {
		return "", false, nil
	}
	var sealed struct {
		Body string `json:"body"`
		Sig  string `json:"sig"`
	}
	if json.Unmarshal([]byte(raw), &sealed) != nil || !hmac.Equal([]byte(sealed.Sig), []byte(c.sign(sealed.Body))) {
		return "", false, errors.New("the relay holds settings this dashboard didn't sign: ignoring them")
	}
	return sealed.Body, true, nil
}

func (c *Client) sign(body string) string {
	m := hmac.New(sha256.New, c.key)
	m.Write([]byte(body))
	return hex.EncodeToString(m.Sum(nil))
}

func randomHex(n int) string {
	b := make([]byte, n)
	if _, err := rand.Read(b); err != nil {
		panic(err) // the system has no randomness: nothing safe to do
	}
	return hex.EncodeToString(b)
}
