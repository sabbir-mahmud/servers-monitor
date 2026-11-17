package collect

import (
	"os/exec"
	"path/filepath"
)

// gpuPercent: AMD (and some Intel) expose gpu_busy_percent in sysfs; NVIDIA needs nvidia-smi.
func gpuPercent() (float64, bool) {
	paths, _ := filepath.Glob("/sys/class/drm/card*/device/gpu_busy_percent")
	for _, p := range paths {
		if v, ok := firstNumber(readFirst(p)); ok {
			return v, true
		}
	}
	if _, err := exec.LookPath("nvidia-smi"); err == nil {
		return firstNumber(run("nvidia-smi", "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits"))
	}
	return 0, false
}

func windowsModel() string { return "" }
