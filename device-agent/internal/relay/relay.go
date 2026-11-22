// Package relay is just enough of the Redis protocol (RESP2) for the agent: AUTH, RPUSH,
// GET and PING, over TLS. The agent never talks to the dashboard itself: it pairs and
// checks in through the dashboard's Redis relay (see main/apps/agent_api/relay.py).
package relay

import (
	"bufio"
	"crypto/tls"
	"errors"
	"fmt"
	"io"
	"net"
	"net/url"
	"os"
	"strconv"
	"strings"
	"time"
)

// Timeout bounds dialing and each command.
const Timeout = 20 * time.Second

// maxBulk: the largest value read back (a gzipped blocklist is about a megabyte).
const maxBulk = 64 << 20

// Error is an error reply from Redis ("WRONGPASS …", "NOPERM …").
type Error string

func (e Error) Error() string { return "relay: " + string(e) }

// Denied: Redis refused this login or command. For a paired device it means the dashboard
// removed it (its login was deleted) or paired the computer again (a new login).
func Denied(err error) bool {
	var e Error
	if !errors.As(err, &e) {
		return false
	}
	s := string(e)
	return strings.HasPrefix(s, "WRONGPASS") || strings.HasPrefix(s, "NOPERM") ||
		strings.Contains(s, "invalid username-password")
}

// Address is a parsed relay URL: rediss://user:password@host:port, with the dashboard's
// key prefix in the query string of a pairing address (?prefix=sm).
type Address struct {
	TLS      bool
	Host     string // host:port
	User     string
	Password string
	Prefix   string
}

// Parse checks a relay URL. Plain redis:// (no TLS) only to this machine or the local
// network, where a test relay may run without a certificate; NEXGUARD_ALLOW_PLAIN=1 lifts
// that.
func Parse(raw string) (*Address, error) {
	u, err := url.Parse(strings.TrimSpace(raw))
	if err != nil || u.Hostname() == "" || (u.Scheme != "rediss" && u.Scheme != "redis") {
		return nil, fmt.Errorf("that isn't a relay address like rediss://…@relay.example.com:6379 " +
			"(copy the whole line from the dashboard)")
	}
	if u.Scheme == "redis" && !isLocal(u.Hostname()) && os.Getenv("NEXGUARD_ALLOW_PLAIN") != "1" {
		return nil, errors.New("the relay address must start with rediss:// (encrypted): over " +
			"plain redis:// the login would cross the internet unencrypted")
	}
	port := u.Port()
	if port == "" {
		port = "6379"
	}
	a := &Address{TLS: u.Scheme == "rediss", Host: net.JoinHostPort(u.Hostname(), port),
		User: u.User.Username(), Prefix: u.Query().Get("prefix")}
	a.Password, _ = u.User.Password()
	return a, nil
}

// Redacted: the address without its password, for messages and logs.
func (a *Address) Redacted() string {
	scheme := "redis"
	if a.TLS {
		scheme = "rediss"
	}
	return scheme + "://" + a.Host
}

func isLocal(host string) bool {
	if host == "localhost" || strings.HasSuffix(host, ".local") || strings.HasSuffix(host, ".localhost") {
		return true
	}
	ip := net.ParseIP(host)
	return ip != nil && (ip.IsLoopback() || ip.IsPrivate() || ip.IsLinkLocalUnicast())
}

// Conn is one connection. Not safe for concurrent use.
type Conn struct {
	c net.Conn
	r *bufio.Reader
}

// Dial connects and authenticates.
func Dial(raw string) (*Conn, error) {
	a, err := Parse(raw)
	if err != nil {
		return nil, err
	}
	d := &net.Dialer{Timeout: Timeout}
	var nc net.Conn
	if a.TLS {
		host, _, _ := net.SplitHostPort(a.Host)
		nc, err = tls.DialWithDialer(d, "tcp", a.Host, &tls.Config{ServerName: host, MinVersion: tls.VersionTLS12})
	} else {
		nc, err = d.Dial("tcp", a.Host)
	}
	if err != nil {
		return nil, fmt.Errorf("can't reach the relay at %s: %w", a.Redacted(), err)
	}
	c := &Conn{c: nc, r: bufio.NewReader(nc)}
	if a.Password != "" {
		args := []string{"AUTH", a.Password}
		if a.User != "" {
			args = []string{"AUTH", a.User, a.Password}
		}
		if _, err := c.Do(args...); err != nil {
			c.Close()
			return nil, err
		}
	}
	return c, nil
}

func (c *Conn) Close() error { return c.c.Close() }

// Do runs one command. Replies: string (status or bulk), nil (no value), int64, []any, or
// an Error.
func (c *Conn) Do(args ...string) (any, error) {
	_ = c.c.SetDeadline(time.Now().Add(Timeout))
	var b strings.Builder
	fmt.Fprintf(&b, "*%d\r\n", len(args))
	for _, a := range args {
		fmt.Fprintf(&b, "$%d\r\n%s\r\n", len(a), a)
	}
	if _, err := io.WriteString(c.c, b.String()); err != nil {
		return nil, fmt.Errorf("relay: %w", err)
	}
	return c.read()
}

func (c *Conn) read() (any, error) {
	line, err := c.r.ReadString('\n')
	if err != nil {
		return nil, fmt.Errorf("relay: %w", err)
	}
	if len(line) < 3 || !strings.HasSuffix(line, "\r\n") {
		return nil, fmt.Errorf("relay: unexpected reply %q", line)
	}
	kind, rest := line[0], line[1:len(line)-2]
	switch kind {
	case '+':
		return rest, nil
	case '-':
		return nil, Error(rest)
	case ':':
		return strconv.ParseInt(rest, 10, 64)
	case '$':
		n, err := strconv.Atoi(rest)
		if err != nil || n > maxBulk {
			return nil, fmt.Errorf("relay: bad reply length %q", rest)
		}
		if n < 0 {
			return nil, nil
		}
		buf := make([]byte, n+2)
		if _, err := io.ReadFull(c.r, buf); err != nil {
			return nil, fmt.Errorf("relay: %w", err)
		}
		return string(buf[:n]), nil
	case '*':
		n, err := strconv.Atoi(rest)
		if err != nil {
			return nil, fmt.Errorf("relay: bad reply length %q", rest)
		}
		if n < 0 {
			return nil, nil
		}
		out := make([]any, n)
		for i := range out {
			if out[i], err = c.read(); err != nil {
				return nil, err
			}
		}
		return out, nil
	}
	return nil, fmt.Errorf("relay: unexpected reply %q", line)
}

// Get returns the value at key, or "" and false when there is none.
func (c *Conn) Get(key string) (string, bool, error) {
	v, err := c.Do("GET", key)
	if err != nil || v == nil {
		return "", false, err
	}
	s, ok := v.(string)
	if !ok {
		return "", false, fmt.Errorf("relay: unexpected reply to GET")
	}
	return s, true, nil
}

// Push appends one value to the list at key.
func (c *Conn) Push(key, value string) error {
	_, err := c.Do("RPUSH", key, value)
	return err
}
