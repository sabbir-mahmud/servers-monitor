// Package config stores the device's pairing (dashboard address and token) where only an
// administrator can read or change it: the agent runs as a system service, and the person
// being monitored uses a standard account.
package config

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
)

// ErrNotPaired means there is no saved pairing: run `nexguard pair` (as an administrator).
var ErrNotPaired = errors.New("this computer isn't paired yet")

type Config struct {
	Server   string `json:"server"`
	Token    string `json:"token"`
	Interval int    `json:"interval"` // seconds between check-ins, as the dashboard asks
	Name     string `json:"device_name,omitempty"`
	Account  string `json:"account,omitempty"`
}

// Dir is where the agent keeps its files. NEXGUARD_CONFIG_DIR overrides it (tests).
func Dir() string {
	if d := os.Getenv("NEXGUARD_CONFIG_DIR"); d != "" {
		return d
	}
	switch runtime.GOOS {
	case "windows":
		base := os.Getenv("ProgramData")
		if base == "" {
			base = `C:\ProgramData`
		}
		return filepath.Join(base, "NexGuard")
	case "darwin":
		return "/Library/Application Support/NexGuard"
	default:
		return "/etc/nexguard"
	}
}

func Path() string { return filepath.Join(Dir(), "config.json") }

func Load() (*Config, error) {
	data, err := os.ReadFile(Path())
	if errors.Is(err, os.ErrNotExist) {
		return nil, ErrNotPaired
	}
	if errors.Is(err, os.ErrPermission) {
		return nil, fmt.Errorf("can't read %s: run this as an administrator", Path())
	}
	if err != nil {
		return nil, err
	}
	var c Config
	if err := json.Unmarshal(data, &c); err != nil {
		return nil, fmt.Errorf("%s is damaged (%v): pair again", Path(), err)
	}
	if c.Server == "" || c.Token == "" {
		return nil, ErrNotPaired
	}
	if c.Interval < 60 {
		c.Interval = 300
	}
	return &c, nil
}

// Save writes the config atomically, readable by administrators only.
func Save(c *Config) error {
	if err := os.MkdirAll(Dir(), 0o755); err != nil {
		return fmt.Errorf("can't create %s: %w (run as an administrator)", Dir(), err)
	}
	data, err := json.MarshalIndent(c, "", "  ")
	if err != nil {
		return err
	}
	tmp := Path() + ".tmp"
	if err := os.WriteFile(tmp, data, 0o600); err != nil {
		return fmt.Errorf("can't write %s: %w (run as an administrator)", tmp, err)
	}
	if err := os.Chmod(tmp, 0o600); err != nil { // WriteFile keeps an existing file's mode
		return err
	}
	if err := os.Rename(tmp, Path()); err != nil {
		return err
	}
	return restrict(Path())
}

func Remove() error {
	err := os.Remove(Path())
	if errors.Is(err, os.ErrNotExist) {
		return nil
	}
	return err
}

// restrict: Unix mode 0600 already means root only. On Windows, files under ProgramData
// inherit "Users can read", so replace the inherited rules with SYSTEM + Administrators.
func restrict(path string) error {
	if runtime.GOOS != "windows" || os.Getenv("NEXGUARD_CONFIG_DIR") != "" {
		return nil
	}
	out, err := exec.Command("icacls", path, "/inheritance:r",
		"/grant:r", "*S-1-5-18:F", "/grant:r", "*S-1-5-32-544:F").CombinedOutput()
	if err != nil {
		return fmt.Errorf("can't restrict %s: %v: %s", path, err, out)
	}
	return nil
}
