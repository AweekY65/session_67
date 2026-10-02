package lease

import (
	"errors"
	"path/filepath"
	"sync"
	"testing"
	"time"
)

var testStart = time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)

func newTestManager(t *testing.T) (*Manager, *FakeClock) {
	t.Helper()
	clock := NewFakeClock(testStart)
	m, err := NewManager(clock, nil)
	if err != nil {
		t.Fatalf("NewManager: %v", err)
	}
	return m, clock
}

// 竞争获取：N 个客户端同时抢锁，恰好一个成功。
func TestContendedAcquireExactlyOneWinner(t *testing.T) {
	m, _ := newTestManager(t)
	const n = 32
	start := make(chan struct{})
	tokens := make([]uint64, n)
	errs := make([]error, n)
	var wg sync.WaitGroup
	for i := 0; i < n; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			<-start
			tok, _, err := m.Acquire("res", clientID(i), 10*time.Second)
			tokens[i], errs[i] = tok, err
		}(i)
	}
	close(start)
	wg.Wait()

	winners := 0
	for i := 0; i < n; i++ {
		if errs[i] == nil {
			winners++
			if tokens[i] == 0 {
				t.Fatalf("winner %d got zero token", i)
			}
		} else if !errors.Is(errs[i], ErrLeaseHeld) {
			t.Fatalf("loser %d got unexpected error: %v", i, errs[i])
		}
	}
	if winners != 1 {
		t.Fatalf("expected exactly 1 winner, got %d", winners)
	}
}

// 续约：成功续约可延长租约，使原过期时间点之后锁仍然有效。
func TestRenewExtendsLease(t *testing.T) {
	m, clock := newTestManager(t)
	ttl := 10 * time.Second
	token, expiry, err := m.Acquire("res", "a", ttl)
	if err != nil {
		t.Fatalf("acquire: %v", err)
	}
	clock.Advance(6 * time.Second)
	newExpiry, err := m.Renew("res", "a", token, ttl)
	if err != nil {
		t.Fatalf("renew: %v", err)
	}
	want := testStart.Add(16 * time.Second)
	if !newExpiry.Equal(want) {
		t.Fatalf("new expiry = %v, want %v", newExpiry, want)
	}
	// 越过原始过期时间，但在新租约内：别人仍拿不到锁。
	clock.Advance(5 * time.Second) // t=11s > 原始 10s
	if _, _, err := m.Acquire("res", "b", ttl); !errors.Is(err, ErrLeaseHeld) {
		t.Fatalf("acquire after original expiry: got %v, want ErrLeaseHeld", err)
	}
	_ = expiry
}

// 租约超时：过期后旧持有者续约失败，新客户端可以获取。
func TestLeaseExpiryBlocksRenewAllowsNewAcquire(t *testing.T) {
	m, clock := newTestManager(t)
	ttl := 5 * time.Second
	token, _, err := m.Acquire("res", "a", ttl)
	if err != nil {
		t.Fatalf("acquire: %v", err)
	}
	clock.Advance(6 * time.Second) // 过期
	if _, err := m.Renew("res", "a", token, ttl); !errors.Is(err, ErrLeaseExpired) {
		t.Fatalf("renew after expiry: got %v, want ErrLeaseExpired", err)
	}
	tok2, _, err := m.Acquire("res", "b", ttl)
	if err != nil {
		t.Fatalf("acquire by new holder: %v", err)
	}
	if tok2 <= token {
		t.Fatalf("token not increasing: old %d new %d", token, tok2)
	}
	// 旧持有者不再是当前持有者，释放不被认可。
	if err := m.Release("res", "a", token); !errors.Is(err, ErrNotHeld) {
		t.Fatalf("release by stale holder: got %v, want ErrNotHeld", err)
	}
}

// 边界：恰好在过期瞬间不算有效。
func TestLeaseInvalidAtExactExpiry(t *testing.T) {
	m, clock := newTestManager(t)
	ttl := 5 * time.Second
	token, _, err := m.Acquire("res", "a", ttl)
	if err != nil {
		t.Fatalf("acquire: %v", err)
	}
	clock.Advance(ttl) // 恰好到达过期时刻
	if _, err := m.Renew("res", "a", token, ttl); !errors.Is(err, ErrLeaseExpired) {
		t.Fatalf("renew at exact expiry: got %v, want ErrLeaseExpired", err)
	}
}

// token 单调性：多次获取（释放或过期后）token 严格递增。
func TestFencingTokenMonotonic(t *testing.T) {
	m, clock := newTestManager(t)
	ttl := time.Second
	prev := uint64(0)
	for i := 0; i < 100; i++ {
		tok, _, err := m.Acquire("res", "a", ttl)
		if err != nil {
			t.Fatalf("acquire %d: %v", i, err)
		}
		if tok <= prev {
			t.Fatalf("token not strictly increasing: prev %d, got %d", prev, tok)
		}
		prev = tok
		if i%2 == 0 {
			if err := m.Release("res", "a", tok); err != nil {
				t.Fatalf("release %d: %v", i, err)
			}
		} else {
			clock.Advance(2 * ttl) // 让它过期
		}
	}
	if got := m.LastToken(); got != 100 {
		t.Fatalf("LastToken = %d, want 100", got)
	}
}

