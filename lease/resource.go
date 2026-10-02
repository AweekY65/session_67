package lease

import (
	"errors"
	"fmt"
	"sync"
)

// ErrStaleFencingToken is returned by FencedResource when a write carries a
// fencing token older than the highest token the resource has seen. This is
// how a downstream resource detects a paused-and-recovered former holder.
var ErrStaleFencingToken = errors.New("fenced resource: stale fencing token")

// FencedResource simulates a downstream resource (e.g. a database or file)
// that enforces fencing: it remembers the highest token it has ever accepted
// and rejects any write with a smaller token.
type FencedResource struct {
	mu        sync.Mutex
	maxToken  uint64
	data      map[string]string
	lastWrite string
}

func NewFencedResource() *FencedResource {
	return &FencedResource{data: map[string]string{}}
}

// Write applies key=value only if token is at least as high as every token
// seen before. Equal tokens are allowed (same holder writing twice).
func (r *FencedResource) Write(token uint64, key, value string) error {
	r.mu.Lock()
	defer r.mu.Unlock()
	if token < r.maxToken {
		return fmt.Errorf("%w: got %d, already seen %d", ErrStaleFencingToken, token, r.maxToken)
	}
	r.maxToken = token
	r.data[key] = value
	r.lastWrite = value
	return nil
}

// Read returns the value for key.
func (r *FencedResource) Read(key string) (string, bool) {
	r.mu.Lock()
	defer r.mu.Unlock()
	v, ok := r.data[key]
	return v, ok
}

// MaxToken returns the highest fencing token accepted so far.
func (r *FencedResource) MaxToken() uint64 {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.maxToken
}
