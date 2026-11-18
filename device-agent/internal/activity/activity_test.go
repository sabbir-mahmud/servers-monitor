package activity

import (
	"database/sql"
	"os"
	"path/filepath"
	"testing"
	"time"
)

// fresh state in a temp config dir for each test.
func setup(t *testing.T) string {
	t.Helper()
	dir := t.TempDir()
	t.Setenv("NEXGUARD_CONFIG_DIR", dir)
	mu.Lock()
	cur = nil
	mu.Unlock()
	return dir
}

func TestBatchIsResentUntilConfirmed(t *testing.T) {
	setup(t)
	_ = SetControls(Controls{ScreenTime: true})
	AddScreen("2026-11-18", "sara", "Minecraft", 60)
	AddScreen("2026-11-18", "sara", "Minecraft", 60)
	b := Take()
	if b == nil || len(b.Screen) != 1 || b.Screen[0].Seconds != 120 {
		t.Fatalf("batch: %+v", b)
	}
	AddScreen("2026-11-18", "sara", "Safari", 60) // counted while the batch was in flight
	if again := Take(); again.ID != b.ID || len(again.Screen) != 1 {
		t.Fatalf("an unconfirmed batch must be sent again unchanged: %+v", again)
	}
	mu.Lock()
	cur = nil // survives a restart
	mu.Unlock()
	if again := Take(); again == nil || again.ID != b.ID {
		t.Fatal("in-flight batch lost on restart")
	}
	Confirm(b.ID)
	next := Take()
	if next == nil || next.ID == b.ID || next.Screen[0].App != "Safari" {
		t.Fatalf("next batch: %+v", next)
	}
	Confirm(next.ID)
	if Take() != nil {
		t.Fatal("nothing left to send")
	}
}

func TestSwitchingOffDropsWhatWasNotSent(t *testing.T) {
	setup(t)
	_ = SetControls(Controls{ScreenTime: true, WebActivity: true})
	AddScreen("2026-11-18", "sara", "X", 60)
	_ = addWeb([]WebRow{{Day: "2026-11-18", User: "sara", Browser: "Chrome", Domain: "a.com", Visits: 1}},
		map[string]int64{"/h": 9})
	_ = SetControls(Controls{})
	if Take() != nil || len(cursors()) != 0 {
		t.Fatal("off must drop pending rows and cursors (no back-fill when turned on again)")
	}
	AddScreen("2026-11-18", "sara", "X", 60)
	if Take() != nil {
		t.Fatal("nothing is counted while off")
	}
}

func TestBatchesAreCapped(t *testing.T) {
	setup(t)
	_ = SetControls(Controls{ScreenTime: true})
	for i := range MaxRows + 20 {
		AddScreen("2026-11-18", "sara", "app"+string(rune('A'+i%26))+time.Duration(i).String(), 60)
	}
	b := Take()
	if len(b.Screen) != MaxRows {
		t.Fatalf("got %d rows", len(b.Screen))
	}
	Confirm(b.ID)
	if b := Take(); b == nil || len(b.Screen) != 20 {
		t.Fatalf("rest: %+v", b)
	}
}

func TestDomainIsTheSiteNotThePage(t *testing.T) {
	for in, want := range map[string]string{
		"https://m.youtube.com/watch?v=secret": "youtube.com",
		"https://www.bbc.co.uk/news/x":         "bbc.co.uk",
		"http://Example.COM.":                  "example.com",
		"chrome://settings":                    "",
		"file:///Users/sara/a.pdf":             "",
		"http://192.168.1.1/admin":             "",
		"http://localhost:8000/":               "",
		"https://user:pw@shop.example.org/":    "example.org",
	} {
		if got := Domain(in); got != want {
			t.Errorf("Domain(%q) = %q, want %q", in, got, want)
		}
	}
}

func TestFriendlyAppNames(t *testing.T) {
	for in, want := range map[string]string{
		`C:\Program Files\Google\Chrome\Application\chrome.exe`: "Google Chrome",
		`C:\Games\SomeGame.exe`:                                 "Somegame",
		"Google Chrome":                                         "Google Chrome",
		"":                                                      "Unknown",
	} {
		if got := friendlyApp(in); got != want {
			t.Errorf("friendlyApp(%q) = %q, want %q", in, got, want)
		}
	}
}

// ------------------------------------------------------------------ browser histories

