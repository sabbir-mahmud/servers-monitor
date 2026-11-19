package svc

import (
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
)

func xdgConfig() string {
	if d := os.Getenv("XDG_CONFIG_HOME"); d != "" {
		return d
	}
	home, _ := os.UserHomeDir()
	return filepath.Join(home, ".config")
}

func registerUser(exe, dir string) error {
	if exec.Command("systemctl", "--user", "show-environment").Run() == nil {
		units := filepath.Join(xdgConfig(), "systemd", "user")
		if err := os.MkdirAll(units, 0o755); err != nil {
			return err
		}
		unit := filepath.Join(units, userUnit+".service")
		if err := os.WriteFile(unit, []byte(userUnitFile(exe, dir)), 0o644); err != nil {
			return fmt.Errorf("couldn't write %s: %w", unit, err)
		}
		_ = exec.Command("systemctl", "--user", "daemon-reload").Run()
		_ = exec.Command("systemctl", "--user", "enable", userUnit+".service").Run()
		if out, err := exec.Command("systemctl", "--user", "restart", userUnit+".service").CombinedOutput(); err != nil {
			return fmt.Errorf("the agent didn't start (%s). Check it with: systemctl --user status %s",
				strings.TrimSpace(string(out)), userUnit)
		}
		return nil
	}
	// No systemd user session: start with the desktop session, and start now.
	auto := filepath.Join(xdgConfig(), "autostart")
	if err := os.MkdirAll(auto, 0o755); err != nil {
		return err
	}
	if err := os.WriteFile(filepath.Join(auto, userUnit+".desktop"), []byte(autostartFile(exe, dir)), 0o644); err != nil {
		return err
	}
	pidFile := filepath.Join(dir, "agent.pid")
	if data, err := os.ReadFile(pidFile); err == nil {
		if pid, err := strconv.Atoi(strings.TrimSpace(string(data))); err == nil && pid != os.Getpid() {
			_ = syscall.Kill(pid, syscall.SIGTERM)
		}
	}
	cmd := exec.Command(exe, "--user", "--dir", dir, "run")
	cmd.SysProcAttr = &syscall.SysProcAttr{Setsid: true}
	if err := cmd.Start(); err != nil {
		return fmt.Errorf("couldn't start the agent: %w", err)
	}
	_ = os.WriteFile(pidFile, []byte(strconv.Itoa(cmd.Process.Pid)), 0o600)
	return cmd.Process.Release()
}
