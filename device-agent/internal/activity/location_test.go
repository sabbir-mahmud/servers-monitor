package activity

import (
	"errors"
	"testing"
	"time"
)

func TestLocationOnlyWhileSwitchedOnAndFresh(t *testing.T) {
	setup(t)
	defer func(f func() (*Fix, error)) { locateFn = f }(locateFn)
	calls := 0
	locateFn = func() (*Fix, error) { calls++; return &Fix{Lat: 23.81, Lon: 90.41, Accuracy: 30}, nil }

	if err := LocateOnce(); err != nil || calls != 0 || LastFix() != nil {
		t.Fatal("off: the location service must not even be asked")
	}
	_ = SetControls(Controls{Location: true})
	if err := LocateOnce(); err != nil || calls != 1 {
		t.Fatalf("on: %v", err)
	}
	if f := LastFix(); f == nil || f.Lat != 23.81 || f.Accuracy != 30 {
		t.Fatalf("fix: %+v", f)
	}
	fixMu.Lock()
	lastFix.At = time.Now().Add(-fixMaxAge - time.Second)
	fixMu.Unlock()
	if LastFix() != nil {
		t.Fatal("an old position must not be sent as current")
	}
	_ = LocateOnce()
	_ = SetControls(Controls{})
	if LastFix() != nil {
		t.Fatal("switched off: nothing is sent, even a fresh position")
	}
}

func TestImplausiblePositionsAreDropped(t *testing.T) {
	setup(t)
	defer func(f func() (*Fix, error)) { locateFn = f }(locateFn)
	_ = SetControls(Controls{Location: true})
	for _, f := range []*Fix{{Lat: 0, Lon: 0, Accuracy: 10}, {Lat: 91, Lon: 1}, {Lat: 1, Lon: 1, Accuracy: 500_000}} {
		locateFn = func() (*Fix, error) { return f, nil }
		if err := LocateOnce(); err == nil || LastFix() != nil {
			t.Errorf("%+v must be dropped", f)
		}
	}
	locateFn = func() (*Fix, error) { return nil, ErrNoLocationService }
	if err := LocateOnce(); !errors.Is(err, ErrNoLocationService) {
		t.Fatalf("got %v", err)
	}
}
