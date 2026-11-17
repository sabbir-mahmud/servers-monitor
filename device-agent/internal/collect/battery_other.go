//go:build !darwin

package collect

import (
	"math"

	"github.com/distatus/battery"
)

// batteryReading: the first battery with a known capacity (sysfs on Linux, the Win32
// battery API on Windows). Desktops have none, and report nothing.
func batteryReading() map[string]any {
	all, _ := battery.GetAll() // partial errors still return the batteries it could read
	for _, b := range all {
		if b == nil || b.Full <= 0 {
			continue
		}
		pct := math.Min(100, math.Round(100*b.Current/b.Full))
		// "charging" in the API means plugged in (as the phone apps and scripts send it).
		return map[string]any{"percent": int(pct), "charging": b.State.Raw != battery.Discharging}
	}
	return nil
}
