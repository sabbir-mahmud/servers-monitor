package agent

import (
	"encoding/json"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"

	"github.com/sabbir-mahmud/servers-monitor/device-agent/internal/activity"
	"github.com/sabbir-mahmud/servers-monitor/device-agent/internal/config"
	"github.com/sabbir-mahmud/servers-monitor/device-agent/internal/logs"
)

// A dashboard that wants blocking at version v1, and counts blocklist downloads.
func dashboard(t *testing.T, controls string, fetches *atomic.Int32) *httptest.Server {
	t.Helper()
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/device/v1/checkin":
			var body map[string]any
			_ = json.NewDecoder(r.Body).Decode(&body)
			_, _ = w.Write([]byte(`{"checkin_seconds":300,"controls":` + controls + `}`))
		case "/device/v1/blocklist":
			fetches.Add(1)
			if r.Header.Get("If-None-Match") == `"v1"` {
				w.WriteHeader(304)
				return
			}
			_, _ = w.Write([]byte("0.0.0.0 bad.example\n"))
		}
	}))
}

func paired(t *testing.T, server string) *config.Config {
	t.Helper()
	t.Setenv("NEXGUARD_CONFIG_DIR", t.TempDir())
	hosts := filepath.Join(t.TempDir(), "hosts")
	_ = os.WriteFile(hosts, []byte("127.0.0.1 localhost\n"), 0o644)
	t.Setenv("NEXGUARD_HOSTS", hosts)
	_ = activity.Reset()
	cfg := &config.Config{Server: server, Token: "ngd_x", Interval: 300}
	if err := config.Save(cfg); err != nil {
		t.Fatal(err)
	}
	return cfg
}

func hostsFile(t *testing.T) string {
	data, _ := os.ReadFile(os.Getenv("NEXGUARD_HOSTS"))
	return string(data)
}

func TestBlockingFollowsTheDashboard(t *testing.T) {
	var fetches atomic.Int32
	srv := dashboard(t, `{"screen_time":true,"web_activity":false,"blocking":"v1"}`, &fetches)
	defer srv.Close()
	cfg := paired(t, srv.URL)
	if err := CheckinOnce(cfg, "test"); err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(hostsFile(t), "0.0.0.0 bad.example") || !activity.CurrentControls().ScreenTime {
		t.Fatalf("hosts:\n%s", hostsFile(t))
	}
	_ = CheckinOnce(cfg, "test")
	if fetches.Load() != 1 {
		t.Errorf("an unchanged version must not be downloaded again (%d fetches)", fetches.Load())
	}
	// Someone deletes the section: the next check-in puts it back from the saved copy.
	_ = os.WriteFile(os.Getenv("NEXGUARD_HOSTS"), []byte("127.0.0.1 localhost\n"), 0o644)
	_ = CheckinOnce(cfg, "test")
	if !strings.Contains(hostsFile(t), "bad.example") || fetches.Load() != 1 {
		t.Fatalf("not restored (%d fetches):\n%s", fetches.Load(), hostsFile(t))
	}
}

func TestNoControlsRemovesBlocking(t *testing.T) {
	var fetches atomic.Int32
	on := dashboard(t, `{"screen_time":false,"web_activity":false,"blocking":"v1"}`, &fetches)
	defer on.Close()
	cfg := paired(t, on.URL)
	_ = CheckinOnce(cfg, "test")
	off := dashboard(t, `null`, &fetches) // e.g. the device was reassigned to an adult
	defer off.Close()
	cfg.Server = off.URL
	_ = CheckinOnce(cfg, "test")
	if got := hostsFile(t); got != "127.0.0.1 localhost\n" {
		t.Fatalf("hosts left:\n%q", got)
	}
}

func TestForgetUndoesEverything(t *testing.T) {
	var fetches atomic.Int32
	srv := dashboard(t, `{"screen_time":true,"web_activity":true,"blocking":"v1"}`, &fetches)
	defer srv.Close()
	cfg := paired(t, srv.URL)
	_ = CheckinOnce(cfg, "test")
	Forget()
	if strings.Contains(hostsFile(t), "bad.example") {
		t.Error("blocking must go with the pairing")
	}
	if _, err := config.Load(); err != config.ErrNotPaired {
		t.Errorf("config: %v", err)
	}
	if activity.CurrentControls().ScreenTime {
		t.Error("activity state must be reset")
	}
}

func TestCheckinIsLoggedForThePerson(t *testing.T) {
	var fetches atomic.Int32
	srv := dashboard(t, `{"screen_time":true,"web_activity":false,"blocking":""}`, &fetches)
	defer srv.Close()
	cfg := paired(t, srv.URL)
	logs.Setup(nil)
	defer slog.SetDefault(slog.New(slog.NewTextHandler(os.Stderr, nil)))
	if err := CheckinOnce(cfg, "test"); err != nil {
		t.Fatal(err)
	}
	data, err := os.ReadFile(logs.Path())
	if err != nil {
		t.Fatal(err)
	}
	got := string(data)
	for _, want := range []string{`"msg":"the dashboard switched screen time on"`, `"msg":"checked in"`} {
		if !strings.Contains(got, want) {
			t.Errorf("log is missing %s:\n%s", want, got)
		}
	}
	if strings.Contains(got, cfg.Token) {
		t.Fatal("the pairing token must never be logged")
	}
}

// An edited hosts file is reported once with the next check-in, then forgotten.
func TestEditedBlocklistIsReportedOnce(t *testing.T) {
	var reports []map[string]any
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/device/v1/checkin":
			var body map[string]any
			_ = json.NewDecoder(r.Body).Decode(&body)
			reports = append(reports, body)
			_, _ = w.Write([]byte(`{"checkin_seconds":300,"controls":{"blocking":"v1"}}`))
		case "/device/v1/blocklist":
			_, _ = w.Write([]byte("0.0.0.0 bad.example\n"))
		}
	}))
	defer srv.Close()
	cfg := paired(t, srv.URL)
	tamper = nil
	_ = CheckinOnce(cfg, "test") // first apply: not tampering
	_ = os.WriteFile(os.Getenv("NEXGUARD_HOSTS"), []byte("127.0.0.1 localhost\n"), 0o644)
	_ = CheckinOnce(cfg, "test") // notices and puts it back
	_ = CheckinOnce(cfg, "test") // reports it
	_ = CheckinOnce(cfg, "test") // and only once
	got := []any{reports[0]["tamper"], reports[1]["tamper"], reports[2]["tamper"], reports[3]["tamper"]}
	if got[0] != nil || got[1] != nil || got[3] != nil {
		t.Fatalf("tamper sent at the wrong time: %v", got)
	}
	if list, _ := got[2].([]any); len(list) != 1 || list[0] != "hosts_edited" {
		t.Fatalf("tamper not reported: %v", got)
	}
}

func TestGoodbyeTellsTheDashboard(t *testing.T) {
	var bye any
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var body map[string]any
		_ = json.NewDecoder(r.Body).Decode(&body)
		bye = body["bye"]
		_, _ = w.Write([]byte(`{"checkin_seconds":300}`))
	}))
	defer srv.Close()
	paired(t, srv.URL)
	Goodbye("test")
	if bye != "uninstall" {
		t.Fatalf("bye = %v", bye)
	}
}
