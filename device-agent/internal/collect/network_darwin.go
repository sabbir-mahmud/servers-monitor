package collect

import "regexp"

var macWired = regexp.MustCompile(`^(en|bridge)[0-9]+$`)

// network: the interface carrying the default route, Wi-Fi or wired.
func network() map[string]any {
	iface := routeGetInterface(run("route", "-n", "get", "default"))
	switch {
	case iface == "":
		return networkReading(netNone, "")
	case isWiFiPort(run("networksetup", "-listallhardwareports"), iface):
		return networkReading(netWiFi, lineValue(run("ipconfig", "getsummary", iface), "SSID"))
	case macWired.MatchString(iface):
		return networkReading(netEthernet, "")
	}
	return nil // utun (VPN) and the like
}
