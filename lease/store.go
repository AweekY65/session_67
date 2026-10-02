package lease

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"time"
)

// Lease describes a single granted lease.
type Lease struct {
	Resource string    `json:"resource"`
	Holder   string    `json:"holder"`
	Token    uint64    `json:"token"`
	Expiry   time.Time `json:"expiry"`
}

// persistedState is the on-disk representation of the lock manager.
type persistedState struct {
	// LastToken is the highest fencing token ever issued. It must survive
	// restarts so tokens stay monotonically increasing across the process
	// lifetime.
	LastToken uint64            `json:"last_token"`
	Leases    map[string]*Lease `json:"leases"`
}

// FileStore persists manager state to a single JSON file on the local disk.
// Writes are atomic (write to temp file + rename).
type FileStore struct {
	path string
}

func NewFileStore(path string) *FileStore {
	return &FileStore{path: path}
}

func (s *FileStore) Save(state *persistedState) error {
	data, err := json.MarshalIndent(state, "", "  ")
	if err != nil {
		return fmt.Errorf("marshal state: %w", err)
	}
	tmp := s.path + ".tmp"
	if err := os.WriteFile(tmp, data, 0o644); err != nil {
		return fmt.Errorf("write temp state: %w", err)
	}
	if err := os.Rename(tmp, s.path); err != nil {
		return fmt.Errorf("rename state: %w", err)
	}
	return nil
}

func (s *FileStore) Load() (*persistedState, error) {
	data, err := os.ReadFile(s.path)
	if errors.Is(err, os.ErrNotExist) {
		return &persistedState{Leases: map[string]*Lease{}}, nil
	}
	if err != nil {
		return nil, fmt.Errorf("read state: %w", err)
	}
	var state persistedState
	if err := json.Unmarshal(data, &state); err != nil {
		return nil, fmt.Errorf("unmarshal state: %w", err)
	}
	if state.Leases == nil {
		state.Leases = map[string]*Lease{}
	}
	return &state, nil
}

// Path returns the underlying file path (mainly for tests and tooling).
func (s *FileStore) Path() string { return s.path }

// Dir returns a store rooted at dir, creating it if needed.
func NewFileStoreInDir(dir, name string) (*FileStore, error) {
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return nil, err
	}
	return &FileStore{path: filepath.Join(dir, name)}, nil
}
