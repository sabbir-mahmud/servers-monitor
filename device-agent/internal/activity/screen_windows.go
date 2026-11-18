package activity

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"os/user"
	"strings"
	"syscall"
	"time"
	"unsafe"

	"golang.org/x/sys/windows"

	"github.com/sabbir-mahmud/servers-monitor/device-agent/internal/config"
)

// The service runs in session 0, which can't see the signed-in person's screen. Each
// sample starts this same program as that person, on their desktop, with `sample`: it
// prints one JSON line (user, idle time, app in front) and exits.

var (
	user32                 = windows.NewLazySystemDLL("user32.dll")
	kernel32               = windows.NewLazySystemDLL("kernel32.dll")
	procGetForegroundWnd   = user32.NewProc("GetForegroundWindow")
	procGetWindowThreadPID = user32.NewProc("GetWindowThreadProcessId")
	procGetLastInputInfo   = user32.NewProc("GetLastInputInfo")
	procGetTickCount       = kernel32.NewProc("GetTickCount")
)

const noSession = 0xFFFFFFFF

type helperOutput struct {
	User   string `json:"user"`
	App    string `json:"app"`
	IdleMS uint32 `json:"idle_ms"`
	Locked bool   `json:"locked"`
}

func sample() (Use, error) {
	if config.UserMode() {
		// Already running in the person's session: look directly. Only while it's the
		// session at the screen (not one switched away from).
		var mine uint32
		if err := windows.ProcessIdToSessionId(windows.GetCurrentProcessId(), &mine); err == nil &&
			mine != windows.WTSGetActiveConsoleSessionId() {
			return Use{}, errors.New("this session isn't at the screen")
		}
		h := look()
		return Use{User: h.User, App: h.App, Idle: time.Duration(h.IdleMS) * time.Millisecond, Locked: h.Locked}, nil
	}
	session := windows.WTSGetActiveConsoleSessionId()
	if session == noSession {
		return Use{}, errors.New("no console session")
	}
	var token windows.Token
	if err := windows.WTSQueryUserToken(session, &token); err != nil {
		return Use{}, fmt.Errorf("nobody signed in: %w", err) // also: not running as SYSTEM
	}
	defer token.Close()
	exe, err := os.Executable()
	if err != nil {
		return Use{}, err
	}
	out, err := runAsUser(token, exe, "sample", 10*time.Second)
	if err != nil {
		return Use{}, err
	}
	var h helperOutput
	if err := json.Unmarshal(out, &h); err != nil {
		return Use{}, fmt.Errorf("sample helper: %w", err)
	}
	return Use{User: h.User, App: h.App, Idle: time.Duration(h.IdleMS) * time.Millisecond, Locked: h.Locked}, nil
}

// runAsUser starts exe on the user's interactive desktop, hidden, and returns its output.
func runAsUser(token windows.Token, exe string, arg string, limit time.Duration) ([]byte, error) {
	sa := &windows.SecurityAttributes{InheritHandle: 1}
	sa.Length = uint32(unsafe.Sizeof(*sa))
	var r, w windows.Handle
	if err := windows.CreatePipe(&r, &w, sa, 0); err != nil {
		return nil, err
	}
	reader := os.NewFile(uintptr(r), "sample-out")
	defer reader.Close()
	_ = windows.SetHandleInformation(r, windows.HANDLE_FLAG_INHERIT, 0)

	desktop, _ := windows.UTF16PtrFromString(`winsta0\default`)
	cmdline, _ := windows.UTF16PtrFromString(windows.EscapeArg(exe) + " " + arg)
	si := &windows.StartupInfo{
		Desktop:    desktop,
		Flags:      windows.STARTF_USESTDHANDLES | windows.STARTF_USESHOWWINDOW,
		ShowWindow: windows.SW_HIDE,
		StdOutput:  w,
		StdErr:     w,
	}
	si.Cb = uint32(unsafe.Sizeof(*si))
	var pi windows.ProcessInformation
	err := windows.CreateProcessAsUser(token, nil, cmdline, nil, nil, true,
		windows.CREATE_NO_WINDOW, nil, nil, si, &pi)
	windows.CloseHandle(w) // ours; the child has its own copy
	if err != nil {
		return nil, fmt.Errorf("start sample helper: %w", err)
	}
	defer windows.CloseHandle(pi.Thread)
	defer windows.CloseHandle(pi.Process)

	done := make(chan []byte, 1)
	go func() {
		data, _ := io.ReadAll(io.LimitReader(reader, 64*1024))
		done <- data
	}()
	if ev, _ := windows.WaitForSingleObject(pi.Process, uint32(limit/time.Millisecond)); ev != windows.WAIT_OBJECT_0 {
		_ = windows.TerminateProcess(pi.Process, 1)
		return nil, errors.New("sample helper timed out")
	}
	select {
	case data := <-done:
		return data, nil
	case <-time.After(2 * time.Second):
		return nil, errors.New("sample helper sent nothing")
	}
}

// PrintSample is `nexguard sample`, run as the signed-in person by the service.
func PrintSample() error {
	return json.NewEncoder(os.Stdout).Encode(look())
}

// look at this session's screen: who, what's in front, how long since input.
func look() helperOutput {
	h := helperOutput{IdleMS: idleMS()}
	if u, err := user.Current(); err == nil {
		h.User = u.Username
		if i := strings.LastIndex(h.User, `\`); i >= 0 {
			h.User = h.User[i+1:] // "PC\sara" -> "sara"
		}
	}
	h.App = foregroundExe()
	base := strings.ToLower(h.App[strings.LastIndexAny(h.App, `\/`)+1:])
	h.Locked = h.App == "" || base == "lockapp.exe" || base == "logonui.exe"
	return h
}

func idleMS() uint32 {
	var info struct {
		Size uint32
		Time uint32
	}
	info.Size = uint32(unsafe.Sizeof(info))
	if ok, _, _ := procGetLastInputInfo.Call(uintptr(unsafe.Pointer(&info))); ok == 0 {
		return uint32(IdleAfter / time.Millisecond)
	}
	now, _, _ := procGetTickCount.Call()
	return uint32(now) - info.Time // wraps correctly every ~49 days
}

func foregroundExe() string {
	hwnd, _, _ := procGetForegroundWnd.Call()
	if hwnd == 0 {
		return ""
	}
	var pid uint32
	procGetWindowThreadPID.Call(hwnd, uintptr(unsafe.Pointer(&pid)))
	if pid == 0 {
		return ""
	}
	h, err := windows.OpenProcess(windows.PROCESS_QUERY_LIMITED_INFORMATION, false, pid)
	if err != nil {
		return ""
	}
	defer windows.CloseHandle(h)
	buf := make([]uint16, syscall.MAX_PATH*2)
	size := uint32(len(buf))
	if err := windows.QueryFullProcessImageName(h, 0, &buf[0], &size); err != nil {
		return ""
	}
	return windows.UTF16ToString(buf[:size])
}
