package activity

import (
	"encoding/json"
	"os"
	"path/filepath"
	"time"

	"github.com/sabbir-mahmud/servers-monitor/device-agent/internal/config"
)

// Access is what the running agent last found about reading browsers: the ones the system
// refuses (macOS until the agent has Full Disk Access). The installer waits on this file
// while the person switches access on; it has to come from the running agent, because
// macOS decides per program, not per user.
type Access struct {
	At       time.Time `json:"at"`
	NoAccess []string  `json:"no_access"`
}

func AccessPath() string { return filepath.Join(config.Dir(), "access.json") }

// CheckAccess looks into each browser's folder (reading nothing), records the browsers
// refused, and returns them.
func CheckAccess() []string {
	_, denied := historyFiles()
	setNoAccess(denied)
	names := NoAccess()
	if data, err := json.Marshal(Access{At: time.Now(), NoAccess: names}); err == nil {
		_ = os.WriteFile(AccessPath(), data, 0o644) // no secrets: the installer reads it
	}
	return names
}

func ReadAccess() (*Access, error) {
	data, err := os.ReadFile(AccessPath())
	if err != nil {
		return nil, err
	}
	var a Access
	return &a, json.Unmarshal(data, &a)
}
