// Package svc installs and runs the agent as a system service: a Windows service, a macOS
// LaunchDaemon or a systemd unit. It starts at boot, restarts if it stops, and only an
// administrator can stop or remove it.
package svc

import (
	"errors"
	"fmt"
	"io"
	"log"
	"os"
	"path/filepath"
	"runtime"

	"github.com/kardianos/service"

	"github.com/sabbir-mahmud/servers-monitor/device-agent/internal/config"
)

const name = "nexguard"

// InstalledPath is where `install` puts the binary: owned by the administrator, so the
// person being monitored can't replace it.
func InstalledPath() string {
	switch runtime.GOOS {
	case "windows":
		base := os.Getenv("ProgramFiles")
		if base == "" {
			base = `C:\Program Files`
		}
		return filepath.Join(base, "NexGuard", "nexguard.exe")
	case "darwin":
		return filepath.Join(config.Dir(), "nexguard")
	default:
		return "/usr/local/bin/nexguard"
	}
}

func definition() *service.Config {
	return &service.Config{
		Name:        name,
		DisplayName: "NexGuard device agent",
		Description: "Reports this computer's health to the NexGuard dashboard it was paired with.",
		Executable:  InstalledPath(),
		Arguments:   []string{"run"},
		Option: service.KeyValue{
			"KeepAlive":              true,      // launchd: restart if it exits
			"RunAtLoad":              true,      // launchd: start at boot
			"OnFailure":              "restart", // Windows
			"OnFailureDelayDuration": "30s",
			"Restart":                "always", // systemd
			"StartType":              "automatic",
		},
	}
}

type program struct {
	version string
	loop    func(version string, stop <-chan struct{})
	stop    chan struct{}
	done    chan struct{}
}

func (p *program) Start(service.Service) error {
	p.stop, p.done = make(chan struct{}), make(chan struct{})
	go func() { defer close(p.done); p.loop(p.version, p.stop) }()
	return nil
}

func (p *program) Stop(service.Service) error {
	close(p.stop)
	<-p.done
	return nil
}

func newService(p service.Interface) (service.Service, error) {
	if p == nil {
		p = &program{}
	}
	return service.New(p, definition())
}

// Interactive: started from a terminal rather than by the service manager.
func Interactive() bool { return service.Interactive() }

// Run the check-in loop, under the service manager or in the foreground (Ctrl-C stops it).
func Run(version string, loop func(string, <-chan struct{})) error {
	s, err := newService(&program{version: version, loop: loop})
	if err != nil {
		return err
	}
	if !service.Interactive() {
		if l, err := s.Logger(nil); err == nil {
			log.SetOutput(loggerWriter{l})
			log.SetFlags(0)
		}
	}
	return s.Run()
}

// State: "running", "stopped" or "not installed".
func State() string {
	s, err := newService(nil)
	if err != nil {
		return "unknown"
	}
	switch st, err := s.Status(); {
	case errors.Is(err, service.ErrNotInstalled):
		return "not installed"
	case err != nil:
		return "unknown (" + err.Error() + ")"
	case st == service.StatusRunning:
		return "running"
	default:
		return "stopped"
	}
}

// Install copies this binary into place and starts the service. Run again with a newer
// binary, it upgrades in place.
func Install(version string) error {
	if !isAdmin() {
		return errors.New("installing needs administrator rights: run it with sudo, or from an administrator PowerShell")
	}
	if _, err := config.Load(); errors.Is(err, config.ErrNotPaired) {
		fmt.Println("Note: not paired yet. The service will wait until you run: nexguard pair <dashboard> <code>")
	}
	s, err := newService(nil)
	if err != nil {
		return err
	}
	_, statusErr := s.Status()
	installed := !errors.Is(statusErr, service.ErrNotInstalled)
	if installed {
		_ = s.Stop()
	}
	if err := copySelf(InstalledPath()); err != nil {
		return err
	}
	if !installed {
		if err := s.Install(); err != nil {
			return fmt.Errorf("couldn't register the service: %w", err)
		}
	}
	if err := s.Start(); err != nil {
		return fmt.Errorf("couldn't start the service: %w", err)
	}
	verb := "Installed"
	if installed {
		verb = "Updated"
	}
	fmt.Printf("%s NexGuard %s. It runs in the background and starts with the computer.\n", verb, version)
	return nil
}

// Uninstall stops and removes the service, the pairing and the installed binary.
func Uninstall() error {
	if !isAdmin() {
		return errors.New("removing needs administrator rights: run it with sudo, or from an administrator PowerShell")
	}
	s, err := newService(nil)
	if err != nil {
		return err
	}
	_ = s.Stop()
	if err := s.Uninstall(); err != nil && !errors.Is(err, service.ErrNotInstalled) {
		return fmt.Errorf("couldn't remove the service: %w", err)
	}
	_ = os.Remove(filepath.Join(config.Dir(), "status.json"))
	_ = config.Remove()
	removeBinary(InstalledPath())
	_ = os.Remove(config.Dir()) // only if empty
	fmt.Println("NexGuard is removed from this computer. The dashboard lists it until someone deletes it there.")
	return nil
}

func copySelf(dst string) error {
	src, err := os.Executable()
	if err != nil {
		return err
	}
	if src, err = filepath.EvalSymlinks(src); err != nil {
		return err
	}
	if same, _ := filepath.Abs(dst); same == src {
		return nil
	}
	if err := os.MkdirAll(filepath.Dir(dst), 0o755); err != nil {
		return err
	}
	in, err := os.Open(src)
	if err != nil {
		return err
	}
	defer in.Close()
	tmp := dst + ".new"
	out, err := os.OpenFile(tmp, os.O_CREATE|os.O_TRUNC|os.O_WRONLY, 0o755)
	if err != nil {
		return fmt.Errorf("can't write %s: %w", tmp, err)
	}
	if _, err := io.Copy(out, in); err != nil {
		out.Close()
		return err
	}
	if err := out.Close(); err != nil {
		return err
	}
	return replace(tmp, dst)
}

type loggerWriter struct{ l service.Logger }

func (w loggerWriter) Write(p []byte) (int, error) {
	return len(p), w.l.Info(string(p))
}
