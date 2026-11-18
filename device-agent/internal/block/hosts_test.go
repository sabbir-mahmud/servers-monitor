package block

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func hostsFile(t *testing.T, content string) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "hosts")
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
	t.Setenv("NEXGUARD_HOSTS", path)
	return path
}

func read(t *testing.T, path string) string {
	t.Helper()
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	return string(data)
}

func TestApplyKeepsOtherLinesAndIsIdempotent(t *testing.T) {
	system := "127.0.0.1 localhost\n::1 localhost\n10.0.0.5 nas.home\n"
	path := hostsFile(t, system)
	lines := []string{"0.0.0.0 bad.example", "216.239.38.120 www.google.com"}
	if changed, err := Apply(lines); err != nil || !changed {
		t.Fatalf("first apply: %v %v", changed, err)
	}
	got := read(t, path)
	if !strings.HasPrefix(got, system) || !strings.Contains(got, "\n0.0.0.0 bad.example\n") {
		t.Fatalf("hosts:\n%s", got)
	}
	if changed, _ := Apply(lines); changed {
		t.Error("same lines must not rewrite the file")
	}
	if Count() != 2 || Fingerprint() != FingerprintOf(lines) {
		t.Errorf("count %d, fingerprint mismatch", Count())
	}
	if _, err := Apply([]string{"0.0.0.0 other.example"}); err != nil {
		t.Fatal(err)
	}
	if got := read(t, path); strings.Contains(got, "bad.example") || strings.Count(got, begin) != 1 {
		t.Fatalf("replace:\n%s", got)
	}
	if err := Remove(); err != nil {
		t.Fatal(err)
	}
	if got := read(t, path); got != system {
		t.Fatalf("remove left:\n%q", got)
	}
}

func TestEditsToTheSectionAreNoticed(t *testing.T) {
	path := hostsFile(t, "127.0.0.1 localhost\n")
	lines := []string{"0.0.0.0 bad.example"}
	_, _ = Apply(lines)
	edited := strings.Replace(read(t, path), "0.0.0.0 bad.example\n", "", 1)
	_ = os.WriteFile(path, []byte(edited), 0o644)
	if Fingerprint() == FingerprintOf(lines) {
		t.Error("an emptied section must look different")
	}
	if changed, _ := Apply(lines); !changed {
		t.Error("apply must restore it")
	}
}

func TestOnlyAddressNamePairsGetIn(t *testing.T) {
	hostsFile(t, "")
	_, _ = Apply([]string{
		"0.0.0.0 ok.example",
		"0.0.0.0 two.example three.example",
		"0.0.0.0 evil.example\n127.0.0.1 bank.example",
		"# >>> NexGuard",
		"not-an-ip ok.example",
		"0.0.0.0 UPPER.example",
	})
	if Count() != 1 {
		t.Fatalf("section: %v", section([]byte(read(t, Path()))))
	}
}

func TestWindowsLineEndingsAreKept(t *testing.T) {
	path := hostsFile(t, "127.0.0.1 localhost\r\n")
	_, _ = Apply([]string{"0.0.0.0 bad.example"})
	if got := read(t, path); strings.Contains(strings.ReplaceAll(got, "\r\n", ""), "\n") {
		t.Fatalf("mixed line endings: %q", got)
	}
}
