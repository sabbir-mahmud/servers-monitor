package activity

import (
	"database/sql"
	"errors"
	"fmt"
	"io"
	"net"
	"net/url"
	"os"
	"path/filepath"
	"runtime"
	"sort"
	"strings"
	"time"

	"golang.org/x/net/publicsuffix"
	_ "modernc.org/sqlite" // pure Go: the agent stays one static file, no C library
)

// WebEvery: how often browser histories are read.
const WebEvery = 10 * time.Minute

const (
	maxVisitSeconds = 3600            // one visit can't count more than this
	gapCap          = 5 * time.Minute // Firefox/Safari: time until the next visit, capped
	readLimit       = 5000            // visits per history file per read
)

// historyFile is one browser profile's history database.
type historyFile struct {
	Path    string
	User    string
	Browser string
	Kind    string // chromium, firefox, safari
}

type visit struct {
	ID       int64
	URL      string
	At       time.Time
	Duration time.Duration // 0 when the browser doesn't record it
}

// ReadWebOnce reads new visits from every browser history on the computer and counts them.
// A history the agent can't read (Safari without Full Disk Access) is skipped.
func ReadWebOnce() error {
	if !CurrentControls().WebActivity {
		return nil
	}
	seen := cursors()
	var rows []WebRow
	moved := map[string]int64{}
	var errs []error
	for _, f := range historyFiles() {
		after, known := seen[f.Path]
		visits, last, err := readHistory(f, after, !known)
		if err != nil {
			errs = append(errs, fmt.Errorf("%s (%s): %w", f.Browser, f.User, err))
			continue
		}
		moved[f.Path] = last
		rows = append(rows, summarize(f, visits)...)
	}
	if err := addWeb(rows, moved); err != nil {
		return err
	}
	return errors.Join(errs...)
}

// readHistory returns visits after `after` and the new cursor. The first time a file is
// seen (fresh) it only notes where its history ends: nothing from before monitoring was
// switched on is sent.
func readHistory(f historyFile, after int64, fresh bool) ([]visit, int64, error) {
	dir, err := os.MkdirTemp("", "nexguard-history-")
	if err != nil {
		return nil, after, err
	}
	defer os.RemoveAll(dir)
	// The browser keeps its database locked and half in a write-ahead log: read a copy.
	copyPath := filepath.Join(dir, "history.db")
	if err := copyFile(f.Path, copyPath); err != nil {
		return nil, after, err
	}
	_ = copyFile(f.Path+"-wal", copyPath+"-wal")
	db, err := sql.Open("sqlite", "file:"+filepath.ToSlash(copyPath)+"?_pragma=busy_timeout(2000)")
	if err != nil {
		return nil, after, err
	}
	defer db.Close()

	var maxID sql.NullInt64
	if err := db.QueryRow(queries[f.Kind].max).Scan(&maxID); err != nil {
		return nil, after, err
	}
	if fresh {
		return nil, maxID.Int64, nil
	}
	if maxID.Int64 < after {
		after = 0 // history was cleared and ids started again
	}
	rows, err := db.Query(queries[f.Kind].visits, after, readLimit)
	if err != nil {
		return nil, after, err
	}
	defer rows.Close()
	var out []visit
	last := after
	for rows.Next() {
		var v visit
		var at, dur int64
		if err := rows.Scan(&v.ID, &v.URL, &at, &dur); err != nil {
			return nil, after, err
		}
		v.At = toTime(f.Kind, at)
		v.Duration = time.Duration(dur) * time.Microsecond
		out = append(out, v)
		last = v.ID
	}
	return out, last, rows.Err()
}

