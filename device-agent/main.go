// nexguard: the NexGuard agent for Windows, Mac and Linux computers.
//
// An administrator installs it once; it then runs as a system service (a Windows service,
// a macOS LaunchDaemon or a systemd unit) and checks in with the dashboard. The person using
// the computer can't stop or remove it without administrator rights.
//
//	nexguard pair <dashboard> <code>   show who will see this computer, pair on a yes
//	nexguard install                   copy itself into place and start the service
//	nexguard status                    paired? last check-in?
//	nexguard checkin                   check in once now
//	nexguard uninstall                 stop the service and remove everything
//	nexguard run                       the service itself (foreground)
package main

import (
	"bufio"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"runtime"
	"strings"
	"time"

	"github.com/sabbir-mahmud/servers-monitor/device-agent/internal/activity"
	"github.com/sabbir-mahmud/servers-monitor/device-agent/internal/agent"
	"github.com/sabbir-mahmud/servers-monitor/device-agent/internal/api"
	"github.com/sabbir-mahmud/servers-monitor/device-agent/internal/block"
	"github.com/sabbir-mahmud/servers-monitor/device-agent/internal/collect"
	"github.com/sabbir-mahmud/servers-monitor/device-agent/internal/config"
	"github.com/sabbir-mahmud/servers-monitor/device-agent/internal/svc"
)

// Version is set at build time: -ldflags "-X main.Version=1.0.0".
var Version = "dev"

const usage = `NexGuard agent %s: reports this computer's health to your NexGuard dashboard.

Usage (as an administrator):
  nexguard pair [--yes] <dashboard address> <pairing code>
  nexguard install      start the background service (after pairing)
  nexguard status       show whether it's paired and when it last checked in
  nexguard checkin      check in once now
  nexguard uninstall    stop the service and remove the agent's files
  nexguard version
`

func main() {
	if len(os.Args) < 2 {
		if svc.Interactive() {
			fmt.Fprintf(os.Stderr, usage, Version)
			os.Exit(2)
		}
		os.Args = append(os.Args, "run") // started by the service manager
	}
	var err error
	switch cmd, args := os.Args[1], os.Args[2:]; cmd {
	case "pair":
		err = pair(args)
	case "install":
		err = svc.Install(Version)
	case "uninstall":
		err = svc.Uninstall(agent.Forget)
	case "status":
		err = status()
	case "checkin":
		err = checkin()
	case "run":
		err = svc.Run(Version, agent.Run)
	case "sample": // Windows: started by the service as the signed-in person
		err = activity.PrintSample()
	case "version", "--version", "-v":
		fmt.Println(Version)
	case "help", "--help", "-h":
		fmt.Printf(usage, Version)
	default:
		fmt.Fprintf(os.Stderr, usage, Version)
		os.Exit(2)
	}
	if err != nil {
		fmt.Fprintln(os.Stderr, "Error:", err)
		os.Exit(1)
	}
}

func pair(args []string) error {
	fs := flag.NewFlagSet("pair", flag.ExitOnError)
	yes := fs.Bool("yes", false, "agree without asking (the person was told some other way)")
	_ = fs.Parse(args)
	if fs.NArg() != 2 {
		return errors.New("usage: nexguard pair [--yes] <dashboard address> <pairing code>")
	}
	server, err := api.CheckServer(fs.Arg(0))
	if err != nil {
		return err
	}
	code := fs.Arg(1)
	client := api.New(server, "nexguard-agent/"+Version)
	info := collect.Info(Version)

	// Ask first: a preview doesn't use the code, so saying no changes nothing.
	preview, err := client.Pair(code, info, true)
	if err != nil {
		return err
	}
	fmt.Printf("\n  %s\n\n", preview.Disclosure)
	if !*yes && !ask("Allow this? Type y and press Enter: ") {
		return errors.New("not paired: you said no. The code still works if you change your mind")
	}
	result, err := client.Pair(code, info, false)
	if err != nil {
		return err
	}
	cfg := &config.Config{Server: server, Token: result.Token, Interval: result.CheckinSeconds,
		Name: result.Device.Name, Account: result.Device.Account}
	if err := config.Save(cfg); err != nil {
		return err
	}
	fmt.Printf("Paired as %q in %s.\n", result.Device.Name, result.Device.Account)
	if err := agent.CheckinOnce(cfg, Version); err != nil {
		fmt.Printf("The first check-in failed (%v); the service will retry.\n", err)
	}
	return nil
}

func status() error {
	cfg, err := config.Load()
	if errors.Is(err, config.ErrNotPaired) {
		fmt.Println("Not paired. Run: nexguard pair <dashboard address> <code>")
	} else if err != nil {
		return err
	} else {
		fmt.Printf("Paired as %q in %s, with %s (every %s).\n", cfg.Name, cfg.Account, cfg.Server,
			time.Duration(cfg.Interval)*time.Second)
	}
	fmt.Println("Service:", svc.State())
	c := activity.CurrentControls()
	fmt.Println("Screen time:", onOff(c.ScreenTime))
	fmt.Println("Websites visited:", onOff(c.WebActivity))
	if n := block.Count(); n > 0 {
		fmt.Printf("Blocking: %d sites in %s\n", n, block.Path())
	} else {
		fmt.Println("Blocking: off")
	}
	if s, err := agent.ReadStatus(); err == nil {
		if s.OK {
			fmt.Printf("Last check-in: %s, OK\n", s.At.Local().Format("Mon 2 Jan 15:04"))
		} else {
			fmt.Printf("Last check-in: %s, failed: %s\n", s.At.Local().Format("Mon 2 Jan 15:04"), s.Error)
		}
	}
	return nil
}

func checkin() error {
	cfg, err := config.Load()
	if err != nil {
		return err
	}
	if err := agent.CheckinOnce(cfg, Version); err != nil {
		return err
	}
	fmt.Println("Checked in.")
	return nil
}

func onOff(on bool) string {
	if on {
		return "on"
	}
	return "off"
}

// ask reads the answer from the terminal, even when the installer was piped into a shell.
func ask(prompt string) bool {
	fmt.Print(prompt)
	var in io.Reader = os.Stdin
	name := "/dev/tty"
	if runtime.GOOS == "windows" {
		name = "CONIN$"
	}
	if tty, err := os.Open(name); err == nil {
		defer tty.Close()
		in = tty
	}
	line, _ := bufio.NewReader(in).ReadString('\n')
	answer := strings.ToLower(strings.TrimSpace(line))
	return answer == "y" || answer == "yes"
}
