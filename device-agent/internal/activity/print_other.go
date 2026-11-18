//go:build !windows

package activity

import "errors"

// PrintSample is only used on Windows (see screen_windows.go).
func PrintSample() error {
	return errors.New("`sample` is only used on Windows")
}
