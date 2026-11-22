// Package relaytest is an in-memory Redis for tests: AUTH, RPUSH, LPOP, GET, SET, PING
// and DEL over plain TCP, with one password per user and the keys each user may touch.
package relaytest

import (
	"bufio"
	"bytes"
	"compress/gzip"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"strconv"
	"strings"
	"sync"
	"testing"
)

type user struct {
	password string
	keys     []string // prefixes this user may touch; nil = any (the admin)
}

type Server struct {
	ln      net.Listener
	mu      sync.Mutex
	users   map[string]user
	Lists   map[string][]string
	Strings map[string]string
	gets    map[string]int
	// OnPush runs (without the lock) after every RPUSH: tests play the dashboard there.
	OnPush func(key, value string)
}

// New starts a server, closed when the test ends.
func New(t *testing.T) *Server {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	s := &Server{ln: ln, users: map[string]user{}, Lists: map[string][]string{}, Strings: map[string]string{}, gets: map[string]int{}}
	go s.serve()
	t.Cleanup(func() { _ = ln.Close() })
	return s
}

// AddUser allows user/password to touch keys starting with any of the prefixes.
func (s *Server) AddUser(name, password string, prefixes ...string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.users[name] = user{password: password, keys: prefixes}
}

// RemoveUser: the dashboard deleted this login (the device was removed).
func (s *Server) RemoveUser(name string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	delete(s.users, name)
}

// URL for user (which must exist), as the dashboard would hand it out.
func (s *Server) URL(name string) string {
	s.mu.Lock()
	defer s.mu.Unlock()
	return fmt.Sprintf("redis://%s:%s@%s", name, s.users[name].password, s.ln.Addr())
}

// Set stores a value as the dashboard's admin connection would.
func (s *Server) Set(key, value string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.Strings[key] = value
}

// Gets: how often key was read.
func (s *Server) Gets(key string) int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.gets[key]
}

// Pop takes the first value of a list ("" when empty).
func (s *Server) Pop(key string) string {
	s.mu.Lock()
	defer s.mu.Unlock()
	l := s.Lists[key]
	if len(l) == 0 {
		return ""
	}
	s.Lists[key] = l[1:]
	return l[0]
}

func (s *Server) serve() {
	for {
		c, err := s.ln.Accept()
		if err != nil {
			return
		}
		go s.handle(c)
	}
}

func (s *Server) handle(c net.Conn) {
	defer c.Close()
	r := bufio.NewReader(c)
	var who *user
	for {
		args, err := readCommand(r)
		if err != nil {
			return
		}
		reply, pushed := s.run(&who, args)
		if _, err := io.WriteString(c, reply); err != nil {
			return
		}
		if pushed != nil && s.OnPush != nil {
			s.OnPush(pushed[0], pushed[1])
		}
	}
}

func (s *Server) allowed(who *user, key string) bool {
	if who == nil {
		return false
	}
	for _, p := range who.keys {
		if strings.HasPrefix(key, p) {
			return true
		}
	}
	return false
}

func (s *Server) run(who **user, args []string) (string, []string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	cmd := strings.ToUpper(args[0])
	if cmd == "AUTH" {
		name, pw := "default", args[len(args)-1]
		if len(args) == 3 {
			name = args[1]
		}
		u, ok := s.users[name]
		if !ok || u.password != pw {
			return "-WRONGPASS invalid username-password pair or user is disabled.\r\n", nil
		}
		*who = &u
		return "+OK\r\n", nil
	}
	if cmd == "PING" {
		return "+PONG\r\n", nil
	}
	if len(args) < 2 {
		return "-ERR wrong number of arguments\r\n", nil
	}
	if !s.allowed(*who, args[1]) {
		return "-NOPERM this user has no permissions to access one of the keys\r\n", nil
	}
	key := args[1]
	switch cmd {
	case "RPUSH":
		s.Lists[key] = append(s.Lists[key], args[2:]...)
		return fmt.Sprintf(":%d\r\n", len(s.Lists[key])), []string{key, args[len(args)-1]}
	case "GET":
		s.gets[key]++
		v, ok := s.Strings[key]
		if !ok {
			return "$-1\r\n", nil
		}
		return fmt.Sprintf("$%d\r\n%s\r\n", len(v), v), nil
	case "SET":
		s.Strings[key] = args[2]
		return "+OK\r\n", nil
	}
	return "-ERR unknown command\r\n", nil
}

func readCommand(r *bufio.Reader) ([]string, error) {
	line, err := r.ReadString('\n')
	if err != nil {
		return nil, err
	}
	if !strings.HasPrefix(line, "*") {
		return nil, fmt.Errorf("not an array: %q", line)
	}
	n, err := strconv.Atoi(strings.TrimSpace(line[1:]))
	if err != nil {
		return nil, err
	}
	args := make([]string, n)
	for i := range args {
		head, err := r.ReadString('\n')
		if err != nil {
			return nil, err
		}
		size, err := strconv.Atoi(strings.TrimSpace(head[1:]))
		if err != nil {
			return nil, err
		}
		buf := make([]byte, size+2)
		if _, err := io.ReadFull(r, buf); err != nil {
			return nil, err
		}
		args[i] = string(buf[:size])
	}
	return args, nil
}

// Seal signs body as the dashboard does (apps/agent_api/relay.py `_sealed_for`).
func Seal(key []byte, body string) string {
	m := hmac.New(sha256.New, key)
	m.Write([]byte(body))
	out, _ := json.Marshal(map[string]string{"body": body, "sig": hex.EncodeToString(m.Sum(nil))})
	return string(out)
}

// BlocklistBody is what the dashboard signs for a blocklist: its version and gzipped lines.
func BlocklistBody(version string, lines ...string) string {
	var buf bytes.Buffer
	zw := gzip.NewWriter(&buf)
	_, _ = zw.Write([]byte(strings.Join(lines, "\n")))
	_ = zw.Close()
	out, _ := json.Marshal(map[string]string{"version": version, "gz": base64.StdEncoding.EncodeToString(buf.Bytes())})
	return string(out)
}

// Verify checks a device's signed message ({"t","body","sig"}) and returns its body.
func Verify(key []byte, message string) (body string, ok bool) {
	var m struct{ Body, Sig string }
	if json.Unmarshal([]byte(message), &m) != nil {
		return "", false
	}
	mac := hmac.New(sha256.New, key)
	mac.Write([]byte(m.Body))
	return m.Body, hmac.Equal([]byte(m.Sig), []byte(hex.EncodeToString(mac.Sum(nil))))
}
