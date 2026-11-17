package collect

import "strings"

// gpuPercent: the GPU's own "Device Utilization %" (Apple silicon, and most Intel/AMD Macs).
func gpuPercent() (float64, bool) {
	out := run("ioreg", "-r", "-d", "1", "-c", "IOAccelerator")
	i := strings.Index(out, `"Device Utilization %"=`)
	if i < 0 {
		return 0, false
	}
	return firstNumber(out[i+len(`"Device Utilization %"=`):])
}

func windowsModel() string { return "" }
