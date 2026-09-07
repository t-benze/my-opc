package sidecar

import (
	"context"
	"net"
	"strings"

	"tailscale.com/tsnet"
)

// TSNetEngine is the production embedded userspace-tailnet adapter. Successful
// Server.Start is the upstream redemption boundary; tsnet persists the node
// identity in the owner-only Dir before returning.
type TSNetEngine struct{ server *tsnet.Server }

func NewTSNetEngine() *TSNetEngine { return &TSNetEngine{} }
func (e *TSNetEngine) Start(ctx context.Context, c EngineConfig, credential []byte) (RedemptionReceipt, error) {
	e.server = &tsnet.Server{Dir: c.StateDir, Hostname: c.RoleIdentity, ControlURL: c.ControlURL, AuthKey: string(credential), Ephemeral: false}
	if err := e.server.Start(); err != nil {
		return RedemptionReceipt{}, ErrEngineStart
	}
	e.server.AuthKey = ""
	lc, err := e.server.LocalClient()
	if err != nil {
		return RedemptionReceipt{}, ErrNetworkJoin
	}
	status, err := lc.Status(ctx)
	if err != nil || status.BackendState != "Running" {
		return RedemptionReceipt{}, ErrNetworkJoin
	}
	visible := false
	for _, peer := range status.Peer {
		for _, expected := range c.ExpectedPeers {
			if peer.HostName == expected || strings.TrimSuffix(peer.DNSName, ".") == expected {
				visible = true
				break
			}
		}
	}
	return RedemptionReceipt{Redeemed: true, Durable: true, ExpectedPeerVisible: visible}, nil
}
func (e *TSNetEngine) Listen(addr string) (net.Listener, error) {
	if e.server == nil {
		return nil, ErrEngine
	}
	return e.server.Listen("tcp", addr)
}
func (e *TSNetEngine) Close() error {
	if e.server == nil {
		return nil
	}
	return e.server.Close()
}
