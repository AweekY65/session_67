package lease

import (
	"errors"
	"fmt"
	"sync"
	"time"
)

var (
	// ErrLeaseHeld is returned by Acquire when a live lease already exists.
	ErrLeaseHeld = errors.New("lease: resource already leased")
	// ErrNotHeld is returned when the caller does not hold a live lease.
	ErrNotHeld = errors.New("lease: no live lease held by caller")
	// ErrLeaseExpired is returned when the caller's lease has expired.
	ErrLeaseExpired = errors.New("lease: lease expired")
	// ErrTokenMismatch is returned when the presented fencing token is stale.
	ErrTokenMismatch = errors.New("lease: fencing token mismatch")
)

// Manager grants, renews and releases leases for named resources.
// It is fully local: state lives in memory and is optionally persisted to a
// local JSON file via FileStore. All time comes from an injected Clock.
type Manager struct {
	mu     sync.Mutex
	clock  Clock
	store  *FileStore // nil means in-memory only
	leases map[string]*Lease
	last   uint64 // highest fencing token ever issued
}

// NewManager creates a Manager. If store is non-nil, state is loaded from it
// and persisted on every mutation. Expired leases found on disk are treated
// as gone: they are never resurrected as valid.
func NewManager(clock Clock, store *FileStore) (*Manager, error) {
	m := &Manager{
		clock:  clock,
		store:  store,
		leases: map[string]*Lease{},
	}
	if store != nil {
		state, err := store.Load()
		if err != nil {
			return nil, err
		}
		m.last = state.LastToken
		now := clock.Now()
		for name, l := range state.Leases {
			if l == nil {
				continue
			}
			// Never restore an expired lease as valid.
			if !now.Before(l.Expiry) {
				continue
			}
			cp := *l
			m.leases[name] = &cp
		}
	}
	return m, nil
}

// Acquire tries to take a lease on resource for holder with the given TTL.
// On success it returns a strictly monotonically increasing fencing token
// and the lease expiry.
func (m *Manager) Acquire(resource, holder string, ttl time.Duration) (token uint64, expiry time.Time, err error) {
	if ttl <= 0 {
		return 0, time.Time{}, fmt.Errorf("lease: ttl must be positive")
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	now := m.clock.Now()
	if l, ok := m.leases[resource]; ok && now.Before(l.Expiry) {
		return 0, time.Time{}, ErrLeaseHeld
	}
	m.last++
	l := &Lease{
		Resource: resource,
		Holder:   holder,
		Token:    m.last,
		Expiry:   now.Add(ttl),
	}
	m.leases[resource] = l
	if err := m.persistLocked(); err != nil {
		delete(m.leases, resource)
		m.last--
		return 0, time.Time{}, err
	}
	return l.Token, l.Expiry, nil
}

// Renew extends the lease held by holder on resource. It fails if the lease
// expired (even by a nanosecond), if the holder does not match, or if the
// presented token is not the token of the current lease.
func (m *Manager) Renew(resource, holder string, token uint64, ttl time.Duration) (expiry time.Time, err error) {
	if ttl <= 0 {
		return time.Time{}, fmt.Errorf("lease: ttl must be positive")
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	now := m.clock.Now()
	l, ok := m.leases[resource]
	if !ok || l.Holder != holder {
		return time.Time{}, ErrNotHeld
	}
	if l.Token != token {
		return time.Time{}, ErrTokenMismatch
	}
	if !now.Before(l.Expiry) {
		return time.Time{}, ErrLeaseExpired
	}
	l.Expiry = now.Add(ttl)
	if err := m.persistLocked(); err != nil {
		return time.Time{}, err
	}
	return l.Expiry, nil
}

// Release drops the lease if holder and token match the current lease.
// Releasing an already-expired lease is a no-op success.
func (m *Manager) Release(resource, holder string, token uint64) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	l, ok := m.leases[resource]
	if !ok {
		return nil
	}
	if l.Holder != holder {
		return ErrNotHeld
	}
	if l.Token != token {
		return ErrTokenMismatch
	}
	delete(m.leases, resource)
	return m.persistLocked()
}

// Inspect returns a snapshot of the current lease for resource, if any.
// The second return value reports whether the lease is still live.
func (m *Manager) Inspect(resource string) (Lease, bool) {
	m.mu.Lock()
	defer m.mu.Unlock()
	l, ok := m.leases[resource]
	if !ok {
		return Lease{}, false
	}
	return *l, m.clock.Now().Before(l.Expiry)
}

// LastToken returns the highest fencing token issued so far.
func (m *Manager) LastToken() uint64 {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.last
}

func (m *Manager) persistLocked() error {
	if m.store == nil {
		return nil
	}
	leases := make(map[string]*Lease, len(m.leases))
	for k, v := range m.leases {
		cp := *v
		leases[k] = &cp
	}
	return m.store.Save(&persistedState{LastToken: m.last, Leases: leases})
}
