package agent

import (
	"encoding/hex"
	"encoding/json"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/sabbir-mahmud/servers-monitor/device-agent/internal/activity"
	"github.com/sabbir-mahmud/servers-monitor/device-agent/internal/api"
	"github.com/sabbir-mahmud/servers-monitor/device-agent/internal/config"
	"github.com/sabbir-mahmud/servers-monitor/device-agent/internal/logs"
	"github.com/sabbir-mahmud/servers-monitor/device-agent/internal/relay/relaytest"
)

var testKey = []byte(strings.Repeat("k", 32))

// relayWith: a fake relay holding this computer's login, with the dashboard's settings
// (controls as JSON) and a blocklist at version v1 already left there, as the bridge does.
func relayWith(t *testing.T, controls string) *relaytest.Server {
	t.Helper()
	srv := relaytest.New(t)
	srv.AddUser("sm-d-1", "devpw", "sm:d:1:")
	setControls(srv, controls)
	srv.Set("sm:d:1:bl", relaytest.Seal(testKey, relaytest.BlocklistBody("v1", "0.0.0.0 bad.example")))
	return srv
}

func setControls(srv *relaytest.Server, controls string) {
	body := `{"checkin_seconds":300,"controls":` + controls + `,"device":{"name":"Mac","account":"A"}}`
	srv.Set("sm:d:1:cfg", relaytest.Seal(testKey, body))
}

// reports: the check-ins the dashboard received, verified.
func reports(t *testing.T, srv *relaytest.Server) []map[string]any {
	t.Helper()
	var out []map[string]any
	for _, raw := range srv.Lists["sm:d:1:in"] {
		body, ok := relaytest.Verify(testKey, raw)
		if !ok {
			t.Fatalf("unsigned check-in %q", raw)
		}
		var m struct {
			Report map[string]any `json:"report"`
		}
		_ = json.Unmarshal([]byte(body), &m)
		out = append(out, m.Report)
	}
	return out
}

func paired(t *testing.T, srv *relaytest.Server) *config.Config {
	t.Helper()
	t.Setenv("NEXGUARD_CONFIG_DIR", t.TempDir())
	hosts := filepath.Join(t.TempDir(), "hosts")
	_ = os.WriteFile(hosts, []byte("127.0.0.1 localhost\n"), 0o644)
	t.Setenv("NEXGUARD_HOSTS", hosts)
	_ = activity.Reset()
	login := api.Login{URL: srv.URL("sm-d-1"), Inbox: "sm:d:1:in", Config: "sm:d:1:cfg", Blocklist: "sm:d:1:bl"}
	cfg := &config.Config{Relay: login, Key: hex.EncodeToString(testKey), Interval: 300}
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
	srv := relayWith(t, `{"screen_time":true,"web_activity":false,"blocking":"v1"}`)
	cfg := paired(t, srv)
	if err := CheckinOnce(cfg, "test"); err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(hostsFile(t), "0.0.0.0 bad.example") || !activity.CurrentControls().ScreenTime {
		t.Fatalf("hosts:\n%s", hostsFile(t))
	}
	_ = CheckinOnce(cfg, "test")
	if n := srv.Gets("sm:d:1:bl"); n != 1 {
		t.Errorf("an unchanged version must not be read again (%d reads)", n)
	}
	// It tells the dashboard what it has, so nothing is rewritten needlessly.
	last := reports(t, srv)[1]
	if last["blocking_applied"] != "v1" || last["config"] != cfg.Settings || len(cfg.Settings) != 16 {
		t.Fatalf("report: %v (settings %q)", last, cfg.Settings)
	}
	// Someone deletes the section: the next check-in puts it back from the saved copy.
	_ = os.WriteFile(os.Getenv("NEXGUARD_HOSTS"), []byte("127.0.0.1 localhost\n"), 0o644)
	_ = CheckinOnce(cfg, "test")
	if !strings.Contains(hostsFile(t), "bad.example") || srv.Gets("sm:d:1:bl") != 1 {
		t.Fatalf("not restored (%d reads):\n%s", srv.Gets("sm:d:1:bl"), hostsFile(t))
	}
}

