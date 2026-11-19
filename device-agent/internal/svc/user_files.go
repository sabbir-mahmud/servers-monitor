package svc

import (
	"bytes"
	"encoding/xml"
	"path/filepath"
	"strconv"
	"strings"
)

// The files a per-person install writes, one generator per system. Kept free of build
// tags so the tests check every system's quoting on any machine.

func xmlText(s string) string {
	var b bytes.Buffer
	_ = xml.EscapeText(&b, []byte(s))
	return b.String()
}

// launchAgentPlist (macOS): started at sign-in, restarted if it stops (once a minute at most).
func launchAgentPlist(exe, dir string) string {
	args := ""
	for _, a := range []string{exe, "--user", "--dir", dir, "run"} {
		args += "<string>" + xmlText(a) + "</string>"
	}
	return `<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>` + userLabel + `</string>
  <key>ProgramArguments</key>
  <array>` + args + `</array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>60</integer>
  <key>ProcessType</key><string>Background</string>
  <key>StandardOutPath</key><string>/dev/null</string>
  <key>StandardErrorPath</key><string>` + xmlText(filepath.Join(dir, "agent.log")) + `</string>
</dict>
</plist>
`
}

// systemdQuote: one ExecStart argument, quoted, with systemd's specifiers escaped.
func systemdQuote(s string) string {
	s = strings.NewReplacer(`\`, `\\`, `"`, `\"`, "%", "%%", "$", "$$").Replace(s)
	return `"` + s + `"`
}

// userUnitFile (Linux, systemd --user).
func userUnitFile(exe, dir string) string {
	return "[Unit]\nDescription=NexGuard agent (as you, no administrator rights)\n\n" +
		"[Service]\nExecStart=" + systemdQuote(exe) + " --user --dir " + systemdQuote(dir) + " run\n" +
		"Restart=always\nRestartSec=60\n\n[Install]\nWantedBy=default.target\n"
}

// desktopQuote: one Exec argument in a .desktop file (the spec's quoting, then its string
// escapes on top).
func desktopQuote(s string) string {
	s = strings.NewReplacer(`\`, `\\\\`, `"`, `\\"`, "`", "\\\\`", "$", `\\$`, "%", "%%").Replace(s)
	return `"` + s + `"`
}

// autostartFile (Linux without a systemd user session): starts with the desktop session.
func autostartFile(exe, dir string) string {
	return "[Desktop Entry]\nType=Application\nName=NexGuard agent\n" +
		"Exec=" + desktopQuote(exe) + " --user --dir " + desktopQuote(dir) + " run\n" +
		"NoDisplay=true\nX-GNOME-Autostart-enabled=true\n"
}

// psQuote: a PowerShell single-quoted string.
func psQuote(s string) string { return "'" + strings.ReplaceAll(s, "'", "''") + "'" }

// userTaskScript (Windows): a task that runs the agent at sign-in with no window (through
// conhost --headless where Windows has it), restarts it if it stops, and starts it now.
// The same task the dashboard's install.ps1 makes. self is this process, never stopped.
func userTaskScript(exe, dir string, self int) string {
	lines := []string{
		"$ErrorActionPreference = 'Stop'",
		"$exe = " + psQuote(exe),
		"$task = " + psQuote(userTask),
		"Stop-ScheduledTask -TaskName $task -ErrorAction SilentlyContinue",
		"Get-Process -Name nexguard -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq $exe -and $_.Id -ne " +
			strconv.Itoa(self) + " } | Stop-Process -Force -ErrorAction SilentlyContinue",
		"$agentArgs = '--user --dir \"' + " + psQuote(dir) + " + '\" run'",
		"$conhost = Join-Path $env:windir 'System32\\conhost.exe'",
		"if ([Environment]::OSVersion.Version.Build -ge 17763 -and (Test-Path $conhost)) { " +
			"$action = New-ScheduledTaskAction -Execute $conhost -Argument ('--headless \"' + $exe + '\" ' + $agentArgs) } " +
			"else { $action = New-ScheduledTaskAction -Execute $exe -Argument $agentArgs }",
		"$user = \"$env:USERDOMAIN\\$env:USERNAME\"",
		"$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries " +
			"-StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) " +
			"-RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)",
		"$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited",
		"Register-ScheduledTask -TaskName $task -Action $action -Trigger (New-ScheduledTaskTrigger -AtLogOn -User $user) " +
			"-Settings $settings -Principal $principal " +
			"-Description 'NexGuard agent: health, and screen time and websites if turned on. Runs as you.' -Force | Out-Null",
		"Start-ScheduledTask -TaskName $task",
	}
	return strings.Join(lines, "\n") + "\n"
}
