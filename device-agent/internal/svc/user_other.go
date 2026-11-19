//go:build !darwin && !linux && !windows

package svc

import "errors"

func registerUser(exe, dir string) error {
	return errors.New("this system has no per-person install: use the administrator install")
}
