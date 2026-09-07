package sidecar

import (
	"context"
	"errors"
	"io"
	"net"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"
)

type fakeEngine struct {
	listener                      net.Listener
	receipt                       RedemptionReceipt
	startErr, listenErr, closeErr error
	events                        *[]string
	listenEntered, listenRelease  chan struct{}
	closed                        chan struct{}
}

func (f *fakeEngine) Start(context.Context, EngineConfig, []byte) (RedemptionReceipt, error) {
	*f.events = append(*f.events, "start")
	return f.receipt, f.startErr
}
func (f *fakeEngine) Listen(string) (net.Listener, error) {
	*f.events = append(*f.events, "listen")
	if f.listenEntered != nil {
		close(f.listenEntered)
		<-f.listenRelease
	}
	return f.listener, f.listenErr
}
func (f *fakeEngine) Close() error {
	*f.events = append(*f.events, "engine-close")
	if f.closed != nil {
		close(f.closed)
	}
	return f.closeErr
}

func validConfig(t *testing.T) Config {
	t.Helper()
	state := filepath.Join(t.TempDir(), "state")
	if err := os.Mkdir(state, 0o700); err != nil {
		t.Fatal(err)
	}
	cred := filepath.Join(filepath.Dir(state), "credential")
	if err := os.WriteFile(cred, []byte("secret-value\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	return Config{StateDir: state, CredentialFile: cred, ControlURL: "https://headscale.private.example", RoleIdentity: "home-sidecar-123", ExpectedPeers: []string{"mac-client-123"}, ListenAddr: ":443", ConnectorAddr: "127.0.0.1:9443", DERPPolicy: "private-only"}
}

func TestValidateRejectsUnsafeTopology(t *testing.T) {
	base := validConfig(t)
	cases := []func(*Config){
		func(c *Config) { c.ControlURL = "https://controlplane.tailscale.com" },
		func(c *Config) { c.ControlURL = "http://headscale.private.example" },
		func(c *Config) { c.ConnectorAddr = "0.0.0.0:9443" },
		func(c *Config) { c.ConnectorAddr = "127.0.0.2:9443" },
		func(c *Config) { c.ConnectorAddr = "127.0.0.1:8765" },
		func(c *Config) { c.RoleIdentity = "ambiguous" },
		func(c *Config) { c.DERPPolicy = "public-fallback" },
	}
	for i, mutate := range cases {
		c := base
		mutate(&c)
		if err := c.Validate(); !errors.Is(err, ErrConfiguration) {
			t.Fatalf("case %d: %v", i, err)
		}
	}
}

func TestCredentialFailuresOccurBeforeListenAndAreRedacted(t *testing.T) {
	for _, name := range []string{"missing", "symlink", "loose", "empty", "replay"} {
		t.Run(name, func(t *testing.T) {
			cfg := validConfig(t)
			events := []string{}
			target := cfg.CredentialFile
			switch name {
			case "missing":
				os.Remove(target)
			case "symlink":
				os.Remove(target)
				os.Symlink(filepath.Join(t.TempDir(), "hostile-secret"), target)
			case "loose":
				os.Chmod(target, 0o644)
			case "empty":
				os.WriteFile(target, nil, 0o600)
			case "replay":
				os.WriteFile(filepath.Join(cfg.StateDir, consumedMarker), []byte("1"), 0o600)
			}
			e := &fakeEngine{receipt: RedemptionReceipt{Redeemed: true, Durable: true, ExpectedPeerVisible: true}, events: &events}
			s := New(cfg, e, &net.Dialer{})
			err := s.Start(context.Background())
			if err == nil || strings.Contains(err.Error(), "secret") || contains(events, "listen") {
				t.Fatalf("err=%v events=%v", err, events)
			}
		})
	}
}

func TestCredentialPathWithSymlinkedParentFailsClosed(t *testing.T) {
	cfg := validConfig(t)
	realParent := filepath.Dir(cfg.CredentialFile)
	alias := filepath.Join(t.TempDir(), "alias")
	if err := os.Symlink(realParent, alias); err != nil {
		t.Fatal(err)
	}
	cfg.CredentialFile = filepath.Join(alias, filepath.Base(cfg.CredentialFile))
	events := []string{}
	err := New(cfg, &fakeEngine{events: &events}, &net.Dialer{}).Start(context.Background())
	if !errors.Is(err, ErrCredentialInput) || len(events) != 0 {
		t.Fatalf("err=%v events=%v", err, events)
	}
}

func TestRedemptionAndDeletionMustBeDurableBeforeListen(t *testing.T) {
	for _, receipt := range []RedemptionReceipt{{}, {Redeemed: true}, {Redeemed: true, Durable: true}} {
		cfg := validConfig(t)
		events := []string{}
		e := &fakeEngine{receipt: receipt, events: &events}
		want := ErrDurableCommit
		if receipt.Redeemed && receipt.Durable {
			want = ErrNetworkJoin
		}
		if err := New(cfg, e, &net.Dialer{}).Start(context.Background()); !errors.Is(err, want) {
			t.Fatalf("%v", err)
		}
		if contains(events, "listen") {
			t.Fatal(events)
		}
	}
}

func TestConnectorProbeFailureClosesEngineBeforeListener(t *testing.T) {
	cfg := validConfig(t)
	events := []string{}
	e := &fakeEngine{receipt: RedemptionReceipt{Redeemed: true, Durable: true, ExpectedPeerVisible: true}, events: &events}
	s := New(cfg, e, dialFunc(func(context.Context, string, string) (net.Conn, error) { return nil, errors.New("hostile secret") }))
	err := s.Start(context.Background())
	if !errors.Is(err, ErrConnector) || strings.Contains(err.Error(), "secret") || contains(events, "listen") {
		t.Fatalf("err=%v events=%v", err, events)
	}
	if events[len(events)-1] != "engine-close" {
		t.Fatal(events)
	}
}

func TestStartSuccessConsumesCredentialThenProxiesRawBytes(t *testing.T) {
	cfg := validConfig(t)
	events := []string{}
	probeClient, probeServer := net.Pipe()
	tailClient, tailServer := net.Pipe()
	connectorClient, connectorServer := net.Pipe()
	listener := &oneListener{conn: tailServer, blockAfter: true, closed: make(chan struct{}), accepted: make(chan struct{}), events: &events}
	e := &fakeEngine{listener: listener, receipt: RedemptionReceipt{Redeemed: true, Durable: true, ExpectedPeerVisible: true}, events: &events}
	dials := []net.Conn{probeClient, connectorClient}
	dial := dialFunc(func(context.Context, string, string) (net.Conn, error) {
		c := dials[0]
		dials = dials[1:]
		return c, nil
	})
	s := New(cfg, e, dial)
	if err := s.Start(context.Background()); err != nil {
		t.Fatal(err)
	}
	_ = probeServer.Close()
	if _, err := os.Stat(cfg.CredentialFile); !os.IsNotExist(err) {
		t.Fatalf("credential remains: %v", err)
	}
	<-listener.accepted
	go tailClient.Write([]byte("raw-request"))
	got := make([]byte, 11)
	if _, err := io.ReadFull(connectorServer, got); err != nil {
		t.Fatal(err)
	}
	if string(got) != "raw-request" {
		t.Fatalf("%q", got)
	}
	go connectorServer.Write([]byte("raw-reply"))
	reply := make([]byte, 9)
	if _, err := io.ReadFull(tailClient, reply); err != nil {
		t.Fatal(err)
	}
	if string(reply) != "raw-reply" {
		t.Fatalf("%q", reply)
	}
	if err := s.Stop(); err != nil {
		t.Fatal(err)
	}
	if events[len(events)-2] != "listener-close" || events[len(events)-1] != "engine-close" {
		t.Fatal(events)
	}
}

func TestSystemdStagedCredentialIsReadOnceAndNeverUnlinked(t *testing.T) {
	cfg := validConfig(t)
	credentialDir := filepath.Join(filepath.Dir(cfg.StateDir), "systemd-credentials")
	if err := os.Mkdir(credentialDir, 0o700); err != nil {
		t.Fatal(err)
	}
	exactCredential := filepath.Join(credentialDir, "enrollment.key")
	if err := os.Rename(cfg.CredentialFile, exactCredential); err != nil {
		t.Fatal(err)
	}
	cfg.CredentialFile = exactCredential
	t.Setenv("CREDENTIALS_DIRECTORY", credentialDir)
	if err := os.Chmod(cfg.CredentialFile, 0o400); err != nil {
		t.Fatal(err)
	}
	events := []string{}
	probeClient, probeServer := net.Pipe()
	defer probeServer.Close()
	listener := &oneListener{acceptErr: net.ErrClosed, events: &events, accepted: make(chan struct{})}
	e := &fakeEngine{listener: listener, receipt: RedemptionReceipt{Redeemed: true, Durable: true, ExpectedPeerVisible: true}, events: &events}
	s := New(cfg, e, dialFunc(func(context.Context, string, string) (net.Conn, error) { return probeClient, nil }))
	if err := s.Start(context.Background()); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(cfg.CredentialFile); err != nil {
		t.Fatalf("systemd-staged credential must remain systemd-owned: %v", err)
	}
	if _, err := os.Stat(filepath.Join(cfg.StateDir, consumedMarker)); err != nil {
		t.Fatalf("durable consumption marker missing: %v", err)
	}
	_ = s.Stop()
}

func TestEnrolledStateRestartDoesNotReadOrRequireCredential(t *testing.T) {
	cfg := validConfig(t)
	if err := os.Remove(cfg.CredentialFile); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(cfg.StateDir, consumedMarker), []byte("durable\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	events := []string{}
	probeClient, probeServer := net.Pipe()
	defer probeServer.Close()
	listener := &oneListener{acceptErr: net.ErrClosed, events: &events, accepted: make(chan struct{})}
	e := &fakeEngine{listener: listener, receipt: RedemptionReceipt{Redeemed: true, Durable: true, ExpectedPeerVisible: true}, events: &events}
	s := New(cfg, e, dialFunc(func(context.Context, string, string) (net.Conn, error) { return probeClient, nil }))
	if err := s.Start(context.Background()); err != nil {
		t.Fatal(err)
	}
	_ = s.Stop()
}

func TestPartialStartAndFailuresCloseListenerFirst(t *testing.T) {
	cfg := validConfig(t)
	events := []string{}
	l := &oneListener{acceptErr: errors.New("raw hostile path /tmp/secret"), events: &events, accepted: make(chan struct{})}
	e := &fakeEngine{listener: l, receipt: RedemptionReceipt{Redeemed: true, Durable: true, ExpectedPeerVisible: true}, events: &events}
	probeClient, probeServer := net.Pipe()
	s := New(cfg, e, dialFunc(func(context.Context, string, string) (net.Conn, error) { return probeClient, nil }))
	if err := s.Start(context.Background()); err != nil {
		t.Fatal(err)
	}
	_ = probeServer.Close()
	<-l.accepted
	if err := s.Stop(); err != nil && strings.Contains(err.Error(), "secret") {
		t.Fatal(err)
	}
	if events[len(events)-2] != "listener-close" || events[len(events)-1] != "engine-close" {
		t.Fatal(events)
	}
}

func TestConcurrentStopIsIdempotent(t *testing.T) {
	cfg := validConfig(t)
	events := []string{}
	l := &oneListener{acceptErr: net.ErrClosed, events: &events, accepted: make(chan struct{})}
	probeClient, probeServer := net.Pipe()
	s := New(cfg, &fakeEngine{listener: l, receipt: RedemptionReceipt{Redeemed: true, Durable: true, ExpectedPeerVisible: true}, events: &events}, dialFunc(func(context.Context, string, string) (net.Conn, error) { return probeClient, nil }))
	if err := s.Start(context.Background()); err != nil {
		t.Fatal(err)
	}
	_ = probeServer.Close()
	var wg sync.WaitGroup
	for i := 0; i < 8; i++ {
		wg.Add(1)
		go func() { defer wg.Done(); _ = s.Stop() }()
	}
	wg.Wait()
	if count(events, "listener-close") != 1 || count(events, "engine-close") != 1 {
		t.Fatal(events)
	}
}

func TestStopWhileListenBlockedRejectsPostShutdownAdmission(t *testing.T) {
	cfg := validConfig(t)
	events := []string{}
	entered, release := make(chan struct{}), make(chan struct{})
	l := &oneListener{acceptErr: errors.New("must never accept"), events: &events, accepted: make(chan struct{})}
	probeClient, probeServer := net.Pipe()
	e := &fakeEngine{listener: l, receipt: RedemptionReceipt{Redeemed: true, Durable: true, ExpectedPeerVisible: true}, events: &events, listenEntered: entered, listenRelease: release}
	s := New(cfg, e, dialFunc(func(context.Context, string, string) (net.Conn, error) { return probeClient, nil }))
	startResult := make(chan error, 1)
	go func() { startResult <- s.Start(context.Background()) }()
	<-entered
	_ = probeServer.Close()
	stopResult := make(chan error, 1)
	go func() { stopResult <- s.Stop() }()
	select {
	case err := <-stopResult:
		t.Fatalf("Stop returned before in-flight Listen was resolved: %v", err)
	case <-time.After(20 * time.Millisecond):
	}
	close(release)
	if err := <-startResult; !errors.Is(err, ErrListener) || strings.Contains(err.Error(), "secret") {
		t.Fatalf("Start error = %v", err)
	}
	if err := <-stopResult; err != nil {
		t.Fatalf("Stop error = %v", err)
	}
	select {
	case <-l.accepted:
		t.Fatal("post-shutdown accept loop admitted")
	default:
	}
	if strings.Join(events, ",") != "start,listen,listener-close,engine-close" {
		t.Fatalf("teardown order = %v", events)
	}
}

func TestUnexpectedAcceptFailureAutomaticallyTearsDownAndIsReported(t *testing.T) {
	cfg := validConfig(t)
	events := []string{}
	l := &oneListener{acceptErr: errors.New("hostile secret /tmp/private"), events: &events, accepted: make(chan struct{})}
	probeClient, probeServer := net.Pipe()
	closed := make(chan struct{})
	e := &fakeEngine{listener: l, receipt: RedemptionReceipt{Redeemed: true, Durable: true, ExpectedPeerVisible: true}, events: &events, closed: closed}
	s := New(cfg, e, dialFunc(func(context.Context, string, string) (net.Conn, error) { return probeClient, nil }))
	if err := s.Start(context.Background()); err != nil {
		t.Fatal(err)
	}
	_ = probeServer.Close()
	<-l.accepted
	select {
	case <-closed:
	case <-time.After(time.Second):
		t.Fatal("automatic teardown did not complete")
	}
	if err := s.Stop(); !errors.Is(err, ErrListener) || strings.Contains(err.Error(), "secret") || strings.Contains(err.Error(), "/tmp") {
		t.Fatalf("Stop error = %v", err)
	}
	if strings.Join(events[len(events)-2:], ",") != "listener-close,engine-close" {
		t.Fatalf("teardown order = %v", events)
	}
}

func TestShippingFailureMatrixUsesStableCategories(t *testing.T) {
	tests := []struct {
		name   string
		engine func(*[]string, net.Listener) *fakeEngine
		dialer func(net.Conn) Dialer
		want   error
	}{
		{"engine-start", func(e *[]string, l net.Listener) *fakeEngine {
			return &fakeEngine{startErr: errors.New("secret"), events: e}
		}, func(net.Conn) Dialer { return &net.Dialer{} }, ErrEngineStart},
		{"listen", func(e *[]string, l net.Listener) *fakeEngine {
			return &fakeEngine{receipt: RedemptionReceipt{true, true, true}, listenErr: errors.New("secret"), events: e}
		}, func(c net.Conn) Dialer {
			return dialFunc(func(context.Context, string, string) (net.Conn, error) { return c, nil })
		}, ErrListener},
		{"probe-close", func(e *[]string, l net.Listener) *fakeEngine {
			return &fakeEngine{receipt: RedemptionReceipt{true, true, true}, events: e}
		}, func(net.Conn) Dialer {
			return dialFunc(func(context.Context, string, string) (net.Conn, error) { return &errorCloseConn{}, nil })
		}, ErrConnector},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			cfg := validConfig(t)
			events := []string{}
			probeClient, probeServer := net.Pipe()
			defer probeServer.Close()
			err := New(cfg, tc.engine(&events, nil), tc.dialer(probeClient)).Start(context.Background())
			if !errors.Is(err, tc.want) || strings.Contains(err.Error(), "secret") {
				t.Fatalf("error = %v events=%v", err, events)
			}
			if contains(events, "listen") && tc.want != ErrListener {
				t.Fatalf("unexpected listener admission: %v", events)
			}
		})
	}
}

func TestAcceptedConnectionDialAndCopyFailuresCloseResources(t *testing.T) {
	for _, tc := range []struct {
		name       string
		proxyDial  func() (net.Conn, error)
		wantClosed <-chan struct{}
	}{
		{name: "dial", proxyDial: func() (net.Conn, error) { return nil, errors.New("dial secret") }},
		func() struct {
			name       string
			proxyDial  func() (net.Conn, error)
			wantClosed <-chan struct{}
		} {
			out := newFailureConn(false)
			return struct {
				name       string
				proxyDial  func() (net.Conn, error)
				wantClosed <-chan struct{}
			}{"copy", func() (net.Conn, error) { return out, nil }, out.closed}
		}(),
	} {
		t.Run(tc.name, func(t *testing.T) {
			cfg := validConfig(t)
			events := []string{}
			in := newFailureConn(tc.name == "copy")
			l := &oneListener{conn: in, blockAfter: true, closed: make(chan struct{}), accepted: make(chan struct{}), events: &events}
			probeClient, probeServer := net.Pipe()
			defer probeServer.Close()
			dials := 0
			d := dialFunc(func(context.Context, string, string) (net.Conn, error) {
				dials++
				if dials == 1 {
					return probeClient, nil
				}
				return tc.proxyDial()
			})
			s := New(cfg, &fakeEngine{listener: l, receipt: RedemptionReceipt{true, true, true}, events: &events}, d)
			if err := s.Start(context.Background()); err != nil {
				t.Fatal(err)
			}
			select {
			case <-in.closed:
			case <-time.After(time.Second):
				t.Fatal("inbound connection was not closed")
			}
			if tc.wantClosed != nil {
				select {
				case <-tc.wantClosed:
				case <-time.After(time.Second):
					t.Fatal("outbound connection was not closed")
				}
			}
			if err := s.Stop(); err != nil {
				t.Fatal(err)
			}
		})
	}
}

func TestStopFailureWithPartialResourcesIsListenerFirstAndRedacted(t *testing.T) {
	cfg := validConfig(t)
	events := []string{}
	l := &oneListener{blockAfter: true, closed: make(chan struct{}), accepted: make(chan struct{}), events: &events, closeErr: errors.New("listener secret")}
	probeClient, probeServer := net.Pipe()
	defer probeServer.Close()
	e := &fakeEngine{listener: l, receipt: RedemptionReceipt{true, true, true}, closeErr: errors.New("engine secret"), events: &events}
	s := New(cfg, e, dialFunc(func(context.Context, string, string) (net.Conn, error) { return probeClient, nil }))
	if err := s.Start(context.Background()); err != nil {
		t.Fatal(err)
	}
	err := s.Stop()
	if !errors.Is(err, ErrEngine) || strings.Contains(err.Error(), "secret") {
		t.Fatalf("Stop error = %v", err)
	}
	if strings.Join(events[len(events)-2:], ",") != "listener-close,engine-close" {
		t.Fatalf("events = %v", events)
	}
}

type failureConn struct {
	failRead bool
	closed   chan struct{}
	once     sync.Once
}

func newFailureConn(failRead bool) *failureConn {
	return &failureConn{failRead: failRead, closed: make(chan struct{})}
}
func (c *failureConn) Read([]byte) (int, error) {
	if c.failRead {
		return 0, errors.New("copy secret")
	}
	<-c.closed
	return 0, io.EOF
}
func (*failureConn) Write(p []byte) (int, error)      { return len(p), nil }
func (c *failureConn) Close() error                   { c.once.Do(func() { close(c.closed) }); return nil }
func (*failureConn) LocalAddr() net.Addr              { return fakeAddr("local") }
func (*failureConn) RemoteAddr() net.Addr             { return fakeAddr("remote") }
func (*failureConn) SetDeadline(time.Time) error      { return nil }
func (*failureConn) SetReadDeadline(time.Time) error  { return nil }
func (*failureConn) SetWriteDeadline(time.Time) error { return nil }

type errorCloseConn struct{ net.Conn }

func (*errorCloseConn) Close() error { return errors.New("hostile secret") }

type dialFunc func(context.Context, string, string) (net.Conn, error)

func (f dialFunc) DialContext(c context.Context, n, a string) (net.Conn, error) { return f(c, n, a) }

type oneListener struct {
	conn       net.Conn
	acceptErr  error
	accepted   chan struct{}
	events     *[]string
	once       sync.Once
	closeErr   error
	blockAfter bool
	closed     chan struct{}
	closeOnce  sync.Once
}

func (l *oneListener) Accept() (net.Conn, error) {
	l.once.Do(func() { close(l.accepted) })
	if l.conn != nil {
		c := l.conn
		l.conn = nil
		return c, nil
	}
	if l.acceptErr != nil {
		return nil, l.acceptErr
	}
	if l.blockAfter {
		<-l.closed
		return nil, net.ErrClosed
	}
	select {}
}
func (l *oneListener) Close() error {
	if l.events != nil {
		*l.events = append(*l.events, "listener-close")
	}
	if l.closed != nil {
		l.closeOnce.Do(func() { close(l.closed) })
	}
	return l.closeErr
}
func (l *oneListener) Addr() net.Addr { return fakeAddr("") }

type fakeAddr string

func (a fakeAddr) Network() string { return "tcp" }
func (a fakeAddr) String() string  { return string(a) }
func contains(xs []string, s string) bool {
	for _, x := range xs {
		if x == s {
			return true
		}
	}
	return false
}
func count(xs []string, s string) int {
	n := 0
	for _, x := range xs {
		if x == s {
			n++
		}
	}
	return n
}

var _ = time.Second
