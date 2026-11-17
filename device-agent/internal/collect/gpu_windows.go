package collect

import (
	"strings"

	"golang.org/x/sys/windows/registry"
)

// gpuPercent: 3D engine use summed over processes, as Task Manager shows it. The counter
// name is localized, so on a Windows that isn't in English this reports nothing.
func gpuPercent() (float64, bool) {
	out := run("powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
		`((Get-Counter '\GPU Engine(*engtype_3D)\Utilization Percentage' -ErrorAction Stop).CounterSamples | Measure-Object -Property CookedValue -Sum).Sum`)
	v, ok := firstNumber(strings.ReplaceAll(out, ",", "."))
	if !ok {
		return 0, false
	}
	return min(v, 100), true
}

// windowsModel: maker and model from the firmware (e.g. "LENOVO 20XW"), as Settings shows it.
func windowsModel() string {
	k, err := registry.OpenKey(registry.LOCAL_MACHINE, `HARDWARE\DESCRIPTION\System\BIOS`, registry.QUERY_VALUE)
	if err != nil {
		return ""
	}
	defer k.Close()
	maker, _, _ := k.GetStringValue("SystemManufacturer")
	product, _, _ := k.GetStringValue("SystemProductName")
	switch product {
	case "", "To Be Filled By O.E.M.", "System Product Name", "Default string":
		return ""
	}
	if maker == "" || strings.Contains(product, maker) {
		return product
	}
	return maker + " " + product
}
