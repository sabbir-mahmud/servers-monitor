// Package agent is the check-in loop the service runs, and the status it leaves behind
// for `nexguard status`.
package agent

import (
	"encoding/json"
	"errors"
	"log"
	"os"
	"path/filepath"
	"time"

	"github.com/sabbir-mahmud/servers-monitor/device-agent/internal/api"
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

// CheckinOnce sends one report. On success it saves a new interval if the dashboard asks.
// When the dashboard says the device is gone, it forgets the pairing.
func CheckinOnce(cfg *config.Config, version string) error {
	client := api.New(cfg.Server, "nexguard-agent/"+version)
	interval, err := client.Checkin(cfg.Token, collect.Report(version))
	if errors.Is(err, api.ErrUnpaired) {
		_ = config.Remove()
		writeStatus(Status{At: time.Now(), Error: "removed on the dashboard; pair again to resume"})
		return err
	}
	if err != nil {
		writeStatus(Status{At: time.Now(), Error: err.Error(), Interval: cfg.Interval})
		return err
	}
	if interval >= 60 && interval != cfg.Interval {
		cfg.Interval = interval
		if err := config.Save(cfg); err != nil {
			log.Printf("couldn't save the new interval: %v", err)
		}
	}
	writeStatus(Status{At: time.Now(), OK: true, Interval: cfg.Interval})
	return nil
}

// Run checks in until stop is closed: at the dashboard's interval, backing off on errors,
// and waiting quietly while not paired.
func Run(version string, stop <-chan struct{}) {
	backoff := time.Duration(0)
	for {
		wait := notPairedWait
		if cfg, err := config.Load(); err != nil {
			if !errors.Is(err, config.ErrNotPaired) {
				log.Printf("config: %v", err)
			}
		} else if err := CheckinOnce(cfg, version); err != nil {
			log.Printf("check-in failed: %v", err)
			if !errors.Is(err, api.ErrUnpaired) {
				backoff = min(max(2*backoff, time.Duration(cfg.Interval)*time.Second), maxBackoff)
				wait = backoff
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
