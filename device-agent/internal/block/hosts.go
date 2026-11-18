// Package block keeps the dashboard's blocklist in the system hosts file, inside a section
// the agent owns. Lines outside that section (the system's own, or an administrator's) are
// never touched. Blocking by hosts file works in every browser and needs no DNS server,
// but a VPN or a browser's own secure DNS can bypass it.
package block

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"os"
	"path/filepath"
	"runtime"
	"strings"
)

const (
	begin = "# >>> NexGuard (managed by the NexGuard agent, edits are undone) >>>"
	end   = "# <<< NexGuard <<<"
)

// Path of the system hosts file. NEXGUARD_HOSTS overrides it (tests).
func Path() string {
	if p := os.Getenv("NEXGUARD_HOSTS"); p != "" {
		return p
	}
	if runtime.GOOS == "windows" {
		root := os.Getenv("SystemRoot")
		if root == "" {
			root = `C:\Windows`
		}
		return filepath.Join(root, "System32", "drivers", "etc", "hosts")
	}
	return "/etc/hosts"
}

// Apply makes the managed section hold exactly these "ip domain" lines (none: no section).
// It writes only when something changed, and reports whether it did.
func Apply(lines []string) (bool, error) {
	path := Path()
	old, err := os.ReadFile(path)
	if err != nil && !errors.Is(err, os.ErrNotExist) {
		return false, err
	}
	updated := render(old, lines)
	if bytes.Equal(old, updated) {
		return false, nil
	}
	if err := write(path, updated); err != nil {
		return false, err
	}
	flushDNS()
	return true, nil
}

// Remove takes the managed section out (unpaired, blocking turned off, uninstalled).
func Remove() error {
	_, err := Apply(nil)
	return err
}

// Fingerprint of the managed section as it is on disk ("" when there is none), to notice
// someone editing it.
func Fingerprint() string {
	data, err := os.ReadFile(Path())
	if err != nil {
		return ""
	}
	return fingerprint(section(data))
}

// FingerprintOf the section these lines would make.
func FingerprintOf(lines []string) string {
	return fingerprint(cleanLines(lines))
}

// Count of blocked or redirected names in the managed section.
func Count() int {
	data, err := os.ReadFile(Path())
	if err != nil {
		return 0
	}
	return len(section(data))
}

func fingerprint(lines []string) string {
	if len(lines) == 0 {
		return ""
	}
	sum := sha256.Sum256([]byte(strings.Join(lines, "\n")))
	return hex.EncodeToString(sum[:8])
}

// render: the file without any managed section, then the new section at the end.
func render(old []byte, lines []string) []byte {
	newline := "\n"
	if bytes.Contains(old, []byte("\r\n")) || (len(old) == 0 && runtime.GOOS == "windows") {
		newline = "\r\n"
	}
	var keep []string
	inside := false
	for _, line := range strings.Split(strings.ReplaceAll(string(old), "\r\n", "\n"), "\n") {
		switch strings.TrimSpace(line) {
		case begin:
			inside = true
			continue
		case end:
			inside = false
			continue
		}
		if !inside {
			keep = append(keep, line)
		}
	}
	for len(keep) > 0 && strings.TrimSpace(keep[len(keep)-1]) == "" {
		keep = keep[:len(keep)-1]
	}
	out := keep
	if clean := cleanLines(lines); len(clean) > 0 {
		if len(out) > 0 {
			out = append(out, "")
		}
		out = append(out, begin)
		out = append(out, clean...)
		out = append(out, end)
	}
	if len(out) == 0 {
		return nil
	}
	return []byte(strings.Join(out, newline) + newline)
}

// cleanLines: only "address name" pairs, so a damaged or hostile list can't inject
// anything else into the hosts file.
func cleanLines(lines []string) []string {
	var out []string
	for _, line := range lines {
		f := strings.Fields(line)
		if len(f) != 2 || !validAddress(f[0]) || !validName(f[1]) {
			continue
		}
		out = append(out, f[0]+" "+f[1])
	}
	return out
}

func validAddress(s string) bool {
	if len(s) > 45 {
		return false
	}
	for _, r := range s {
		if !(r >= '0' && r <= '9' || r == '.' || r == ':' || r >= 'a' && r <= 'f') {
			return false
		}
	}
	return s != ""
}

func validName(s string) bool {
	if len(s) > 253 || !strings.Contains(s, ".") {
		return false
	}
	for _, r := range s {
		if !(r >= 'a' && r <= 'z' || r >= '0' && r <= '9' || r == '.' || r == '-') {
			return false
		}
	}
	return true
}

// section: the lines inside the managed section.
func section(data []byte) []string {
	var out []string
	inside := false
	for _, line := range strings.Split(strings.ReplaceAll(string(data), "\r\n", "\n"), "\n") {
		switch strings.TrimSpace(line) {
		case begin:
			inside = true
		case end:
			inside = false
		default:
			if inside && strings.TrimSpace(line) != "" {
				out = append(out, strings.TrimSpace(line))
			}
		}
	}
	return out
}