var queries = map[string]struct{ max, visits string }{
	// Chromium: visit_time is microseconds since 1601; subframe navigations (ads, embeds)
	// aren't visits a person made.
	"chromium": {
		max: `SELECT MAX(id) FROM visits`,
		visits: `SELECT v.id, u.url, v.visit_time, v.visit_duration FROM visits v
			JOIN urls u ON u.id = v.url
			WHERE v.id > ? AND (v.transition & 255) NOT IN (3, 4)
			ORDER BY v.id LIMIT ?`,
	},
	// Firefox: visit_date is microseconds since 1970; types 5 and 6 are redirects.
	"firefox": {
		max: `SELECT MAX(id) FROM moz_historyvisits`,
		visits: `SELECT v.id, p.url, v.visit_date, 0 FROM moz_historyvisits v
			JOIN moz_places p ON p.id = v.place_id
			WHERE v.id > ? AND v.visit_type NOT IN (5, 6)
			ORDER BY v.id LIMIT ?`,
	},
	// Safari: visit_time is seconds since 2001 (a float).
	"safari": {
		max: `SELECT MAX(id) FROM history_visits`,
		visits: `SELECT v.id, i.url, CAST(v.visit_time * 1000000 AS INTEGER), 0 FROM history_visits v
			JOIN history_items i ON i.id = v.history_item
			WHERE v.id > ? ORDER BY v.id LIMIT ?`,
	},
}

var (
	webkitEpoch = time.Date(1601, 1, 1, 0, 0, 0, 0, time.UTC)
	appleEpoch  = time.Date(2001, 1, 1, 0, 0, 0, 0, time.UTC)
)

func toTime(kind string, micros int64) time.Time {
	d := time.Duration(micros) * time.Microsecond
	switch kind {
	case "chromium":
		// Too far for one Duration (292 years max): add whole days first.
		days := micros / (86400 * 1_000_000)
		rest := time.Duration(micros%(86400*1_000_000)) * time.Microsecond
		return webkitEpoch.AddDate(0, 0, int(days)).Add(rest).Local()
	case "safari":
		return appleEpoch.Add(d).Local()
	}
	return time.Unix(0, 0).Add(d).Local()
}

// summarize visits into (day, domain) rows. Time is the browser's own visit duration
// (Chromium) or the gap until the next visit, capped (Firefox, Safari).
func summarize(f historyFile, visits []visit) []WebRow {
	sort.Slice(visits, func(i, j int) bool { return visits[i].At.Before(visits[j].At) })
	type k struct{ day, domain string }
	acc := map[k]*WebRow{}
	var order []k
	for i, v := range visits {
		domain := Domain(v.URL)
		if domain == "" {
			continue
		}
		spent := v.Duration
		if spent == 0 && i+1 < len(visits) {
			spent = min(visits[i+1].At.Sub(v.At), gapCap)
		}
		spent = min(max(spent, 0), maxVisitSeconds*time.Second)
		key := k{v.At.Format("2006-01-02"), domain}
		row, ok := acc[key]
		if !ok {
			row = &WebRow{Day: key.day, User: f.User, Browser: f.Browser, Domain: domain}
			acc[key] = row
			order = append(order, key)
		}
		row.Visits++
		row.Seconds += int(spent / time.Second)
	}
	out := make([]WebRow, 0, len(order))
	for _, key := range order {
		out = append(out, *acc[key])
	}
	return out
}

// Domain: the site a URL belongs to ("https://m.youtube.com/watch?v=x" -> "youtube.com"),
// or "" for anything that isn't a public website (files, settings pages, local addresses).
func Domain(raw string) string {
	u, err := url.Parse(raw)
	if err != nil || (u.Scheme != "http" && u.Scheme != "https") {
		return ""
	}
	host := strings.TrimSuffix(strings.ToLower(u.Hostname()), ".")
	if host == "" || net.ParseIP(host) != nil || !strings.Contains(host, ".") {
		return ""
	}
	site, err := publicsuffix.EffectiveTLDPlusOne(host)
	if err != nil {
		return ""
	}
	return site
}