const webkitOffset = 11644473600 * 1_000_000 // microseconds from 1601 to 1970

func chromeHistory(t *testing.T, path string) *sql.DB {
	t.Helper()
	db := openDB(t, path)
	mustExec(t, db, `CREATE TABLE urls (id INTEGER PRIMARY KEY, url TEXT)`)
	mustExec(t, db, `CREATE TABLE visits (id INTEGER PRIMARY KEY, url INTEGER, visit_time INTEGER,
		visit_duration INTEGER, transition INTEGER)`)
	return db
}

func chromeVisit(t *testing.T, db *sql.DB, id int, url string, at time.Time, dur time.Duration, transition int) {
	t.Helper()
	mustExec(t, db, `INSERT INTO urls (id, url) VALUES (?, ?)`, id, url)
	mustExec(t, db, `INSERT INTO visits VALUES (?, ?, ?, ?, ?)`, id, id,
		at.UnixMicro()+webkitOffset, dur.Microseconds(), transition)
}

func openDB(t *testing.T, path string) *sql.DB {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	db, err := sql.Open("sqlite", "file:"+filepath.ToSlash(path))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { db.Close() })
	return db
}

func mustExec(t *testing.T, db *sql.DB, q string, args ...any) {
	t.Helper()
	if _, err := db.Exec(q, args...); err != nil {
		t.Fatalf("%s: %v", q, err)
	}
}

func TestChromeHistoryOnlyNewVisitsAsSites(t *testing.T) {
	setup(t)
	homes := t.TempDir()
	t.Setenv("NEXGUARD_HOMES", filepath.Join(homes, "*"))
	path := filepath.Join(homes, "sara", filepath.FromSlash(chromiumGlobs()["Chrome"][0]))
	path = filepath.Join(filepath.Dir(filepath.Dir(path)), "Default", "History")
	db := chromeHistory(t, path)
	now := time.Now()
	chromeVisit(t, db, 1, "https://old.example/before-monitoring", now.Add(-time.Hour), time.Minute, 0)
	_ = SetControls(Controls{WebActivity: true})

	if err := ReadWebOnce(); err != nil { // first read: only notes where history ends
		t.Fatal(err)
	}
	if Take() != nil {
		t.Fatal("history from before monitoring must not be sent")
	}
	chromeVisit(t, db, 2, "https://www.youtube.com/watch?v=1", now, 2*time.Minute, 0)
	chromeVisit(t, db, 3, "https://m.youtube.com/watch?v=2", now, 3*time.Minute, 1)
	chromeVisit(t, db, 4, "https://ads.example/frame", now, time.Minute, 3) // a subframe
	if err := ReadWebOnce(); err != nil {
		t.Fatal(err)
	}
	b := Take()
	if b == nil || len(b.Web) != 1 {
		t.Fatalf("batch: %+v", b)
	}
	if r := b.Web[0]; r.Domain != "youtube.com" || r.Visits != 2 || r.Seconds != 300 ||
		r.User != "sara" || r.Browser != "Chrome" || r.Day != now.Format("2006-01-02") {
		t.Fatalf("row: %+v", r)
	}
	Confirm(b.ID)
	if err := ReadWebOnce(); err != nil || Take() != nil {
		t.Fatal("the same visits must not be counted twice")
	}
}

