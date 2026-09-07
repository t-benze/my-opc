package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	sidecar "happyranch/linux-tsnet-sidecar"
)

func TestConnectorHealthChild(t *testing.T) {
	if os.Getenv("HAPPYRANCH_TEST_HEALTH_CHILD") == "" {
		return
	}
	fd, _ := strconv.Atoi(os.Getenv("HAPPYRANCH_CHILD_HEALTH_FD"))
	out := os.NewFile(uintptr(fd), "health")
	generation := os.Getenv("HAPPYRANCH_CHILD_HEALTH_GENERATION")
	mode := os.Getenv("HAPPYRANCH_TEST_HEALTH_CHILD")
	sequence := uint64(1)
	state := "ready"
	if mode == "waiting" {
		state = "waiting"
	}
	for {
		_, _ = out.WriteString(healthRecord(generation, sequence, state))
		sequence++
		if state == "ready" {
			state = "healthy"
		}
		time.Sleep(2 * time.Millisecond)
	}
}

func runCompositeOrdering(t *testing.T, initiallyActive bool) []string {
	t.Helper()
	t.Setenv("HAPPYRANCH_TEST_HEALTH_CHILD", "healthy")
	var active atomic.Bool
	active.Store(initiallyActive)
	ctx, cancel := context.WithCancel(context.Background())
	n := &recordingNotifier{}
	done := make(chan int, 1)
	go func() {
		done <- superviseConnector(ctx, []string{os.Args[0], "-test.run=TestConnectorHealthChild"}, n, time.Second, 50*time.Millisecond, nil,
			func(context.Context) sidecarObservation {
				if active.Load() {
					return sidecarPresentHealthy
				}
				return sidecarAbsent
			},
			func(context.Context) bool { active.Store(false); return true })
	}()
	if !initiallyActive {
		time.Sleep(10 * time.Millisecond)
		active.Store(true)
	}
	deadline := time.Now().Add(time.Second)
	for time.Now().Before(deadline) {
		n.mu.Lock()
		ready := false
		for _, call := range n.calls {
			if call == "READY=1" {
				ready = true
			}
		}
		n.mu.Unlock()
		if ready {
			break
		}
		time.Sleep(time.Millisecond)
	}
	cancel()
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("supervisor did not stop")
	}
	n.mu.Lock()
	defer n.mu.Unlock()
	return append([]string(nil), n.calls...)
}

func TestCompositeReadinessAcceptsConnectorFirstAndSidecarFirst(t *testing.T) {
	for _, initiallyActive := range []bool{false, true} {
		calls := runCompositeOrdering(t, initiallyActive)
		ready := 0
		for _, call := range calls {
			if call == "READY=1" {
				ready++
			}
		}
		if ready != 1 {
			t.Fatalf("initiallyActive=%v READY count=%d calls=%v", initiallyActive, ready, calls)
		}
	}
}

func TestRepeatedWaitingCannotExtendStartupDeadline(t *testing.T) {
	t.Setenv("HAPPYRANCH_TEST_HEALTH_CHILD", "waiting")
	n := &recordingNotifier{}
	started := time.Now()
	code := superviseConnector(context.Background(), []string{os.Args[0], "-test.run=TestConnectorHealthChild"}, n,
		20*time.Millisecond, 50*time.Millisecond, nil, func(context.Context) sidecarObservation { return sidecarAbsent }, func(context.Context) bool { return true })
	if code != 1 || time.Since(started) > 300*time.Millisecond {
		t.Fatalf("deadline refreshed: code=%d elapsed=%s", code, time.Since(started))
	}
	for _, call := range n.calls {
		if call == "READY=1" || call == "WATCHDOG=1" {
			t.Fatalf("partial health notified: %v", n.calls)
		}
	}
}

