package relay_test

import (
	"strings"
	"testing"

	"github.com/sabbir-mahmud/servers-monitor/device-agent/internal/relay"
	"github.com/sabbir-mahmud/servers-monitor/device-agent/internal/relay/relaytest"
)

func TestPlainRedisOnlyOnThisMachineOrTheLAN(t *testing.T) {
	for _, ok := range []string{"rediss://u:p@relay.example.com:6379/?prefix=sm",
		"redis://u:p@127.0.0.1:6379", "redis://u:p@192.168.1.20", "redis://u:p@nas.local"} {
		if _, err := relay.Parse(ok); err != nil {
			t.Errorf("%s refused: %v", ok, err)
		}
	}
	for _, bad := range []string{"redis://u:p@relay.example.com:6379", "https://m.example.com", "nonsense"} {
		if _, err := relay.Parse(bad); err == nil {
			t.Errorf("%s should be refused", bad)
		}
	}
	t.Setenv("NEXGUARD_ALLOW_PLAIN", "1")
	if _, err := relay.Parse("redis://u:p@relay.example.com:6379"); err != nil {
		t.Error(err)
	}
}

func TestAddressPartsAndRedaction(t *testing.T) {
	a, err := relay.Parse("rediss://sm-pc:s%2Fecret@relay.example.com:6380/?prefix=sm")
	if err != nil {
		t.Fatal(err)
	}
	if a.User != "sm-pc" || a.Password != "s/ecret" || a.Prefix != "sm" || a.Host != "relay.example.com:6380" {
		t.Fatalf("%+v", a)
	}
	if r := a.Redacted(); strings.Contains(r, "ecret") || r != "rediss://relay.example.com:6380" {
		t.Fatalf("redacted: %s", r)
	}
}

func TestCommandsAndRefusals(t *testing.T) {
	srv := relaytest.New(t)
	srv.AddUser("dev", "pw", "mine:")
	url := srv.URL("dev")
	c, err := relay.Dial(url)
	if err != nil {
		t.Fatal(err)
	}
	defer c.Close()
	if err := c.Push("mine:in", "hello\r\nworld"); err != nil {
		t.Fatal(err)
	}
	if got := srv.Pop("mine:in"); got != "hello\r\nworld" {
		t.Fatalf("pushed %q", got)
	}
	if _, ok, err := c.Get("mine:cfg"); ok || err != nil {
		t.Fatalf("missing key: %v %v", ok, err)
	}
	srv.Set("mine:cfg", strings.Repeat("x", 100000))
	if v, ok, err := c.Get("mine:cfg"); !ok || err != nil || len(v) != 100000 {
		t.Fatalf("big value: %d %v %v", len(v), ok, err)
	}
	if err := c.Push("theirs:in", "x"); !relay.Denied(err) {
		t.Fatalf("another device's key must be refused: %v", err)
	}
	srv.RemoveUser("dev")
	if _, err := relay.Dial(url); !relay.Denied(err) {
		t.Fatalf("a removed user must be refused: %v", err)
	}
}
