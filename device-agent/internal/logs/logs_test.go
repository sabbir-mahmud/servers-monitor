package logs

import (
	"bytes"
	"encoding/json"
	"log"
	"log/slog"
	"os"
	"strings"
	"testing"
)

func TestJSONLinesToFileAndTextToConsole(t *testing.T) {
	t.Setenv("NEXGUARD_CONFIG_DIR", t.TempDir())
	var console bytes.Buffer
	Setup(&console)
	slog.Info("checked in", "interval", 300)
	log.Printf("from the log package") // slog took it over

	data, err := os.ReadFile(Path())
	if err != nil {
		t.Fatal(err)
	}
	lines := strings.Split(strings.TrimSpace(string(data)), "\n")
	if len(lines) != 2 {
		t.Fatalf("want 2 lines, got %q", data)
	}
	var first map[string]any
	if err := json.Unmarshal([]byte(lines[0]), &first); err != nil {
		t.Fatal(err)
	}
	if first["level"] != "INFO" || first["msg"] != "checked in" || first["interval"] != 300.0 || first["time"] == nil {
		t.Fatalf("unexpected record %v", first)
	}
	if !strings.Contains(lines[1], `"msg":"from the log package"`) {
		t.Fatalf("log.Printf not captured: %s", lines[1])
	}
	if !strings.Contains(console.String(), "msg=\"checked in\" interval=300") {
		t.Fatalf("console: %q", console.String())
	}
	if info, _ := os.Stat(Path()); info.Mode().Perm()&0o044 != 0o044 {
		t.Fatalf("log file should be readable by everyone, mode %v", info.Mode())
	}
}

func TestRotatesAtMaxSize(t *testing.T) {
	t.Setenv("NEXGUARD_CONFIG_DIR", t.TempDir())
	defer func(old int64) { MaxSize = old }(MaxSize)
	MaxSize = 100
	w := &File{}
	defer w.Close()
	line := []byte(strings.Repeat("x", 59) + "\n") // 60 bytes
	for range 3 {
		if _, err := w.Write(line); err != nil {
			t.Fatal(err)
		}
	}
	cur, _ := os.ReadFile(Path())
	old, _ := os.ReadFile(Path() + ".1")
	if len(cur) != 60 || len(old) != 60 {
		t.Fatalf("want one line in each file, got %d and %d bytes", len(cur), len(old))
	}
}

func TestUnwritableFolderIsIgnored(t *testing.T) {
	dir := t.TempDir()
	blocker := dir + "/file"
	_ = os.WriteFile(blocker, nil, 0o644)
	t.Setenv("NEXGUARD_CONFIG_DIR", blocker) // logs/ can't be created under a file
	w := &File{}
	if n, err := w.Write([]byte("hello\n")); err != nil || n != 6 {
		t.Fatalf("write should be dropped quietly, got %d %v", n, err)
	}
}