func TestSystemdSidecarHealthRequiresCompleteAuthoritativeState(t *testing.T) {
	dir := t.TempDir()
	command := filepath.Join(dir, "systemctl")
	if err := os.WriteFile(command, []byte("#!/bin/sh\nprintf 'active\\nrunning\\nsuccess\\n42\\n'\n"), 0700); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PATH", dir)
	if systemdSidecarState(context.Background()) != sidecarPresentHealthy {
		t.Fatal("active sidecar was rejected")
	}
	if err := os.WriteFile(command, []byte("#!/bin/sh\nprintf 'activating\\nstart\\nsuccess\\n42\\n'\n"), 0700); err != nil {
		t.Fatal(err)
	}
	if systemdSidecarState(context.Background()) != sidecarPresentUnhealthy {
		t.Fatal("partial sidecar readiness was accepted")
	}
	if err := os.WriteFile(command, []byte("#!/bin/sh\nprintf 'garbage\\n'\n"), 0700); err != nil {
		t.Fatal(err)
	}
	if systemdSidecarState(context.Background()) != sidecarUnknown {
		t.Fatal("malformed state was not unknown")
	}
}

func TestAdmissionRemovalWaitsForSidecarBeforeConnectorCleanup(t *testing.T) {
	active := true
	events := []string{}
	healthy := func(context.Context) sidecarObservation {
		events = append(events, "probe")
		if active {
			return sidecarPresentHealthy
		}
		return sidecarAbsent
	}
	stop := func(context.Context) bool { events = append(events, "sidecar-stop"); active = false; return true }
	if !removeSidecarAdmission(context.Background(), healthy, stop) {
		t.Fatal("admission removal failed")
	}
	if len(events) < 3 || events[0] != "probe" || events[1] != "sidecar-stop" || events[2] != "probe" {
		t.Fatalf("unexpected ordering: %v", events)
	}
}

func TestAdmissionRemovalIsIdempotentWhenSidecarAlreadyStopped(t *testing.T) {
	called := false
	if !removeSidecarAdmission(context.Background(), func(context.Context) sidecarObservation { return sidecarAbsent }, func(context.Context) bool { called = true; return true }) {
		t.Fatal("already removed admission was rejected")
	}
	if called {
		t.Fatal("stopped sidecar was signalled twice")
	}
}

func TestAdmissionRemovalUnknownFailsClosedWithoutCleanup(t *testing.T) {
	called := false
	if removeSidecarAdmission(context.Background(), func(context.Context) sidecarObservation { return sidecarUnknown }, func(context.Context) bool { called = true; return true }) {
		t.Fatal("unknown state claimed admission removed")
	}
	if called {
		t.Fatal("unknown state triggered stop")
	}
}

func TestStructuredChildHealthAcceptsExactRecords(t *testing.T) {
	records := make(chan childHealth, 2)
	failed := make(chan error, 1)
	scanChildHealth(bytes.NewBufferString(healthRecord("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", 1, "ready")), records, failed)
	select {
	case err := <-failed:
		t.Fatal(err)
	default:
	}
	record := <-records
	if record.Version != 1 || record.Sequence != 1 || record.State != "ready" {
		t.Fatalf("unexpected record: %#v", record)
	}
}

func TestStructuredChildHealthRejectsMalformedPartialAndUnknownShape(t *testing.T) {
	for _, raw := range []string{
		"not-json\n",
		`{"version":1,"generation":"a","sequence":1}` + "\n",
		`{"version":1,"generation":"a","sequence":1,"state":"ready","extra":true}` + "\n",
	} {
		records := make(chan childHealth, 2)
		failed := make(chan error, 1)
		scanChildHealth(bytes.NewBufferString(raw), records, failed)
		select {
		case <-failed:
		default:
			t.Fatalf("accepted malformed record %q", raw)
		}
	}
}

type countingStopper struct{ calls int }

func (s *countingStopper) Stop() error {
	s.calls++
	return nil
}

