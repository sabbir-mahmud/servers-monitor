package api

import (
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"testing"
)

func fakeDashboard(t *testing.T, seen *[]map[string]any) *httptest.Server {
	t.Helper()
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var body map[string]any
		_ = json.NewDecoder(r.Body).Decode(&body)
		*seen = append(*seen, body)
		w.Header().Set("Content-Type", "application/json")
		switch {
		case r.URL.Path == "/device/v1/pair" && body["code"] != "K7PQ3MXA":
			w.WriteHeader(404)
			_, _ = w.Write([]byte(`{"error":"That code isn't valid."}`))
		case r.URL.Path == "/device/v1/pair" && body["preview"] == true:
			_, _ = w.Write([]byte(`{"disclosure":"The Rahmans will see...","device":{"name":"Sara's Mac","account":"The Rahmans"}}`))
		case r.URL.Path == "/device/v1/pair":
			w.WriteHeader(201)
			_, _ = w.Write([]byte(`{"token":"ngd_abc","checkin_seconds":300,"device":{"name":"Sara's Mac","account":"The Rahmans"}}`))
		case r.Header.Get("Authorization") != "Bearer ngd_abc":
			w.WriteHeader(401)
			_, _ = w.Write([]byte(`{"error":"Not paired."}`))
		default:
			_, _ = w.Write([]byte(`{"checkin_seconds":600}`))
		}
	}))
}

func TestPreviewThenPairThenCheckin(t *testing.T) {
	var seen []map[string]any
	srv := fakeDashboard(t, &seen)
	defer srv.Close()
	c := New(srv.URL, "test")
	info := map[string]any{"platform": "macos"}

	p, err := c.Pair("K7PQ3MXA", info, true)
	if err != nil || p.Disclosure == "" || p.Token != "" {
		t.Fatalf("preview: %+v %v", p, err)
	}
	r, err := c.Pair("K7PQ3MXA", info, false)
	if err != nil || r.Token != "ngd_abc" || r.Device.Account != "The Rahmans" {
		t.Fatalf("pair: %+v %v", r, err)
	}
	if seen[0]["preview"] != true || seen[1]["preview"] != nil || seen[1]["platform"] != "macos" {
		t.Fatalf("bodies: %v", seen)
	}
	if every, err := c.Checkin("ngd_abc", map[string]any{"cpu": map[string]any{"percent": 5}}); err != nil || every != 600 {
		t.Fatalf("checkin: %d %v", every, err)
	}
}

func TestRefusalsKeepTheDashboardsWords(t *testing.T) {
	var seen []map[string]any
	srv := fakeDashboard(t, &seen)
	defer srv.Close()
	c := New(srv.URL, "test")
	_, err := c.Pair("WRONG000", nil, true)
	var apiErr *Error
	if !errors.As(err, &apiErr) || apiErr.Status != 404 || apiErr.Message != "That code isn't valid." {
		t.Fatalf("got %v", err)
	}
	if _, err := c.Checkin("ngd_old", nil); !errors.Is(err, ErrUnpaired) {
		t.Fatalf("a removed device must get ErrUnpaired, got %v", err)
	}
}

func TestPlainHTTPOnlyOnThisMachineOrTheLAN(t *testing.T) {
	ok := map[string]string{
		"monitor.example.com":       "https://monitor.example.com",
		"https://m.example.com/x/":  "https://m.example.com/x",
		"http://127.0.0.1:8000":     "http://127.0.0.1:8000",
		"http://192.168.1.20:8000/": "http://192.168.1.20:8000",
		"http://nas.local":          "http://nas.local",
	}
	for in, want := range ok {
		if got, err := CheckServer(in); err != nil || got != want {
			t.Errorf("CheckServer(%q) = %q, %v; want %q", in, got, err, want)
		}
	}
	for _, bad := range []string{"http://monitor.example.com", "ftp://x", "https://"} {
		if _, err := CheckServer(bad); err == nil {
			t.Errorf("CheckServer(%q) should refuse", bad)
		}
	}
	t.Setenv("NEXGUARD_ALLOW_HTTP", "1")
	if _, err := CheckServer("http://monitor.example.com"); err != nil {
		t.Error(err)
	}
}