// 释放后重获：释放后无需等待即可再次获取。
func TestReleaseThenReacquire(t *testing.T) {
	m, _ := newTestManager(t)
	tok1, _, err := m.Acquire("res", "a", time.Minute)
	if err != nil {
		t.Fatalf("acquire: %v", err)
	}
	if err := m.Release("res", "a", tok1); err != nil {
		t.Fatalf("release: %v", err)
	}
	tok2, _, err := m.Acquire("res", "b", time.Minute)
	if err != nil {
		t.Fatalf("reacquire: %v", err)
	}
	if tok2 <= tok1 {
		t.Fatalf("token not increasing after release: %d then %d", tok1, tok2)
	}
}

// 错误的持有者或 token 不能续约/释放。
func TestRenewAndReleaseAuthorization(t *testing.T) {
	m, _ := newTestManager(t)
	tok, _, err := m.Acquire("res", "a", time.Minute)
	if err != nil {
		t.Fatalf("acquire: %v", err)
	}
	if _, err := m.Renew("res", "b", tok, time.Minute); !errors.Is(err, ErrNotHeld) {
		t.Fatalf("renew by stranger: got %v, want ErrNotHeld", err)
	}
	if _, err := m.Renew("res", "a", tok+99, time.Minute); !errors.Is(err, ErrTokenMismatch) {
		t.Fatalf("renew with wrong token: got %v, want ErrTokenMismatch", err)
	}
	if err := m.Release("res", "b", tok); !errors.Is(err, ErrNotHeld) {
		t.Fatalf("release by stranger: got %v, want ErrNotHeld", err)
	}
	if err := m.Release("res", "a", tok+99); !errors.Is(err, ErrTokenMismatch) {
		t.Fatalf("release with wrong token: got %v, want ErrTokenMismatch", err)
	}
}

// 重启恢复（未过期）：重启后有效租约仍然有效，token 计数不回退。
func TestRestartRestoresLiveLease(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "state.json")
	clock := NewFakeClock(testStart)

	m1, err := NewManager(clock, NewFileStore(path))
	if err != nil {
		t.Fatalf("manager1: %v", err)
	}
	tok, _, err := m1.Acquire("res", "a", time.Minute)
	if err != nil {
		t.Fatalf("acquire: %v", err)
	}

	// 模拟进程重启：同一时钟、同一状态文件，新建 Manager。
	m2, err := NewManager(clock, NewFileStore(path))
	if err != nil {
		t.Fatalf("manager2: %v", err)
	}
	l, live := m2.Inspect("res")
	if !live || l.Holder != "a" || l.Token != tok {
		t.Fatalf("live lease not restored: %+v live=%v", l, live)
	}
	if _, _, err := m2.Acquire("res", "b", time.Minute); !errors.Is(err, ErrLeaseHeld) {
		t.Fatalf("acquire after restart: got %v, want ErrLeaseHeld", err)
	}
	// 持有者可以跨重启续约。
	if _, err := m2.Renew("res", "a", tok, time.Minute); err != nil {
		t.Fatalf("renew after restart: %v", err)
	}
	// token 计数器持久化，重启后继续递增。
	if err := m2.Release("res", "a", tok); err != nil {
		t.Fatalf("release: %v", err)
	}
	tok2, _, err := m2.Acquire("res", "c", time.Minute)
	if err != nil {
		t.Fatalf("acquire after release: %v", err)
	}
	if tok2 <= tok {
		t.Fatalf("token regressed across restart: %d then %d", tok, tok2)
	}
}

// 重启恢复（已过期）：过期租约不得被错误恢复，token 计数依然不回退。
func TestRestartDoesNotRestoreExpiredLease(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "state.json")
	clock := NewFakeClock(testStart)

	m1, err := NewManager(clock, NewFileStore(path))
	if err != nil {
		t.Fatalf("manager1: %v", err)
	}
	tok, _, err := m1.Acquire("res", "a", 5*time.Second)
	if err != nil {
		t.Fatalf("acquire: %v", err)
	}
	clock.Advance(10 * time.Second) // 租约已过期

	m2, err := NewManager(clock, NewFileStore(path))
	if err != nil {
		t.Fatalf("manager2: %v", err)
	}
	if _, live := m2.Inspect("res"); live {
		t.Fatalf("expired lease wrongly restored as live")
	}
	tok2, _, err := m2.Acquire("res", "b", time.Minute)
	if err != nil {
		t.Fatalf("acquire after restart: %v", err)
	}
	if tok2 <= tok {
		t.Fatalf("token regressed across restart: %d then %d", tok, tok2)
	}
}

func clientID(i int) string {
	return "client-" + string(rune('A'+i%26)) + string(rune('0'+i/26))
}
