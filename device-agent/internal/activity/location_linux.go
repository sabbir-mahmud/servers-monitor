package activity

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/godbus/dbus/v5"

	"github.com/sabbir-mahmud/servers-monitor/device-agent/internal/config"
)

// GeoClue (the location service most Linux desktops ship) answers a system service only
// when its configuration names it. As root the agent adds itself, once.
const geoclueApp = "nexguard"

const (
	gcName     = "org.freedesktop.GeoClue2"
	gcClient   = gcName + ".Client"
	gcLocation = gcName + ".Location"
	gcExact    = uint32(8) // GCLUE_ACCURACY_LEVEL_EXACT: Wi-Fi where available
)

func locate() (*Fix, error) {
	if !config.UserMode() {
		if err := allowInGeoclue(); err != nil {
			return nil, err
		}
	}
	conn, err := dbus.ConnectSystemBus()
	if err != nil {
		return nil, ErrNoLocationService
	}
	defer conn.Close()
	var client dbus.ObjectPath
	mgr := conn.Object(gcName, "/org/freedesktop/GeoClue2/Manager")
	if err := mgr.Call(gcName+".Manager.GetClient", 0).Store(&client); err != nil {
		return nil, ErrNoLocationService // GeoClue isn't installed
	}
	obj := conn.Object(gcName, client)
	if err := obj.SetProperty(gcClient+".DesktopId", dbus.MakeVariant(geoclueApp)); err != nil {
		return nil, fmt.Errorf("GeoClue: %w", err)
	}
	_ = obj.SetProperty(gcClient+".RequestedAccuracyLevel", dbus.MakeVariant(gcExact))

	if err := conn.AddMatchSignal(dbus.WithMatchObjectPath(client),
		dbus.WithMatchInterface(gcClient), dbus.WithMatchMember("LocationUpdated")); err != nil {
		return nil, err
	}
	signals := make(chan *dbus.Signal, 4)
	conn.Signal(signals)
	if call := obj.Call(gcClient+".Start", 0); call.Err != nil {
		return nil, fmt.Errorf("GeoClue won't answer the agent: %w", call.Err)
	}
	defer obj.Call(gcClient+".Stop", 0)

	timeout := time.After(30 * time.Second)
	for {
		select {
		case s := <-signals:
			if s == nil || len(s.Body) < 2 {
				continue
			}
			if path, ok := s.Body[1].(dbus.ObjectPath); ok {
				return readFix(conn, path)
			}
		case <-timeout:
			return nil, errors.New("GeoClue found no position in 30 seconds")
		}
	}
}

func readFix(conn *dbus.Conn, path dbus.ObjectPath) (*Fix, error) {
	loc := conn.Object(gcName, path)
	get := func(name string) float64 {
		v, err := loc.GetProperty(gcLocation + "." + name)
		if err != nil {
			return 0
		}
		f, _ := v.Value().(float64)
		return f
	}
	return &Fix{Lat: get("Latitude"), Lon: get("Longitude"), Accuracy: get("Accuracy")}, nil
}

// allowInGeoclue adds the agent to GeoClue's allowed system services. GeoClue 2.6+ reads
// conf.d; older versions only geoclue.conf, where the section is appended once.
func allowInGeoclue() error {
	section := "[" + geoclueApp + "]\nallowed=true\nsystem=true\nusers=\n"
	dir := "/etc/geoclue/conf.d"
	if _, err := os.Stat("/etc/geoclue"); err != nil {
		return ErrNoLocationService
	}
	if info, err := os.Stat(dir); err == nil && info.IsDir() {
		p := filepath.Join(dir, "90-nexguard.conf")
		if old, err := os.ReadFile(p); err == nil && string(old) == section {
			return nil
		}
		return os.WriteFile(p, []byte("# Lets the NexGuard agent ask where this computer is.\n"+section), 0o644)
	}
	main := "/etc/geoclue/geoclue.conf"
	old, err := os.ReadFile(main)
	if err != nil {
		return ErrNoLocationService
	}
	if strings.Contains(string(old), "["+geoclueApp+"]") {
		return nil
	}
	f, err := os.OpenFile(main, os.O_APPEND|os.O_WRONLY, 0)
	if err != nil {
		return err
	}
	defer f.Close()
	_, err = f.WriteString("\n# Lets the NexGuard agent ask where this computer is.\n" + section)
	return err
}
