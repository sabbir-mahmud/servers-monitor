// Package agent is the check-in loop the service runs, and the status it leaves behind
// for `nexguard status`.
package agent

import (
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/sabbir-mahmud/servers-monitor/device-agent/internal/activity"
	"github.com/sabbir-mahmud/servers-monitor/device-agent/internal/api"
	"github.com/sabbir-mahmud/servers-monitor/device-agent/internal/block"
	"github.com/sabbir-mahmud/servers-monitor/device-agent/internal/collect"
	"github.com/sabbir-mahmud/servers-monitor/device-agent/internal/config"
)

const (
	notPairedWait = time.Minute      // an administrator may pair it any moment
	maxBackoff    = 30 * time.Minute // dashboard down: retry, but don't hammer it
)

// Status is the outcome of the last check-in, saved next to the config.
type Status struct {
	At       time.Time `json:"at"`
	OK       bool      `json:"ok"`
	Error    string    `json:"error,omitempty"`
	Interval int       `json:"interval,omitempty"`
}

func statusPath() string { return filepath.Join(config.Dir(), "status.json") }

func ReadStatus() (*Status, error) {
	data, err := os.ReadFile(statusPath())
	if err != nil {
		return nil, err
	}
	var s Status
	return &s, json.Unmarshal(data, &s)
}

func writeStatus(s Status) {
	if data, err := json.Marshal(s); err == nil {
		_ = os.WriteFile(statusPath(), data, 0o644) // no secrets: readable for `status`
	}
}

// CheckinOnce sends one report (with any screen time and websites counted since the last
// one). On success it saves a new interval if the dashboard asks and applies its controls.
// When the dashboard says the device is gone, it forgets the pairing and undoes blocking.
func CheckinOnce(cfg *config.Config, version string) error {
	client := api.New(cfg.Server, "nexguard-agent/"+version)
	report := collect.Report(version)
	batch := activity.Take()
	if batch != nil {
		report["activity"] = batch
	}
	if f := activity.LastFix(); f != nil {
		report["location"] = map[string]any{"lat": f.Lat, "lon": f.Lon,
			"accuracy": int(f.Accuracy), "at": f.At.UTC().Format(time.RFC3339), "source": "network"}
	}
	if activity.CurrentControls().WebActivity {
		report["web_no_access"] = activity.NoAccess() // [] clears the dashboard's warning
	}
	res, err := client.Checkin(cfg.Token, report)
	if errors.Is(err, api.ErrUnpaired) {
		Forget()
		slog.Warn("this computer was removed on the dashboard: pairing, activity and blocking cleared")
		writeStatus(Status{At: time.Now(), Error: "removed on the dashboard; pair again to resume"})
		return err
	}
	if err != nil {
		writeStatus(Status{At: time.Now(), Error: err.Error(), Interval: cfg.Interval})
		return err
	}
	if batch != nil {
		activity.Confirm(batch.ID)
	}
	if res.CheckinSeconds >= 60 && res.CheckinSeconds != cfg.Interval {
		cfg.Interval = res.CheckinSeconds
		if err := config.Save(cfg); err != nil {
			slog.Warn("couldn't save the new check-in interval", "error", err.Error())
		}
	}
	if err := applyControls(client, cfg.Token, res.Controls); err != nil {
		slog.Error("couldn't apply the dashboard's settings", "error", err.Error())
	}
	writeStatus(Status{At: time.Now(), OK: true, Interval: cfg.Interval})
	attrs := []any{"dashboard", cfg.Server, "next_in", (time.Duration(cfg.Interval) * time.Second).String()}
	if batch != nil {
		attrs = append(attrs, "activity_batch", batch.ID)
	}
	slog.Info("checked in", attrs...)
	return nil
}

// Forget the pairing and everything that came with it: counted activity and blocking.
func Forget() {
	_ = config.Remove()
	_ = activity.Reset()
	_ = os.Remove(blocklistPath())
	if config.UserMode() {
		return // never had the rights to block
	}
	if err := block.Remove(); err != nil {
		slog.Error("couldn't remove blocking", "file", block.Path(), "error", err.Error())
	}
}

func blocklistPath() string { return filepath.Join(config.Dir(), "blocklist.txt") }

