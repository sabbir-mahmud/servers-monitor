package collect

import (
	"strconv"
	"strings"
)

// The dashboard's network kinds. Anything else (VPN tunnels, virtual switches) is left out:
// its kind says nothing about how the computer is actually connected.
const (
	netWiFi     = "wifi"
	netEthernet = "ethernet"
	netCellular = "cellular"
	netNone     = "none"
)

// networkReading: {"type", "name"} for the report, or nil when the kind is unknown.
// The Wi-Fi name is "" where the system hides it (macOS 14+ and Windows 11 24H2+ without
// location access).
func networkReading(kind, name string) map[string]any {
	if kind == "" {
		return nil
	}
	if kind != netWiFi || name == "<redacted>" {
		name = ""
	}
	return map[string]any{"type": kind, "name": strings.TrimSpace(name)}
}

// routeGetInterface: "en0" from macOS `route -n get default`.
func routeGetInterface(out string) string {
	for _, line := range strings.Split(out, "\n") {
		if k, v, ok := strings.Cut(strings.TrimSpace(line), ":"); ok && k == "interface" {
			return strings.TrimSpace(v)
		}
	}
	return ""
}

// isWiFiPort: whether macOS `networksetup -listallhardwareports` lists iface as Wi-Fi.
func isWiFiPort(ports, iface string) bool {
	port := ""
	for _, line := range strings.Split(ports, "\n") {
		k, v, ok := strings.Cut(strings.TrimSpace(line), ":")
		if !ok {
			continue
		}
		switch k {
		case "Hardware Port":
			port = strings.TrimSpace(v)
		case "Device":
			if strings.TrimSpace(v) == iface {
				return port == "Wi-Fi" || port == "AirPort"
			}
		}
	}
	return false
}

// lineValue: the value of the first "<key> : value" line, e.g. SSID from macOS
// `ipconfig getsummary` or Windows `netsh wlan show interfaces` (not BSSID: the key must
// match exactly).
func lineValue(out, key string) string {
	for _, line := range strings.Split(out, "\n") {
		if k, v, ok := strings.Cut(line, ":"); ok && strings.TrimSpace(k) == key {
			return strings.TrimSpace(v)
		}
	}
	return ""
}

// defaultRouteIface: the interface of the lowest-metric default route in Linux
// /proc/net/route ("" when there is none).
func defaultRouteIface(table string) string {
	best, bestMetric := "", -1
	for _, line := range strings.Split(table, "\n")[1:] {
		f := strings.Fields(line)
		if len(f) < 7 || f[1] != "00000000" {
			continue
		}
		flags, err1 := strconv.ParseUint(f[3], 16, 32)
		metric, err2 := strconv.Atoi(f[6])
		if err1 != nil || err2 != nil || flags&1 == 0 { // RTF_UP
			continue
		}
		if bestMetric < 0 || metric < bestMetric {
			best, bestMetric = f[0], metric
		}
	}
	return best
}