func TestStopTwiceUsesSameProductionInstanceAndReceiptsEachInvocation(t *testing.T) {
	stopper := &countingStopper{}
	file, err := os.CreateTemp(t.TempDir(), "receipt")
	if err != nil {
		t.Fatal(err)
	}
	defer file.Close()
	if err := stopTwice(stopper, file, 4242); err != nil {
		t.Fatal(err)
	}
	if stopper.calls != 2 {
		t.Fatalf("Stop calls = %d, want 2", stopper.calls)
	}
	if err := file.Sync(); err != nil {
		t.Fatal(err)
	}
	raw, err := os.ReadFile(file.Name())
	if err != nil {
		t.Fatal(err)
	}
	want := "lifecycle_stop_complete run=4242 invocation=1\nlifecycle_stop_complete run=4242 invocation=2\n"
	if string(raw) != want {
		t.Fatalf("receipt = %q, want %q", raw, want)
	}
}

func TestDiagnosticReceiptHasStableRedactedCategories(t *testing.T) {
	for _, tc := range []struct {
		err             error
		category, phase string
	}{
		{fmt.Errorf("wrapped: %w", sidecar.ErrCredentialInput), "credential_input", "input_acquisition"},
		{sidecar.ErrEngineStart, "engine_start", "engine_initialization"},
		{sidecar.ErrNetworkJoin, "network_join", "peer_establishment"},
		{sidecar.ErrDurableCommit, "durable_commit", "receipt_commit"},
		{errors.New("provider token=/secret/path"), "unknown", "unknown"},
	} {
		raw := strings.TrimPrefix(diagnosticReceipt(tc.err), "diagnostic_receipt=")
		var got map[string]any
		if err := json.Unmarshal([]byte(raw), &got); err != nil {
			t.Fatal(err)
		}
		if got["category"] != tc.category || got["phase"] != tc.phase {
			t.Fatalf("receipt=%v", got)
		}
		if strings.Contains(raw, "token") || strings.Contains(raw, "/secret") || strings.Contains(raw, "provider") {
			t.Fatalf("secret-bearing receipt %q", raw)
		}
	}
}

type recordingNotifier struct {
	mu    sync.Mutex
	calls []string
	err   error
}

func (n *recordingNotifier) Notify(states ...string) error {
	n.mu.Lock()
	defer n.mu.Unlock()
	n.calls = append(n.calls, states...)
	return n.err
}

func TestWatchdogLoopReportsHealthyProcessAndStops(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	n := &recordingNotifier{}
	done := make(chan struct{})
	failed := make(chan error, 1)
	go func() {
		watchdogLoop(ctx, cancel, n, time.Millisecond, failed)
		close(done)
	}()
	time.Sleep(5 * time.Millisecond)
	cancel()
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("watchdog did not stop")
	}
	n.mu.Lock()
	defer n.mu.Unlock()
	if len(n.calls) == 0 {
		t.Fatal("healthy process emitted no watchdog notification")
	}
	for _, call := range n.calls {
		if call != "WATCHDOG=1" {
			t.Fatalf("unexpected notification %q", call)
		}
	}
}

func TestWatchdogFailureCancelsService(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	n := &recordingNotifier{err: errors.New("notify failed")}
	failed := make(chan error, 1)
	watchdogLoop(ctx, cancel, n, time.Millisecond, failed)
	select {
	case <-ctx.Done():
	default:
		t.Fatal("notify failure did not cancel service")
	}
	if <-failed == nil {
		t.Fatal("notify failure was not reported")
	}
}

func TestWithoutNotifySocketPreventsHelperNotification(t *testing.T) {
	env := withoutNotifySocket([]string{"PATH=/bin", "NOTIFY_SOCKET=/run/systemd/notify", "OTHER=value"})
	if got := strings.Join(env, "\n"); got != "PATH=/bin\nOTHER=value" {
		t.Fatalf("helper environment retained notification authority: %q", got)
	}
}
