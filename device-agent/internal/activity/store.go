// Package activity counts screen time (which app is in front, while someone is using the
// computer) and websites visited (from the browsers' own history, reduced to the site's
// domain on this computer: pages and searches never leave it). It only runs while the
// dashboard has it switched on for this computer.
package activity

import (
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"

	"github.com/sabbir-mahmud/servers-monitor/device-agent/internal/config"
)

// MaxRows sent per check-in; the rest wait for the next one.
const MaxRows = 500

// Controls is what the dashboard asked for at the last check-in.
type Controls struct {
	ScreenTime  bool   `json:"screen_time"`
	WebActivity bool   `json:"web_activity"`
	Blocking    string `json:"blocking"` // blocklist version; "" = no blocking
}

type ScreenRow struct {
	Day     string `json:"day"`
	User    string `json:"user"`
	App     string `json:"app"`
	Seconds int    `json:"seconds"`
}

type WebRow struct {
	Day     string `json:"day"`
	User    string `json:"user"`
	Browser string `json:"browser"`
	Domain  string `json:"domain"`
	Visits  int    `json:"visits"`
	Seconds int    `json:"seconds"`
}

// Batch is what one check-in carries. Until the dashboard confirms it, the same batch
// (same id) is sent again, so a lost answer never counts anything twice.
type Batch struct {
	ID     string      `json:"batch"`
	Screen []ScreenRow `json:"screen"`
	Web    []WebRow    `json:"web"`
}

type state struct {
	Controls Controls          `json:"controls"`
	Screen   map[string]int    `json:"screen"`              // "day\x00user\x00app" -> seconds
	Web      map[string][2]int `json:"web"`                 // "day\x00user\x00browser\x00domain" -> visits, seconds
	Cursors  map[string]int64  `json:"cursors"`             // history file -> last visit id read
	InFlight *Batch            `json:"inflight"`            // sent, not yet confirmed
	Applied  string            `json:"applied"`             // blocklist version in the hosts file
	NoAccess []string          `json:"no_access,omitempty"` // browsers the system won't let it read
}

// NoAccess: browsers whose history the system refused at the last read (sorted).
func NoAccess() []string {
	mu.Lock()
	defer mu.Unlock()
	return append([]string{}, load().NoAccess...)
}

func setNoAccess(denied map[string]bool) {
	names := make([]string, 0, len(denied))
	for b := range denied {
		names = append(names, b)
	}
	sort.Strings(names)
	mu.Lock()
	defer mu.Unlock()
	s := load()
	if strings.Join(s.NoAccess, ",") != strings.Join(names, ",") {
		s.NoAccess = names
		save(s)
	}
}

var (
	mu  sync.Mutex
	cur *state
)

func path() string { return filepath.Join(config.Dir(), "activity.json") }

// load the state once; callers hold mu.
func load() *state {
	if cur != nil {
		return cur
	}
	s := &state{}
	if data, err := os.ReadFile(path()); err == nil {
		_ = json.Unmarshal(data, s)
	}
	if s.Screen == nil {
		s.Screen = map[string]int{}
	}
	if s.Web == nil {
		s.Web = map[string][2]int{}
	}
	if s.Cursors == nil {
		s.Cursors = map[string]int64{}
	}
	cur = s
	return s
}

// save writes the state atomically; callers hold mu. Root/SYSTEM only: it names sites.
func save(s *state) error {
	data, err := json.Marshal(s)
	if err != nil {
		return err
	}
	if err := os.MkdirAll(config.Dir(), 0o755); err != nil {
		return err
	}
	tmp := path() + ".tmp"
	if err := os.WriteFile(tmp, data, 0o600); err != nil {
		return err
	}
	return os.Rename(tmp, path())
}

func key(parts ...string) string { return strings.Join(parts, "\x00") }

// CurrentControls as of the last check-in.
func CurrentControls() Controls {
	mu.Lock()
	defer mu.Unlock()
	return load().Controls
}

