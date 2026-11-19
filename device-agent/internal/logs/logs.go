// Package logs keeps the agent's own log as a small file next to its config, one JSON
// object per line, so the person using the computer (and the NexGuard desktop app) can see
// what it does: check-ins, settings the dashboard switched on or off, and errors. It holds
// no secrets: never the pairing token, never a website or an app name.
//
// The file is logs/nexguard.log in config.Dir(). It is readable by everyone and rotates at
// MaxSize into nexguard.log.1, so it never takes more than about 2 MB.
package logs

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"sync"

	"github.com/sabbir-mahmud/servers-monitor/device-agent/internal/config"
)

// MaxSize of the current file before it's rotated.
var MaxSize int64 = 1 << 20

func Dir() string  { return filepath.Join(config.Dir(), "logs") }
func Path() string { return filepath.Join(Dir(), "nexguard.log") }

// Setup sends slog (and the standard log package, which slog takes over) to the log file
// and, when console isn't nil, to console as text too: the terminal or the service log.
func Setup(console io.Writer) {
	opts := &slog.HandlerOptions{Level: slog.LevelInfo}
	handlers := []slog.Handler{slog.NewJSONHandler(&File{}, opts)}
	if console != nil {
		handlers = append(handlers, slog.NewTextHandler(console, opts))
	}
	slog.SetDefault(slog.New(fanout(handlers)))
}

// File appends to Path(), creating the folder on first use and rotating at MaxSize. A
// failure to write (no rights, full disk) is dropped: logging never stops the agent.
type File struct {
	mu   sync.Mutex
	f    *os.File
	size int64
}

func (w *File) Write(p []byte) (int, error) {
	w.mu.Lock()
	defer w.mu.Unlock()
	if w.f != nil && w.size+int64(len(p)) > MaxSize {
		w.rotate()
	}
	if w.f == nil && w.open() != nil {
		return len(p), nil
	}
	n, err := w.f.Write(p)
	w.size += int64(n)
	if err != nil { // the file went away or the disk is full: reopen next time
		_ = w.f.Close()
		w.f = nil
	}
	return len(p), nil
}

func (w *File) open() error {
	if err := os.MkdirAll(Dir(), 0o755); err != nil {
		return err
	}
	f, err := os.OpenFile(Path(), os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o644)
	if err != nil {
		return err
	}
	info, err := f.Stat()
	if err != nil {
		f.Close()
		return err
	}
	w.f, w.size = f, info.Size()
	if w.size > MaxSize {
		w.rotate()
		return w.open()
	}
	return nil
}

func (w *File) rotate() {
	if w.f != nil {
		_ = w.f.Close()
		w.f = nil
	}
	old := Path() + ".1"
	_ = os.Remove(old) // Windows can't rename over an existing file
	_ = os.Rename(Path(), old)
	w.size = 0
}

// Close the file (tests; the agent keeps it open until it exits).
func (w *File) Close() error {
	w.mu.Lock()
	defer w.mu.Unlock()
	if w.f == nil {
		return nil
	}
	err := w.f.Close()
	w.f = nil
	return err
}

// fanout sends each record to every handler that wants it.
type fanout []slog.Handler

func (h fanout) Enabled(ctx context.Context, l slog.Level) bool {
	for _, x := range h {
		if x.Enabled(ctx, l) {
			return true
		}
	}
	return false
}

func (h fanout) Handle(ctx context.Context, r slog.Record) error {
	var errs []error
	for _, x := range h {
		if x.Enabled(ctx, r.Level) {
			errs = append(errs, x.Handle(ctx, r.Clone()))
		}
	}
	return errors.Join(errs...)
}

func (h fanout) WithAttrs(attrs []slog.Attr) slog.Handler {
	out := make(fanout, len(h))
	for i, x := range h {
		out[i] = x.WithAttrs(attrs)
	}
	return out
}

func (h fanout) WithGroup(name string) slog.Handler {
	out := make(fanout, len(h))
	for i, x := range h {
		out[i] = x.WithGroup(name)
	}
	return out
}
