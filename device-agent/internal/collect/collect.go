// Package collect gathers what a computer reports at each check-in: system facts and
// health (CPU, memory, GPU, battery, storage, network). Every reading is optional: one that fails
// is left out, never the whole report, and never sent as a made-up zero.
package collect

import (
	"bufio"
	"context"
	"math"
	"os"
	"os/exec"
	"runtime"
	"strconv"
	"strings"
	"time"

	"github.com/shirou/gopsutil/v4/cpu"
	"github.com/shirou/gopsutil/v4/disk"
	"github.com/shirou/gopsutil/v4/host"
	"github.com/shirou/gopsutil/v4/mem"
)

// Platform is the dashboard's name for this system.
func Platform() string {
	switch runtime.GOOS {
	case "darwin":
		return "macos"
	case "windows":
		return "windows"
	default:
		return "linux"
	}
}

// Info: what pairing sends (no health): platform, system version, model, name.
func Info(appVersion string) map[string]any {
	info := map[string]any{"platform": Platform(), "app_version": "nexguard-agent " + appVersion}
	if v := osVersion(); v != "" {
		info["os_version"] = v
	}
	if m := model(); m != "" {
		info["model"] = m
	}
	if h := hostname(); h != "" {
		info["hostname"] = h
	}
	return info
}

// Report: Info plus health. Takes about a second (CPU is measured over one).
func Report(appVersion string) map[string]any {
	r := Info(appVersion)
	if pct, err := cpu.Percent(time.Second, false); err == nil && len(pct) == 1 && valid(pct[0]) {
		r["cpu"] = map[string]any{"percent": round1(pct[0])}
	}
	if vm, err := mem.VirtualMemory(); err == nil && vm.Total > 0 {
		r["memory"] = map[string]any{"total": vm.Total, "used": min(vm.Used, vm.Total)}
	}
	if du, err := disk.Usage(systemDisk()); err == nil && du.Total > 0 {
		r["storage"] = map[string]any{"total": du.Total, "free": du.Free}
	}
	if b := batteryReading(); b != nil {
		r["battery"] = b
	}
	if g, ok := gpuPercent(); ok && valid(g) {
		r["gpu"] = map[string]any{"percent": round1(g)}
	}
	if n := network(); n != nil {
		r["network"] = n
	}
	return r
}

func systemDisk() string {
	switch runtime.GOOS {
	case "windows":
		if d := os.Getenv("SystemDrive"); d != "" {
			return d + `\`
		}
		return `C:\`
	case "darwin":
		if _, err := os.Stat("/System/Volumes/Data"); err == nil {
			return "/System/Volumes/Data" // the user's files, not the sealed system volume
		}
	}
	return "/"
}

func osVersion() string {
	hi, err := host.Info()
	if err != nil {
		return ""
	}
	switch runtime.GOOS {
	case "darwin":
		return "macOS " + hi.PlatformVersion
	case "windows":
		return strings.TrimPrefix(strings.TrimSpace(hi.Platform+" "+hi.PlatformVersion), "Microsoft ")
	}
	if pretty := osRelease("PRETTY_NAME"); pretty != "" {
		return pretty
	}
	return strings.TrimSpace(hi.Platform + " " + hi.PlatformVersion)
}

func hostname() string {
	if runtime.GOOS == "darwin" {
		if out := run("scutil", "--get", "ComputerName"); out != "" {
			return out // "Sara's MacBook Air", not "saras-mba.local"
		}
	}
	h, _ := os.Hostname()
	return strings.SplitN(h, ".", 2)[0]
}

func model() string {
	switch runtime.GOOS {
	case "darwin":
		if chip := run("sysctl", "-n", "machdep.cpu.brand_string"); chip != "" {
			return "Mac · " + chip
		}
		return "Mac"
	case "linux":
		vendor := readFirst("/sys/devices/virtual/dmi/id/sys_vendor")
		product := readFirst("/sys/devices/virtual/dmi/id/product_name")
		switch product {
		case "", "To Be Filled By O.E.M.", "System Product Name", "Default string":
			return runtime.GOARCH
		}
		if vendor == "" || strings.Contains(product, vendor) {
			return product
		}
		return vendor + " " + product
	}
	return windowsModel()
}

// ---------------------------------------------------------------------------- helpers

func valid(v float64) bool { return !math.IsNaN(v) && v >= 0 && v <= 100 }

func round1(v float64) float64 { return math.Round(v*10) / 10 }

// run a helper program for at most 5 seconds; "" if it fails.
func run(name string, args ...string) string {
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	out, err := exec.CommandContext(ctx, name, args...).Output()
	if err != nil {
		return ""
	}
	return strings.TrimSpace(string(out))
}

func readFirst(path string) string {
	data, err := os.ReadFile(path)
	if err != nil {
		return ""
	}
	return strings.TrimSpace(strings.SplitN(string(data), "\n", 2)[0])
}

func osRelease(key string) string {
	f, err := os.Open("/etc/os-release")
	if err != nil {
		return ""
	}
	defer f.Close()
	s := bufio.NewScanner(f)
	for s.Scan() {
		if k, v, ok := strings.Cut(s.Text(), "="); ok && k == key {
			if u, err := strconv.Unquote(v); err == nil {
				return u
			}
			return strings.Trim(v, `"'`)
		}
	}
	return ""
}

// firstNumber: the first number in the output, e.g. "18" from `"Device Utilization %"=18`.
func firstNumber(s string) (float64, bool) {
	start := -1
	for i, r := range s {
		isNum := (r >= '0' && r <= '9') || (r == '.' && start >= 0)
		if isNum && start < 0 {
			start = i
		} else if !isNum && start >= 0 {
			v, err := strconv.ParseFloat(s[start:i], 64)
			return v, err == nil
		}
	}
	if start >= 0 {
		v, err := strconv.ParseFloat(s[start:], 64)
		return v, err == nil
	}
	return 0, false
}
