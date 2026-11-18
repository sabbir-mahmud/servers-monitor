package collect

import (
	"strings"
	"unsafe"

	"golang.org/x/sys/windows"
)

// Interface types beyond the ones x/sys names (ipifcons.h).
const (
	ifTypeWWANPP  = 243
	ifTypeWWANPP2 = 244
)

// network: the connected adapter with a gateway and the lowest IPv4 metric, which is the
// one Windows sends internet traffic through.
func network() map[string]any {
	adapters, err := adapterAddresses()
	if err != nil {
		return nil
	}
	var best *windows.IpAdapterAddresses
	for a := adapters; a != nil; a = a.Next {
		if a.OperStatus != windows.IfOperStatusUp || a.FirstGatewayAddress == nil {
			continue
		}
		if best == nil || a.Ipv4Metric < best.Ipv4Metric {
			best = a
		}
	}
	if best == nil {
		return networkReading(netNone, "")
	}
	switch best.IfType {
	case windows.IF_TYPE_IEEE80211:
		return networkReading(netWiFi, lineValue(run("netsh", "wlan", "show", "interfaces"), "SSID"))
	case ifTypeWWANPP, ifTypeWWANPP2:
		return networkReading(netCellular, "")
	case windows.IF_TYPE_ETHERNET_CSMACD:
		desc := strings.ToLower(windows.UTF16PtrToString(best.Description))
		if strings.Contains(desc, "virtual") || strings.Contains(desc, "vpn") || strings.Contains(desc, "tap-") {
			return nil // Hyper-V switch, VPN client: says nothing about the real connection
		}
		return networkReading(netEthernet, "")
	}
	return nil
}

func adapterAddresses() (*windows.IpAdapterAddresses, error) {
	size := uint32(15 * 1024)
	for range 3 {
		buf := make([]byte, size)
		first := (*windows.IpAdapterAddresses)(unsafe.Pointer(&buf[0]))
		err := windows.GetAdaptersAddresses(windows.AF_INET, windows.GAA_FLAG_INCLUDE_GATEWAYS, 0, first, &size)
		if err == nil {
			return first, nil
		}
		if err != windows.ERROR_BUFFER_OVERFLOW {
			return nil, err
		}
	}
	return nil, windows.ERROR_BUFFER_OVERFLOW
}
