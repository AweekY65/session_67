package lease

import (
	"errors"
	"testing"
	"time"
)

// 旧客户端恢复：客户端 A 暂停超过租约时长，期间 B 拿到锁；
// A 恢复后续约失败，且其携带旧 fencing token 的写被下游资源拒绝。
func TestPausedClientLosesLeaseAndIsFencedOut(t *testing.T) {
	m, clock := newTestManager(t)
	res := NewFencedResource()
	ttl := 5 * time.Second

	a := NewClient("A", m, ttl)
	b := NewClient("B", m, ttl)

	tokA, err := a.Acquire("res")
	if err != nil {
		t.Fatalf("A acquire: %v", err)
	}
	if err := a.Write(res, "k", "from-A"); err != nil {
		t.Fatalf("A write: %v", err)
	}

	// A 暂停（模拟 GC stop-the-world / 网络分区），时钟继续走。
	a.Pause()
	clock.Advance(2 * ttl) // 超过租约

	tokB, err := b.Acquire("res")
	if err != nil {
		t.Fatalf("B acquire while A paused: %v", err)
	}
	if tokB <= tokA {
		t.Fatalf("B token %d not greater than A token %d", tokB, tokA)
	}
	if err := b.Write(res, "k", "from-B"); err != nil {
		t.Fatalf("B write: %v", err)
	}

	// A 恢复：它仍以为自己持有锁，但世界已经变了。
	a.Resume()
	if err := a.Renew("res"); !errors.Is(err, ErrNotHeld) && !errors.Is(err, ErrTokenMismatch) {
		t.Fatalf("A renew after resume: got %v, want ErrNotHeld or ErrTokenMismatch", err)
	}
	if err := a.Write(res, "k", "stale-write"); !errors.Is(err, ErrStaleFencingToken) {
		t.Fatalf("A write after resume: got %v, want ErrStaleFencingToken", err)
	}
	// 被 fencing 拒绝后，A 可以重新竞争并拿到更新的 token。
	tokA2, err := reacquireAfter(t, a, b, m, clock, ttl)
	if err != nil {
		t.Fatalf("A reacquire: %v", err)
	}
	if tokA2 <= tokB {
		t.Fatalf("A reacquire token %d not greater than B token %d", tokA2, tokB)
	}
	if v, _ := res.Read("k"); v != "from-B" {
		t.Fatalf("stale write leaked into resource: k=%q", v)
	}
}

// 暂停中的操作会阻塞，恢复后才执行（验证 Pause/Resume 门控本身）。
func TestPauseBlocksOperations(t *testing.T) {
	m, clock := newTestManager(t)
	ttl := time.Minute
	a := NewClient("A", m, ttl)
	b := NewClient("B", m, ttl)

	if _, err := a.Acquire("res"); err != nil {
		t.Fatalf("A acquire: %v", err)
	}
	b.Pause()
	done := make(chan error, 1)
	go func() {
		_, err := b.Acquire("res")
		done <- err
	}()
	select {
	case <-done:
		t.Fatal("paused client operation should block")
	default:
	}
	clock.Advance(2 * ttl) // A 的租约在 B 暂停期间过期
	b.Resume()
	if err := <-done; err != nil {
		t.Fatalf("B acquire after resume: %v", err)
	}
	if !b.Held() {
		t.Fatal("B should hold the lease after resume")
	}
}

func reacquireAfter(t *testing.T, a, b *Client, m *Manager, clock *FakeClock, ttl time.Duration) (uint64, error) {
	t.Helper()
	if err := b.Release("res"); err != nil {
		t.Fatalf("B release: %v", err)
	}
	return a.Acquire("res")
}
