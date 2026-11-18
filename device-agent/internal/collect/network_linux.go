package collect

import (
	"os"
	"os/exec"
	"strings"
)

// network: the interface of the default route; Wi-Fi if it's a wireless device.
func network() map[string]any {
	table, err := os.ReadFile("/proc/net/route")
	if err != nil {
		return nil
	}
	iface := defaultRouteIface(string(table))
	if iface == "" {
		return networkReading(netNone, "")
	}
	sys := "/sys/class/net/" + iface
	switch {
	case exists(sys+"/wireless") || exists(sys+"/phy80211"):
		return networkReading(netWiFi, linuxSSID())
	case strings.HasPrefix(iface, "ww") || strings.HasPrefix(iface, "rmnet"):
		return networkReading(netCellular, "")
	case exists(sys + "/device"):
		return networkReading(netEthernet, "") // a physical NIC; tunnels and bridges have none
	}
	return nil
}

func linuxSSID() string {
	if _, err := exec.LookPath("iwgetid"); err == nil {
		if ssid := run("iwgetid", "-r"); ssid != "" {
			return ssid
		}
	}
	if _, err := exec.LookPath("nmcli"); err == nil {
		for _, line := range strings.Split(run("nmcli", "-t", "-f", "active,ssid", "dev", "wifi"), "\n") {
			if ssid, ok := strings.CutPrefix(line, "yes:"); ok {
				return ssid
			}
		}
	}
	return ""
}

func exists(path string) bool {
	_, err := os.Stat(path)
	return err == nil
}