// SetControls from a check-in answer. Switching something off drops what was counted
// for it and not yet sent: the parent no longer wants it.
func SetControls(c Controls) error {
	mu.Lock()
	defer mu.Unlock()
	s := load()
	if !c.ScreenTime {
		s.Screen = map[string]int{}
	}
	if !c.WebActivity {
		s.Web = map[string][2]int{}
		s.Cursors = map[string]int64{}
	}
	s.Controls = c
	return save(s)
}

// Applied is the blocklist version last written to the hosts file.
func Applied() string {
	mu.Lock()
	defer mu.Unlock()
	return load().Applied
}

func SetApplied(version string) error {
	mu.Lock()
	defer mu.Unlock()
	s := load()
	s.Applied = version
	return save(s)
}

// AddScreen counts seconds of use for an app.
func AddScreen(day, user, app string, seconds int) {
	mu.Lock()
	defer mu.Unlock()
	s := load()
	if !s.Controls.ScreenTime {
		return
	}
	s.Screen[key(day, user, app)] += seconds
	_ = save(s)
}

// addWeb counts visits; cursors move only together with the rows they produced.
func addWeb(rows []WebRow, cursors map[string]int64) error {
	mu.Lock()
	defer mu.Unlock()
	s := load()
	if !s.Controls.WebActivity {
		return nil
	}
	for _, r := range rows {
		k := key(r.Day, r.User, r.Browser, r.Domain)
		v := s.Web[k]
		s.Web[k] = [2]int{v[0] + r.Visits, v[1] + r.Seconds}
	}
	for file, id := range cursors {
		s.Cursors[file] = id
	}
	return save(s)
}

func cursors() map[string]int64 {
	mu.Lock()
	defer mu.Unlock()
	out := map[string]int64{}
	for k, v := range load().Cursors {
		out[k] = v
	}
	return out
}

// Take the batch to send: the unconfirmed one again, or up to MaxRows new rows. Nil when
// there's nothing.
func Take() *Batch {
	mu.Lock()
	defer mu.Unlock()
	s := load()
	if s.InFlight != nil {
		return s.InFlight
	}
	if len(s.Screen) == 0 && len(s.Web) == 0 {
		return nil
	}
	b := &Batch{ID: newID(), Screen: []ScreenRow{}, Web: []WebRow{}}
	for _, k := range sortedKeys(s.Screen) {
		if len(b.Screen) >= MaxRows {
			break
		}
		p := strings.Split(k, "\x00")
		b.Screen = append(b.Screen, ScreenRow{Day: p[0], User: p[1], App: p[2], Seconds: s.Screen[k]})
		delete(s.Screen, k)
	}
	for _, k := range sortedKeys(s.Web) {
		if len(b.Screen)+len(b.Web) >= MaxRows {
			break
		}
		p := strings.Split(k, "\x00")
		v := s.Web[k]
		b.Web = append(b.Web, WebRow{Day: p[0], User: p[1], Browser: p[2], Domain: p[3], Visits: v[0], Seconds: v[1]})
		delete(s.Web, k)
	}
	s.InFlight = b
	_ = save(s)
	return b
}

// Confirm the batch the dashboard accepted.
func Confirm(id string) {
	mu.Lock()
	defer mu.Unlock()
	s := load()
	if s.InFlight != nil && s.InFlight.ID == id {
		s.InFlight = nil
		_ = save(s)
	}
}

// Reset forgets everything (unpaired or uninstalled).
func Reset() error {
	mu.Lock()
	defer mu.Unlock()
	cur = nil
	err := os.Remove(path())
	if errors.Is(err, os.ErrNotExist) {
		return nil
	}
	return err
}

func sortedKeys[V any](m map[string]V) []string {
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	sort.Strings(keys) // oldest day first
	return keys
}

func newID() string {
	var b [16]byte
	_, _ = rand.Read(b[:])
	b[6] = b[6]&0x0f | 0x40 // a version-4 UUID, as the dashboard expects
	b[8] = b[8]&0x3f | 0x80
	h := hex.EncodeToString(b[:])
	return h[:8] + "-" + h[8:12] + "-" + h[12:16] + "-" + h[16:20] + "-" + h[20:]
}
