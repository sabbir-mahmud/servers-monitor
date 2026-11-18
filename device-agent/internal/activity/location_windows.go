package activity

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"strings"
	"syscall"
	"time"
	"unicode/utf16"

	"golang.org/x/sys/windows"

	"github.com/sabbir-mahmud/servers-monitor/device-agent/internal/config"
)

// The Windows location service answers desktop programs when "Location services" and
// "Let desktop apps access your location" are on (Settings > Privacy & security >
// Location), without a prompt. It belongs to the signed-in person's session, so the
// service asks through a copy of itself there (`nexguard locate`), as it does for screen
// time.
const locateScript = `
Add-Type -AssemblyName System.Device
$w = New-Object System.Device.Location.GeoCoordinateWatcher([System.Device.Location.GeoPositionAccuracy]::High)
if (-not $w.TryStart($false, [TimeSpan]::FromSeconds(10))) { exit 2 }
$deadline = (Get-Date).AddSeconds(20)
while ($w.Position.Location.IsUnknown -and (Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 300 }
$l = $w.Position.Location
$w.Stop()
if ($l.IsUnknown) { exit 3 }
$c = [Globalization.CultureInfo]::InvariantCulture
'{"lat":' + $l.Latitude.ToString($c) + ',"lon":' + $l.Longitude.ToString($c) + ',"accuracy":' + $l.HorizontalAccuracy.ToString($c) + '}'
`

func locate() (*Fix, error) {
	var out []byte
	var err error
	if config.UserMode() {
		out, err = locateHere()
	} else {
		session := windows.WTSGetActiveConsoleSessionId()
		if session == noSession {
			return nil, errors.New("nobody signed in")
		}
		var token windows.Token
		if err := windows.WTSQueryUserToken(session, &token); err != nil {
			return nil, fmt.Errorf("nobody signed in: %w", err)
		}
		defer token.Close()
		exe, err2 := os.Executable()
		if err2 != nil {
			return nil, err2
		}
		out, err = runAsUser(token, exe, "locate", 40*time.Second)
	}
	if err != nil {
		return nil, err
	}
	var f Fix
	if err := json.Unmarshal(out, &f); err != nil {
		return nil, ErrNoLocationService // location services off: the helper printed nothing
	}
	return &f, nil
}

// locateHere runs the location query in this session and returns its JSON line.
func locateHere() ([]byte, error) {
	ctx, cancel := context.WithTimeout(context.Background(), 35*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, "powershell.exe", "-NoProfile", "-NonInteractive",
		"-ExecutionPolicy", "Bypass", "-EncodedCommand", encodePS(locateScript))
	cmd.SysProcAttr = &syscall.SysProcAttr{HideWindow: true, CreationFlags: windows.CREATE_NO_WINDOW}
	out, err := cmd.Output()
	if err != nil {
		return nil, ErrNoLocationService
	}
	return []byte(strings.TrimSpace(string(out))), nil
}

// PrintLocation is `nexguard locate`, run as the signed-in person by the service.
func PrintLocation() error {
	out, err := locateHere()
	if err != nil {
		return err
	}
	_, err = os.Stdout.Write(append(out, '\n'))
	return err
}

func encodePS(script string) string {
	u := utf16.Encode([]rune(script))
	b := make([]byte, 2*len(u))
	for i, r := range u {
		b[2*i], b[2*i+1] = byte(r), byte(r>>8)
	}
	return base64.StdEncoding.EncodeToString(b)
}