// historyFiles on this computer, for every local user.
func historyFiles() []historyFile {
	var out []historyFile
	for _, home := range homes() {
		user := filepath.Base(home)
		for browser, globs := range chromiumGlobs() {
			for _, g := range globs {
				profiles, _ := filepath.Glob(filepath.Join(home, g))
				for _, p := range profiles {
					out = append(out, historyFile{Path: p, User: user, Browser: browser, Kind: "chromium"})
				}
			}
		}
		profiles, _ := filepath.Glob(filepath.Join(home, firefoxDir(), "*", "places.sqlite"))
		for _, p := range profiles {
			out = append(out, historyFile{Path: p, User: user, Browser: "Firefox", Kind: "firefox"})
		}
		if runtime.GOOS == "darwin" {
			p := filepath.Join(home, "Library", "Safari", "History.db")
			if _, err := os.Stat(p); err == nil {
				out = append(out, historyFile{Path: p, User: user, Browser: "Safari", Kind: "safari"})
			}
		}
	}
	return out
}

func homes() []string {
	var pattern string
	switch runtime.GOOS {
	case "windows":
		pattern = filepath.Join(os.Getenv("SystemDrive")+`\`, "Users", "*")
	case "darwin":
		pattern = "/Users/*"
	default:
		pattern = "/home/*"
	}
	if p := os.Getenv("NEXGUARD_HOMES"); p != "" {
		pattern = p // tests
	}
	dirs, _ := filepath.Glob(pattern)
	var out []string
	for _, d := range dirs {
		switch strings.ToLower(filepath.Base(d)) {
		case "shared", "public", "default", "default user", "all users", "guest":
			continue
		}
		if info, err := os.Stat(d); err == nil && info.IsDir() {
			out = append(out, d)
		}
	}
	return out
}

// chromiumGlobs: browser -> where its profiles keep "History", relative to the home folder.
// Profiles are subfolders ("Default", "Profile 1"); Opera keeps one at the top.
func chromiumGlobs() map[string][]string {
	j := filepath.Join
	switch runtime.GOOS {
	case "windows":
		l := j("AppData", "Local")
		return map[string][]string{
			"Chrome":   {j(l, "Google", "Chrome", "User Data", "*", "History")},
			"Edge":     {j(l, "Microsoft", "Edge", "User Data", "*", "History")},
			"Brave":    {j(l, "BraveSoftware", "Brave-Browser", "User Data", "*", "History")},
			"Vivaldi":  {j(l, "Vivaldi", "User Data", "*", "History")},
			"Chromium": {j(l, "Chromium", "User Data", "*", "History")},
			"Opera":    {j("AppData", "Roaming", "Opera Software", "Opera*", "History")},
		}
	case "darwin":
		a := j("Library", "Application Support")
		return map[string][]string{
			"Chrome":   {j(a, "Google", "Chrome", "*", "History")},
			"Edge":     {j(a, "Microsoft Edge", "*", "History")},
			"Brave":    {j(a, "BraveSoftware", "Brave-Browser", "*", "History")},
			"Vivaldi":  {j(a, "Vivaldi", "*", "History")},
			"Chromium": {j(a, "Chromium", "*", "History")},
			"Opera":    {j(a, "com.operasoftware.Opera", "History")},
		}
	}
	c := ".config"
	return map[string][]string{
		"Chrome":   {j(c, "google-chrome", "*", "History")},
		"Edge":     {j(c, "microsoft-edge", "*", "History")},
		"Brave":    {j(c, "BraveSoftware", "Brave-Browser", "*", "History")},
		"Vivaldi":  {j(c, "vivaldi", "*", "History")},
		"Chromium": {j(c, "chromium", "*", "History")},
		"Opera":    {j(c, "opera", "History")},
	}
}

func firefoxDir() string {
	switch runtime.GOOS {
	case "windows":
		return filepath.Join("AppData", "Roaming", "Mozilla", "Firefox", "Profiles")
	case "darwin":
		return filepath.Join("Library", "Application Support", "Firefox", "Profiles")
	}
	return filepath.Join(".mozilla", "firefox")
}

func copyFile(src, dst string) error {
	in, err := os.Open(src)
	if err != nil {
		return err
	}
	defer in.Close()
	out, err := os.OpenFile(dst, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, 0o600)
	if err != nil {
		return err
	}
	if _, err := io.Copy(out, in); err != nil {
		out.Close()
		return err
	}
	return out.Close()
}
