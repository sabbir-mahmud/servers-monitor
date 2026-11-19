package svc

import (
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"time"
)

func launchAgentPath() string {
	home, _ := os.UserHomeDir()
	return filepath.Join(home, "Library", "LaunchAgents", userLabel+".plist")
}

func registerUser(exe, dir string) error {
	plist := launchAgentPath()
	if err := os.MkdirAll(filepath.Dir(plist), 0o755); err != nil {
		return err
	}
	if err := os.WriteFile(plist, []byte(launchAgentPlist(exe, dir)), 0o644); err != nil {
		return fmt.Errorf("couldn't write %s: %w", plist, err)
	}
	domain := "gui/" + strconv.Itoa(os.Getuid())
	job := domain + "/" + userLabel
	_ = exec.Command("launchctl", "bootout", job).Run() // the old copy, if any
	var lastErr string
	for range 5 { // bootstrap right after bootout can fail while the old job winds down
		out, err := exec.Command("launchctl", "bootstrap", domain, plist).CombinedOutput()
		if err == nil || exec.Command("launchctl", "print", job).Run() == nil {
			return nil
		}
		lastErr = strings.TrimSpace(string(out))
		time.Sleep(time.Second)
	}
	if exec.Command("launchctl", "load", "-w", plist).Run() == nil {
		return nil
	}
	return fmt.Errorf("macOS didn't start the agent (%s). It starts the next time you sign in; "+
		"if not, allow \"nexguard\" in System Settings > General > Login Items", lastErr)
}
