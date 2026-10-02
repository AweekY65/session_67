package lease

import (
	"errors"
	"sync"
	"time"
)

// Client simulates a lock client. It can be paused (e.g. to model a GC stop
// or a network partition) and resumed later; while paused, every operation
// blocks. Pausing does not stop the clock, so a paused client's lease can
// expire underneath it.
type Client struct {
	ID  string
	mgr *Manager
	ttl time.Duration

	mu     sync.Mutex
	gate   chan struct{} // closed while running; swapped for an open chan on Pause
	paused bool

	token  uint64
	expiry time.Time
	held   bool
}

func NewClient(id string, mgr *Manager, ttl time.Duration) *Client {
	return &Client{ID: id, mgr: mgr, ttl: ttl, gate: closedChan()}
}

func closedChan() chan struct{} {
	c := make(chan struct{})
	close(c)
	return c
}

// Pause makes all subsequent operations block until Resume is called.
func (c *Client) Pause() {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.paused {
		return
	}
	c.paused = true
	c.gate = make(chan struct{})
}

// Resume unblocks the client. Operations that were queued while paused now
// proceed — but the manager may already have granted the lease to someone
// else, which is exactly the failure mode fencing tokens protect against.
func (c *Client) Resume() {
	c.mu.Lock()
	defer c.mu.Unlock()
	if !c.paused {
		return
	}
	c.paused = false
	close(c.gate)
}

func (c *Client) waitGate() {
	c.mu.Lock()
	g := c.gate
	c.mu.Unlock()
	<-g
}

// Acquire tries to take the lease. Returns the fencing token on success.
func (c *Client) Acquire(resource string) (uint64, error) {
	c.waitGate()
	token, expiry, err := c.mgr.Acquire(resource, c.ID, c.ttl)
	if err != nil {
		return 0, err
	}
	c.mu.Lock()
	c.token, c.expiry, c.held = token, expiry, true
	c.mu.Unlock()
	return token, nil
}

// Renew extends the held lease. It fails if the lease already expired or was
// taken over by another client.
func (c *Client) Renew(resource string) error {
	c.waitGate()
	c.mu.Lock()
	token, held := c.token, c.held
	c.mu.Unlock()
	if !held {
		return ErrNotHeld
	}
	expiry, err := c.mgr.Renew(resource, c.ID, token, c.ttl)
	if err != nil {
		if errors.Is(err, ErrLeaseExpired) || errors.Is(err, ErrNotHeld) || errors.Is(err, ErrTokenMismatch) {
			c.mu.Lock()
			c.held = false
			c.mu.Unlock()
		}
		return err
	}
	c.mu.Lock()
	c.expiry = expiry
	c.mu.Unlock()
	return nil
}

// Release gives up the lease.
func (c *Client) Release(resource string) error {
	c.waitGate()
	c.mu.Lock()
	token := c.token
	c.mu.Unlock()
	if err := c.mgr.Release(resource, c.ID, token); err != nil {
		return err
	}
	c.mu.Lock()
	c.held = false
	c.mu.Unlock()
	return nil
}

// Write performs a fenced write to a downstream resource using the client's
// current fencing token. A paused-and-recovered former holder will be
// rejected here with ErrStaleFencingToken.
func (c *Client) Write(resource *FencedResource, key, value string) error {
	c.waitGate()
	c.mu.Lock()
	token := c.token
	c.mu.Unlock()
	return resource.Write(token, key, value)
}

// Token returns the client's current fencing token.
func (c *Client) Token() uint64 {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.token
}

// Held reports whether the client believes it holds a lease.
func (c *Client) Held() bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.held
}
