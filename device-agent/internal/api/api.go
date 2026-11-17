// Package api talks to the dashboard's device API: POST /device/v1/pair and
// POST /device/v1/checkin (see main/apps/devices/api.py).
package api

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"strings"
	"time"
)

// ErrUnpaired: the dashboard no longer knows this token (the device was removed there, or
// paired again elsewhere). Stop reporting; an administrator can pair it again.
var ErrUnpaired = errors.New("the dashboard removed this device")

// Error is a refusal from the dashboard, with its own words ("That code isn't valid").
type Error struct {
	Status  int
	Message string
}

func (e *Error) Error() string { return e.Message }

type Device struct {
	Name    string `json:"name"`
	Account string `json:"account"`
}

type PairResult struct {
	Token          string `json:"token"`
	Disclosure     string `json:"disclosure"`
	Device         Device `json:"device"`
	CheckinSeconds int    `json:"checkin_seconds"`
}

type Client struct {
	Server    string
	UserAgent string
	HTTP      *http.Client
}

func New(server, userAgent string) *Client {
	return &Client{Server: server, UserAgent: userAgent, HTTP: &http.Client{Timeout: 20 * time.Second}}
}

// Pair with preview=true shows who will see this device without using up the code.
func (c *Client) Pair(code string, info map[string]any, preview bool) (*PairResult, error) {
	body := map[string]any{"code": code}
	for k, v := range info {
		body[k] = v
	}
	if preview {
		body["preview"] = true
	}
	var out PairResult
	if err := c.post("/device/v1/pair", "", body, &out); err != nil {
		return nil, err
	}
	if !preview && !strings.HasPrefix(out.Token, "ngd_") {
		return nil, errors.New("the dashboard sent an unexpected answer")
	}
	return &out, nil
}

// Checkin sends one report and returns how often the dashboard wants them (seconds).
func (c *Client) Checkin(token string, report map[string]any) (int, error) {
	var out struct {
		CheckinSeconds int `json:"checkin_seconds"`
	}
	if err := c.post("/device/v1/checkin", token, report, &out); err != nil {
		return 0, err
	}
	return out.CheckinSeconds, nil
}

func (c *Client) post(path, token string, body any, out any) error {
	data, err := json.Marshal(body)
	if err != nil {
		return err
	}
	req, err := http.NewRequest(http.MethodPost, c.Server+path, bytes.NewReader(data))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Accept", "application/json")
	req.Header.Set("User-Agent", c.UserAgent)
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	resp, err := c.HTTP.Do(req)
	if err != nil {
		return fmt.Errorf("can't reach %s: %w", c.Server, err)
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if resp.StatusCode == http.StatusUnauthorized && token != "" {
		return ErrUnpaired
	}
	if resp.StatusCode >= 300 {
		var e struct {
			Error string `json:"error"`
		}
		if json.Unmarshal(raw, &e) != nil || e.Error == "" {
			e.Error = fmt.Sprintf("the dashboard answered HTTP %d", resp.StatusCode)
		}
		return &Error{Status: resp.StatusCode, Message: e.Error}
	}
	if err := json.Unmarshal(raw, out); err != nil {
		return fmt.Errorf("the dashboard sent an unexpected answer: %w", err)
	}
	return nil
}

// CheckServer normalizes the dashboard address and refuses plain http:// except on this
// machine or the local network (where the dashboard may run without a certificate).
// NEXGUARD_ALLOW_HTTP=1 lifts that.
func CheckServer(raw string) (string, error) {
	raw = strings.TrimSpace(raw)
	if !strings.Contains(raw, "://") {
		raw = "https://" + raw
	}
	u, err := url.Parse(raw)
	if err != nil || u.Host == "" || (u.Scheme != "https" && u.Scheme != "http") {
		return "", fmt.Errorf("%q isn't a dashboard address like https://monitor.example.com", raw)
	}
	if u.Scheme == "http" && !isLocal(u.Hostname()) && os.Getenv("NEXGUARD_ALLOW_HTTP") != "1" {
		return "", errors.New("use the dashboard's https:// address: over plain http the token " +
			"would cross the internet unencrypted")
	}
	return strings.TrimRight(u.Scheme+"://"+u.Host+u.Path, "/"), nil
}

func isLocal(host string) bool {
	if host == "localhost" || strings.HasSuffix(host, ".local") || strings.HasSuffix(host, ".localhost") {
		return true
	}
	ip := net.ParseIP(host)
	return ip != nil && (ip.IsLoopback() || ip.IsPrivate() || ip.IsLinkLocalUnicast())
}
