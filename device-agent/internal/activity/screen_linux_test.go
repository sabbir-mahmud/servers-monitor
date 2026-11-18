package activity

import "testing"

func TestLinuxSessions(t *testing.T) {
	desk := "Name=sara\nType=wayland\nActive=yes\nRemote=no\nClass=user\nIdleHint=no\nLockedHint=no\n"
	if u, ok := sessionUse(desk); !ok || u.User != "sara" || !u.Active() {
		t.Fatalf("desktop: %+v %v", u, ok)
	}
	if u, _ := sessionUse(desk + "LockedHint=yes\n"); u.Active() {
		t.Error("locked must not count")
	}
	for _, other := range []string{
		"Name=sara\nType=tty\nActive=yes\nRemote=no\nClass=user\n",
		"Name=sara\nType=x11\nActive=yes\nRemote=yes\nClass=user\n",
		"Name=gdm\nType=wayland\nActive=yes\nRemote=no\nClass=greeter\n",
	} {
		if _, ok := sessionUse(other); ok {
			t.Errorf("not a person at the screen: %q", other)
		}
	}
}
