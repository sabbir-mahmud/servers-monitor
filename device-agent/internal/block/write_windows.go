package block

import (
	"context"
	"os"
	"os/exec"
	"time"
)

// write overwrites the hosts file in place: replacing it by rename would drop the
// permissions Windows sets on it. The file is small enough that a torn write is unlikely,
// and the next check-in rewrites it anyway.
func write(path string, data []byte) error {
	return os.WriteFile(path, data, 0o644)
}

func flushDNS() {
	if os.Getenv("NEXGUARD_HOSTS") != "" {
		return // tests
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	_ = exec.CommandContext(ctx, "ipconfig", "/flushdns").Run()
}
