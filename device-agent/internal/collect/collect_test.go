package collect

import "testing"

// A real reading from the machine running the tests: every value present must be sane.
func TestReportFromThisMachine(t *testing.T) {
	r := Report("test")
	if r["platform"] != Platform() || r["app_version"] != "nexguard-agent test" {
		t.Fatalf("info: %v", r)
	}
	cpu, ok := r["cpu"].(map[string]any)
	if !ok {
		t.Fatalf("no CPU reading: %v", r)
	}
	if p := cpu["percent"].(float64); p < 0 || p > 100 {
		t.Fatalf("cpu %v", p)
	}
	m := r["memory"].(map[string]any)
	if m["used"].(uint64) > m["total"].(uint64) || m["total"].(uint64) == 0 {
		t.Fatalf("memory %v", m)
	}
	s := r["storage"].(map[string]any)
	if s["free"].(uint64) > s["total"].(uint64) {
		t.Fatalf("storage %v", s)
	}
	if b, ok := r["battery"].(map[string]any); ok {
		if p := b["percent"].(int); p < 0 || p > 100 {
			t.Fatalf("battery %v", b)
		}
	}
	t.Logf("report: %v", r)
}

func TestFirstNumber(t *testing.T) {
	for in, want := range map[string]float64{`=18,"x"=3`: 18, " 42.5 %": 42.5, "7": 7} {
		if got, ok := firstNumber(in); !ok || got != want {
			t.Errorf("firstNumber(%q) = %v %v", in, got, ok)
		}
	}
	if _, ok := firstNumber("none"); ok {
		t.Error("no number should be false")
	}
}

func TestNetworkFromThisMachine(t *testing.T) {
	n, ok := Report("test")["network"].(map[string]any)
	if !ok {
		t.Skip("network kind unknown here (VPN or virtual adapter)")
	}
	switch n["type"] {
	case netWiFi, netEthernet, netCellular, netNone:
	default:
		t.Fatalf("network %v", n)
	}
	if n["type"] != netWiFi && n["name"] != "" {
		t.Fatalf("only Wi-Fi has a name: %v", n)
	}
}