func TestFirefoxTimeIsTheGapToTheNextVisit(t *testing.T) {
	setup(t)
	homes := t.TempDir()
	t.Setenv("NEXGUARD_HOMES", filepath.Join(homes, "*"))
	path := filepath.Join(homes, "sara", firefoxDir(), "abc.default", "places.sqlite")
	db := openDB(t, path)
	mustExec(t, db, `CREATE TABLE moz_places (id INTEGER PRIMARY KEY, url TEXT)`)
	mustExec(t, db, `CREATE TABLE moz_historyvisits (id INTEGER PRIMARY KEY, place_id INTEGER,
		visit_date INTEGER, visit_type INTEGER)`)
	_ = SetControls(Controls{WebActivity: true})
	if err := ReadWebOnce(); err != nil {
		t.Fatal(err)
	}
	start := time.Now().Add(-time.Hour)
	for i, v := range []struct {
		url  string
		at   time.Duration
		kind int
	}{
		{"https://en.wikipedia.org/wiki/A", 0, 1},
		{"https://en.wikipedia.org/wiki/B", 2 * time.Minute, 1},
		{"https://redirect.example/", 3 * time.Minute, 5},
		{"https://news.example.com/", 4 * time.Minute, 1},
		{"https://news.example.com/2", 30 * time.Minute, 1},
	} {
		mustExec(t, db, `INSERT INTO moz_places VALUES (?, ?)`, i+1, v.url)
		mustExec(t, db, `INSERT INTO moz_historyvisits VALUES (?, ?, ?, ?)`, i+1, i+1,
			start.Add(v.at).UnixMicro(), v.kind)
	}
	if err := ReadWebOnce(); err != nil {
		t.Fatal(err)
	}
	got := map[string]WebRow{}
	for _, r := range Take().Web {
		got[r.Domain] = r
	}
	if w := got["wikipedia.org"]; w.Visits != 2 || w.Seconds != 240 {
		t.Errorf("wikipedia: %+v", w)
	}
	if n := got["example.com"]; n.Visits != 2 || n.Seconds != 300 { // 26 min gap capped at 5
		t.Errorf("news: %+v", n)
	}
	if _, ok := got["redirect.example"]; ok {
		t.Error("redirects aren't visits")
	}
}

func TestABrowserTheSystemWontOpenIsReportedNotSkippedSilently(t *testing.T) {
	if os.Geteuid() == 0 {
		t.Skip("root can open anything")
	}
	setup(t)
	homes := t.TempDir()
	t.Setenv("NEXGUARD_HOMES", filepath.Join(homes, "*"))
	path := filepath.Join(homes, "sara", filepath.FromSlash(chromiumGlobs()["Chrome"][0]))
	root := filepath.Join(homes, "sara", globRoot(chromiumGlobs()["Chrome"][0]))
	chromeHistory(t, filepath.Join(filepath.Dir(filepath.Dir(path)), "Default", "History"))
	_ = SetControls(Controls{WebActivity: true})
	if err := os.Chmod(root, 0); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.Chmod(root, 0o755) })
	if err := ReadWebOnce(); err == nil {
		t.Fatal("a refused browser must be an error the log shows")
	}
	if got := NoAccess(); len(got) != 1 || got[0] != "Chrome" {
		t.Fatalf("no access: %v", got)
	}
	_ = os.Chmod(root, 0o755) // access given: the warning clears
	if err := ReadWebOnce(); err != nil || len(NoAccess()) != 0 {
		t.Fatalf("after access: %v %v", err, NoAccess())
	}
}

func TestGlobRoot(t *testing.T) {
	for in, want := range map[string]string{
		filepath.Join("A", "Google", "Chrome", "*", "History"):                     filepath.Join("A", "Google", "Chrome"),
		filepath.Join("A", "com.operasoftware.Opera", "History"):                   filepath.Join("A", "com.operasoftware.Opera"),
		filepath.Join("AppData", "Roaming", "Opera Software", "Opera*", "History"): filepath.Join("AppData", "Roaming", "Opera Software"),
	} {
		if got := globRoot(in); got != want {
			t.Errorf("globRoot(%q) = %q, want %q", in, got, want)
		}
	}
}

func TestCheckAccessWritesWhatTheInstallerWaitsFor(t *testing.T) {
	if os.Geteuid() == 0 {
		t.Skip("root can open anything")
	}
	setup(t)
	homes := t.TempDir()
	t.Setenv("NEXGUARD_HOMES", filepath.Join(homes, "*"))
	root := filepath.Join(homes, "sara", filepath.FromSlash(firefoxDir()))
	if err := os.MkdirAll(root, 0o755); err != nil {
		t.Fatal(err)
	}
	_ = os.Chmod(root, 0)
	t.Cleanup(func() { _ = os.Chmod(root, 0o755) })
	if got := CheckAccess(); len(got) != 1 || got[0] != "Firefox" {
		t.Fatalf("refused: %v", got)
	}
	if a, err := ReadAccess(); err != nil || len(a.NoAccess) != 1 {
		t.Fatalf("access.json: %+v %v", a, err)
	}
	_ = os.Chmod(root, 0o755)
	if got := CheckAccess(); len(got) != 0 {
		t.Fatalf("after access: %v", got)
	}
	if a, _ := ReadAccess(); len(a.NoAccess) != 0 {
		t.Fatal("access.json must clear")
	}
}
