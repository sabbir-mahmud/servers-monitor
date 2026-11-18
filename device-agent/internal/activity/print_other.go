//go:build !windows

package activity

import "errors"

// PrintSample is only used on Windows (see screen_windows.go).
func PrintSample() error {
	return errors.New("`sample` is only used on Windows")
}

// PrintLocation is only used on Windows (see location_windows.go).
func PrintLocation() error {
	return errors.New("`locate` is only used on Windows")
}
