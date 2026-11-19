package svc

import (
	"encoding/xml"
	"strings"
	"testing"
)

const oddDir = `/Users/o'neil "x"/Library/Application Support/NexGuard & 100%`

func TestLaunchAgentPlistIsValidXMLWithTheArguments(t *testing.T) {
	p := launchAgentPlist(oddDir+"/nexguard", oddDir)
	dec := xml.NewDecoder(strings.NewReader(p))
	dec.Strict = false // the DOCTYPE
	var strs []string
	for {
		tok, err := dec.Token()
		if err != nil {
			break
		}
		if se, ok := tok.(xml.StartElement); ok && se.Name.Local == "string" {
			var s string
			_ = dec.DecodeElement(&s, &se)
			strs = append(strs, s)
		}
	}
	want := []string{userLabel, oddDir + "/nexguard", "--user", "--dir", oddDir, "run"}
	if len(strs) < len(want) {
		t.Fatalf("strings %q", strs)
	}
	for i, w := range want {
		if strs[i] != w {
			t.Fatalf("string %d = %q, want %q", i, strs[i], w)
		}
	}
}

func TestSystemdUnitQuotesAndEscapesSpecifiers(t *testing.T) {
	u := userUnitFile("/home/a b/.local/share/nexguard/nexguard", `/home/a b/50%$x"`)
	if !strings.Contains(u, `ExecStart="/home/a b/.local/share/nexguard/nexguard" --user --dir "/home/a b/50%%$$x\"" run`) {
		t.Fatal(u)
	}
}

func TestAutostartEntryQuoting(t *testing.T) {
	a := autostartFile("/home/a b/nexguard", `/home/$x`)
	if !strings.Contains(a, `Exec="/home/a b/nexguard" --user --dir "/home/\\$x" run`) {
		t.Fatal(a)
	}
}

func TestWindowsTaskScriptQuotesPaths(t *testing.T) {
	s := userTaskScript(`C:\Users\o'neil\AppData\Local\NexGuard\nexguard.exe`, `C:\Users\o'neil\AppData\Local\NexGuard`, 42)
	for _, want := range []string{
		`$exe = 'C:\Users\o''neil\AppData\Local\NexGuard\nexguard.exe'`,
		`$agentArgs = '--user --dir "' + 'C:\Users\o''neil\AppData\Local\NexGuard' + '" run'`,
		`$_.Id -ne 42`,
		`-TaskName $task`, "$task = 'NexGuard agent'",
	} {
		if !strings.Contains(s, want) {
			t.Errorf("missing %s in\n%s", want, s)
		}
	}
	if strings.Contains(s, "`") {
		t.Error("no backtick continuations: one command per line")
	}
}
