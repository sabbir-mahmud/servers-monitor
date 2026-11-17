//go:build windows

package svc

import (
	"os"
	"os/exec"
	"path/filepath"
	"syscall"

	"golang.org/x/sys/windows"
)

func isAdmin() bool { return windows.GetCurrentProcessToken().IsElevated() }

// replace: a running .exe can't be overwritten, but it can be renamed out of the way.
func replace(tmp, dst string) error {
	_ = os.Remove(dst + ".old")
	if _, err := os.Stat(dst); err == nil {
		if err := os.Rename(dst, dst+".old"); err != nil {
			return err
		}
	}
	return os.Rename(tmp, dst)
}

// removeBinary: if this is the installed copy, it can't delete itself while running, so a
// detached cmd deletes it (and the folder) a few seconds after we exit.
func removeBinary(path string) {
	_ = os.Remove(path + ".old")
	if os.Remove(path) == nil {
		_ = os.Remove(filepath.Dir(path))
		return
	}
	cmd := exec.Command("cmd.exe", "/c", "ping -n 4 127.0.0.1 >nul & del /f /q \""+path+"\" & rmdir \""+filepath.Dir(path)+"\"")
	cmd.SysProcAttr = &syscall.SysProcAttr{HideWindow: true, CreationFlags: windows.DETACHED_PROCESS}
	_ = cmd.Start()
}
