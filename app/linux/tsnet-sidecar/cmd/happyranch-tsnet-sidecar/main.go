package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"net"
	"os"
	"os/signal"
	"path/filepath"
	"syscall"
	"time"

	"github.com/mdlayher/sdnotify"
	sidecar "happyranch/linux-tsnet-sidecar"
)

func main() {
	if len(os.Args) > 1 && os.Args[1] == "supervise-connector" {
		os.Exit(runConnectorSupervisor(os.Args[2:]))
	}
	configPath := flag.String("config", "", "absolute path to the manual-N5 sidecar JSON configuration")
	flag.Parse()
	if *configPath == "" {
		fmt.Fprintln(os.Stderr, "configuration_invalid")
		os.Exit(2)
	}
	raw, err := os.ReadFile(*configPath)
	if err != nil {
		fmt.Fprintln(os.Stderr, "configuration_invalid")
		os.Exit(2)
	}
	var cfg sidecar.Config
	if json.Unmarshal(raw, &cfg) != nil {
		fmt.Fprintln(os.Stderr, "configuration_invalid")
		os.Exit(2)
	}
	credentialsDir := os.Getenv("CREDENTIALS_DIRECTORY")
	if filepath.IsAbs(credentialsDir) {
		cfg.CredentialFile = filepath.Join(filepath.Clean(credentialsDir), "enrollment.key")
	} else {
		// After successful one-use enrollment the privileged post-start helper
		// removes both the source and transient LoadCredential drop-in.  Restarts
		// therefore prove the durable marker against the confirmed-absent source.
		cfg.CredentialFile = "/etc/happyranch/enrollment.key"
	}
	if cfg.Validate() != nil {
		fmt.Fprintln(os.Stderr, "configuration_invalid")
		os.Exit(2)
	}
	ctx, cancel := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer cancel()
	engine := sidecar.NewTSNetEngine()
	svc := sidecar.New(cfg, engine, &netDialer{})
	if err := svc.Start(ctx); err != nil {
		fmt.Fprintln(os.Stderr, diagnosticReceipt(err))
		os.Exit(1)
	}
	notifier, err := sdnotify.New()
	if err != nil || notifier.Notify("READY=1", "STATUS=sidecar listener ready") != nil {
		_ = svc.Stop()
		fmt.Fprintln(os.Stderr, "readiness_unavailable")
		os.Exit(1)
	}
	watchdogErr := make(chan error, 1)
	go watchdogLoop(ctx, cancel, notifier, 10*time.Second, watchdogErr)
	<-ctx.Done()
	if err := stopTwice(svc, os.Stderr, os.Getpid()); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	// Category-only lifecycle evidence for the packaged systemd integration.
	// Stop returns only after listener removal, active-flow drain, and the
	// idempotent engine close have completed.
	select {
	case <-watchdogErr:
		fmt.Fprintln(os.Stderr, "watchdog_unavailable")
		os.Exit(1)
	default:
	}
}

func diagnosticReceipt(err error) string {
	category, phase := "unknown", "unknown"
	switch {
	case errors.Is(err, sidecar.ErrCredentialInput):
		category, phase = "credential_input", "input_acquisition"
	case errors.Is(err, sidecar.ErrEngineStart):
		category, phase = "engine_start", "engine_initialization"
	case errors.Is(err, sidecar.ErrNetworkJoin):
		category, phase = "network_join", "peer_establishment"
	case errors.Is(err, sidecar.ErrDurableCommit):
		category, phase = "durable_commit", "receipt_commit"
	}
	receipt := map[string]any{"category": category, "phase": phase, "actor": "tsnet-sidecar", "unit": "happyranch-tsnet-sidecar.service", "outcome": "failed", "terminal": true, "assertion": map[string]string{"status": "completed"}}
	raw, _ := json.Marshal(receipt)
	return "diagnostic_receipt=" + string(raw)
}

type stoppable interface {
	Stop() error
}

// stopTwice is the production re-entrant shutdown seam. Both calls target the
// same Sidecar; the process-scoped receipts distinguish the invocations.
func stopTwice(svc stoppable, output *os.File, runID int) error {
	for invocation := 1; invocation <= 2; invocation++ {
		if err := svc.Stop(); err != nil {
			return err
		}
		fmt.Fprintf(output, "lifecycle_stop_complete run=%d invocation=%d\n", runID, invocation)
	}
	return nil
}

type systemdNotifier interface {
	Notify(...string) error
}

var _ systemdNotifier = (*sdnotify.Notifier)(nil)

func watchdogLoop(ctx context.Context, cancel context.CancelFunc, notifier systemdNotifier, interval time.Duration, failed chan<- error) {
	ticker := time.NewTicker(interval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			if err := notifier.Notify("WATCHDOG=1"); err != nil {
				failed <- err
				cancel()
				return
			}
		}
	}
}

type netDialer struct{}

func (*netDialer) DialContext(ctx context.Context, network, address string) (net.Conn, error) {
	return (&net.Dialer{}).DialContext(ctx, network, address)
}