func TestANewBlocklistWaitsUntilItIsInTheRelay(t *testing.T) {
	srv := relayWith(t, `{"blocking":"v2"}`) // settings say v2, the relay still holds v1
	cfg := paired(t, srv)
	if err := CheckinOnce(cfg, "test"); err != nil {
		t.Fatal(err)
	}
	if strings.Contains(hostsFile(t), "bad.example") || activity.Applied() != "" {
		t.Fatalf("applied a list of the wrong version:\n%s", hostsFile(t))
	}
	srv.Set("sm:d:1:bl", relaytest.Seal(testKey, relaytest.BlocklistBody("v2", "0.0.0.0 worse.example")))
	_ = CheckinOnce(cfg, "test")
	if !strings.Contains(hostsFile(t), "worse.example") || activity.Applied() != "v2" {
		t.Fatalf("hosts:\n%s", hostsFile(t))
	}
}

func TestNoControlsRemovesBlocking(t *testing.T) {
	srv := relayWith(t, `{"screen_time":false,"web_activity":false,"blocking":"v1"}`)
	cfg := paired(t, srv)
	_ = CheckinOnce(cfg, "test")
	setControls(srv, `null`) // e.g. the device was reassigned to an adult
	_ = CheckinOnce(cfg, "test")
	if got := hostsFile(t); got != "127.0.0.1 localhost\n" {
		t.Fatalf("hosts left:\n%q", got)
	}
}

func TestARemovedComputerForgetsEverything(t *testing.T) {
	srv := relayWith(t, `{"screen_time":true,"web_activity":true,"blocking":"v1"}`)
	cfg := paired(t, srv)
	_ = CheckinOnce(cfg, "test")
	srv.RemoveUser("sm-d-1") // removed on the dashboard: its login is deleted
	if err := CheckinOnce(cfg, "test"); err != api.ErrUnpaired {
		t.Fatalf("got %v", err)
	}
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
	srv := relayWith(t, `{"screen_time":true,"web_activity":false,"blocking":""}`)
	cfg := paired(t, srv)
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
	if strings.Contains(got, "devpw") || strings.Contains(got, cfg.Key) {
		t.Fatal("the relay password and signing key must never be logged")
	}
}

// An edited hosts file is reported once with the next check-in, then forgotten.
func TestEditedBlocklistIsReportedOnce(t *testing.T) {
	srv := relayWith(t, `{"blocking":"v1"}`)
	cfg := paired(t, srv)
	tamper = nil
	_ = CheckinOnce(cfg, "test") // first apply: not tampering
	_ = os.WriteFile(os.Getenv("NEXGUARD_HOSTS"), []byte("127.0.0.1 localhost\n"), 0o644)
	_ = CheckinOnce(cfg, "test") // notices and puts it back
	_ = CheckinOnce(cfg, "test") // reports it
	_ = CheckinOnce(cfg, "test") // and only once
	r := reports(t, srv)
	got := []any{r[0]["tamper"], r[1]["tamper"], r[2]["tamper"], r[3]["tamper"]}
	if got[0] != nil || got[1] != nil || got[3] != nil {
		t.Fatalf("tamper sent at the wrong time: %v", got)
	}
	if list, _ := got[2].([]any); len(list) != 1 || list[0] != "hosts_edited" {
		t.Fatalf("tamper not reported: %v", got)
	}
}

func TestGoodbyeTellsTheDashboard(t *testing.T) {
	srv := relayWith(t, `null`)
	paired(t, srv)
	Goodbye("test")
	if r := reports(t, srv); len(r) != 1 || r[0]["bye"] != "uninstall" {
		t.Fatalf("reports = %v", r)
	}
}
