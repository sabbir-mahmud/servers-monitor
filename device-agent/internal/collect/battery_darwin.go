package collect

import "strings"

// batteryReading from `pmset -g batt`. (Battery libraries read IOKit keys that newer
// macOS moved, and report 0 of 0.) A Mac without a battery prints no percentage.
//
//	Now drawing from 'AC Power'
//	 -InternalBattery-0 (id=...)	79%; charging; 1:02 remaining present: true
func batteryReading() map[string]any {
	out := run("pmset", "-g", "batt")
	i := strings.Index(out, "%")
	if i < 0 {
		return nil
	}
	j := i
	for j > 0 && out[j-1] >= '0' && out[j-1] <= '9' {
		j--
	}
	pct, ok := firstNumber(out[j:i])
	if !ok || pct > 100 {
		return nil
	}
	plugged := strings.Contains(strings.SplitN(out, "\n", 2)[0], "AC Power")
	return map[string]any{"percent": int(pct), "charging": plugged}
}
