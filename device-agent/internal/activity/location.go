package activity

import (
	"errors"
	"sync"
	"time"
)

// LocateEvery: how often the agent asks the computer where it is (check-ins are every 5
// minutes, so each one carries a fresh position).
const LocateEvery = 4 * time.Minute

// fixMaxAge: an older position isn't sent; the dashboard then uses the internet address.
const fixMaxAge = 10 * time.Minute

// ErrNoLocationService: this computer has no location service the agent may use (a Mac:
// that needs a signed app the person allows). Not worth logging every few minutes.
var ErrNoLocationService = errors.New("no location service the agent can use here")

// Fix is a position from the computer's own location service (Wi-Fi based).
type Fix struct {
	Lat      float64   `json:"lat"`
	Lon      float64   `json:"lon"`
	Accuracy float64   `json:"accuracy"` // metres
	At       time.Time `json:"at"`
}

var (
	fixMu    sync.Mutex
	lastFix  *Fix
	locateFn = locate // the system's location service; tests stand in for it
)

// LocateOnce asks the system for a position, when Location is switched on.
func LocateOnce() error {
	if !CurrentControls().Location {
		setFix(nil)
		return nil
	}
	f, err := locateFn()
	if err != nil {
		return err
	}
	if !plausible(f) {
		return errors.New("the location service gave no usable position")
	}
	f.At = time.Now()
	setFix(f)
	return nil
}

// LastFix: the latest position if it's recent and Location is still on, else nil.
func LastFix() *Fix {
	fixMu.Lock()
	defer fixMu.Unlock()
	if lastFix == nil || time.Since(lastFix.At) > fixMaxAge || !CurrentControls().Location {
		return nil
	}
	f := *lastFix
	return &f
}

func setFix(f *Fix) {
	fixMu.Lock()
	lastFix = f
	fixMu.Unlock()
}

func plausible(f *Fix) bool {
	return f != nil && f.Lat >= -90 && f.Lat <= 90 && f.Lon >= -180 && f.Lon <= 180 &&
		!(f.Lat == 0 && f.Lon == 0) && f.Accuracy >= 0 && f.Accuracy < 100_000
}