// applyControls: remember what to collect, and make the hosts file match the dashboard's
// blocklist: fetched only when its version changed, restored when someone edited it.
func applyControls(client *api.Client, token string, c *api.Controls) error {
	if c == nil {
		c = &api.Controls{}
	}
	logChanges(activity.CurrentControls(), activity.Controls(*c))
	if err := activity.SetControls(activity.Controls(*c)); err != nil {
		return err
	}
	if config.UserMode() {
		return nil // the dashboard sends no blocking to a user-mode agent; it couldn't apply it
	}
	if c.Blocking == "" {
		_ = os.Remove(blocklistPath())
		_ = activity.SetApplied("")
		return block.Remove()
	}
	lines := readLines(blocklistPath())
	if c.Blocking != activity.Applied() || lines == nil {
		have := activity.Applied()
		if lines == nil {
			have = "" // nothing cached: ask for the full list
		}
		fresh, err := client.Blocklist(token, have)
		if err != nil {
			return err
		}
		if fresh != nil {
			lines = fresh
			if err := writeLines(blocklistPath(), lines); err != nil {
				return err
			}
		}
	}
	if block.Fingerprint() != block.FingerprintOf(lines) {
		if _, err := block.Apply(lines); err != nil {
			return fmt.Errorf("couldn't update %s: %w", block.Path(), err)
		}
		slog.Info("blocklist updated", "sites", block.Count(), "file", block.Path())
	}
	return activity.SetApplied(c.Blocking)
}

// logChanges notes each collection setting the dashboard switched on or off, so the person
// using the computer can see when it started or stopped.
func logChanges(was, now activity.Controls) {
	for _, x := range []struct {
		name     string
		was, now bool
	}{
		{"screen time", was.ScreenTime, now.ScreenTime},
		{"websites visited", was.WebActivity, now.WebActivity},
		{"location", was.Location, now.Location},
		{"website blocking", was.Blocking != "", now.Blocking != "" && !config.UserMode()},
	} {
		if x.was != x.now {
			slog.Info("the dashboard switched "+x.name+" "+onOff(x.now), "setting", x.name, "on", x.now)
		}
	}
}

func onOff(on bool) string {
	if on {
		return "on"
	}
	return "off"
}

func readLines(path string) []string {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil
	}
	return strings.Split(strings.TrimRight(string(data), "\n"), "\n")
}

func writeLines(path string, lines []string) error {
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, []byte(strings.Join(lines, "\n")+"\n"), 0o600); err != nil {
		return err
	}
	return os.Rename(tmp, path)
}

// sampleLoop counts screen time every minute and reads browser histories every 10, while
// the dashboard has them switched on.
// locateLoop asks the computer where it is while Location is on. Each error is logged once,
// not every few minutes; a Mac has no service the agent may use, so it stays quiet.
func locateLoop(stop <-chan struct{}) {
	lastErr := ""
	for {
		err := activity.LocateOnce()
		switch {
		case err == nil:
			lastErr = ""
		case errors.Is(err, activity.ErrNoLocationService):
		case err.Error() != lastErr:
			slog.Warn("location unavailable", "error", err.Error())
			lastErr = err.Error()
		}
		select {
		case <-stop:
			return
		case <-time.After(activity.LocateEvery):
		}
	}
}

// accessLoop: which browsers the system won't let the agent read. Often while some are
// refused, so the installer (waiting for Full Disk Access) sees the change in seconds.
func accessLoop(stop <-chan struct{}) {
	for {
		wait := 5 * time.Minute
		if len(activity.CheckAccess()) > 0 {
			wait = 10 * time.Second
		}
		select {
		case <-stop:
			return
		case <-time.After(wait):
		}
	}
}

func sampleLoop(stop <-chan struct{}) {
	tick := time.NewTicker(activity.SampleEvery)
	defer tick.Stop()
	// Read once now: the first read only notes where each history ends, so sites start
	// counting from here rather than one interval later.
	if err := activity.ReadWebOnce(); err != nil {
		slog.Warn("couldn't read browser history", "error", err.Error())
	}
	lastWeb := time.Now()
	for {
		select {
		case <-stop:
			return
		case now := <-tick.C:
			activity.SampleOnce(now)
			if now.Sub(lastWeb) >= activity.WebEvery {
				lastWeb = now
				if err := activity.ReadWebOnce(); err != nil {
					slog.Warn("couldn't read browser history", "error", err.Error())
				}
			}
		}
	}
}

// Run checks in until stop is closed: at the dashboard's interval, backing off on errors,
// and waiting quietly while not paired.
func Run(version string, stop <-chan struct{}) {
	slog.Info("agent started", "version", version, "mode", config.Mode())
	defer slog.Info("agent stopped")
	go sampleLoop(stop)
	go accessLoop(stop)
	go locateLoop(stop)
	backoff := time.Duration(0)
	for {
		wait := notPairedWait
		if cfg, err := config.Load(); err != nil {
			if !errors.Is(err, config.ErrNotPaired) {
				slog.Error("couldn't read the pairing", "error", err.Error())
			}
		} else if err := CheckinOnce(cfg, version); err != nil {
			if !errors.Is(err, api.ErrUnpaired) {
				backoff = min(max(2*backoff, time.Duration(cfg.Interval)*time.Second), maxBackoff)
				wait = backoff
				slog.Warn("check-in failed", "error", err.Error(), "retry_in", wait.String())
			}
		} else {
			backoff = 0
			wait = time.Duration(cfg.Interval) * time.Second
		}
		select {
		case <-stop:
			return
		case <-time.After(wait):
		}
	}
}
