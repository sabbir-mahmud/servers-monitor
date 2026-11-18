package collect

import "testing"

func TestNetworkReading(t *testing.T) {
	if networkReading("", "x") != nil {
		t.Error("unknown kind must be left out")
	}
	if n := networkReading(netWiFi, "<redacted>"); n["name"] != "" {
		t.Errorf("hidden SSID: %v", n)
	}
	if n := networkReading(netEthernet, "Home"); n["name"] != "" {
		t.Errorf("only Wi-Fi has a name: %v", n)
	}
	if n := networkReading(netWiFi, " Home "); n["type"] != "wifi" || n["name"] != "Home" {
		t.Errorf("wifi: %v", n)
	}
}

func TestMacParsers(t *testing.T) {
	route := "   route to: default\ndestination: default\n  interface: en0\n      flags: <UP,GATEWAY>\n"
	if got := routeGetInterface(route); got != "en0" {
		t.Errorf("routeGetInterface = %q", got)
	}
	ports := "Hardware Port: Thunderbolt Bridge\nDevice: bridge0\n\nHardware Port: Wi-Fi\nDevice: en0\nEthernet Address: 10:b5\n"
	if !isWiFiPort(ports, "en0") || isWiFiPort(ports, "bridge0") || isWiFiPort(ports, "en7") {
		t.Error("isWiFiPort")
	}
	summary := "<dictionary> {\n  BSSID : aa:bb\n  InterfaceType : WiFi\n  SSID : Home Net\n}"
	if got := lineValue(summary, "SSID"); got != "Home Net" {
		t.Errorf("SSID = %q", got)
	}
}

func TestNetshSSID(t *testing.T) {
	out := "\r\n    Name                   : Wi-Fi\r\n    State                  : connected\r\n" +
		"    SSID                   : Office\r\n    AP BSSID               : aa:bb:cc\r\n"
	if got := lineValue(out, "SSID"); got != "Office" {
		t.Errorf("SSID = %q", got)
	}
	if got := lineValue("    State : disconnected\r\n", "SSID"); got != "" {
		t.Errorf("no SSID line = %q", got)
	}
}

func TestDefaultRouteIface(t *testing.T) {
	table := "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\n" +
		"eth0\t00000000\t0101A8C0\t0003\t0\t0\t100\t00000000\n" +
		"wlan0\t00000000\t0101A8C0\t0003\t0\t0\t600\t00000000\n" +
		"wlan0\t0001A8C0\t00000000\t0001\t0\t0\t600\t00FFFFFF\n"
	if got := defaultRouteIface(table); got != "eth0" {
		t.Errorf("lowest metric = %q", got)
	}
	if got := defaultRouteIface("Iface\tDestination\n"); got != "" {
		t.Errorf("no default route = %q", got)
	}
	down := "Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\neth0\t00000000\t01\t0002\t0\t0\t1\n"
	if got := defaultRouteIface(down); got != "" {
		t.Errorf("route not up = %q", got)
	}
}
