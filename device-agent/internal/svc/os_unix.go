//go:build !windows

package svc

import "os"

func isAdmin() bool { return os.Geteuid() == 0 }

func replace(tmp, dst string) error { return os.Rename(tmp, dst) }

func removeBinary(path string) { _ = os.Remove(path) } // a running binary can be unlinked
