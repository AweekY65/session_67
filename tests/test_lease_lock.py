"""End-to-end tests for the local lease lock simulator.

Every test uses FakeClock: no real sleeps anywhere. Threaded tests
synchronize through barriers / events, never through time.
"""

import json
import threading

import pytest

from leaselock import (
    AutoRenewClient,
    ClientPaused,
    FakeClock,
    FencedStore,
    FileStore,
    LeaseClient,
    LeaseLock,
    MemoryStore,
    StaleFencingToken,
)

TTL = 10.0


def make_lock(clock=None):
    clock = clock or FakeClock()
    return LeaseLock(MemoryStore(), clock), clock


# ---------------------------------------------------------------- contention


def test_concurrent_acquire_exactly_one_winner():
    lock, _ = make_lock()
    n_threads = 16
    barrier = threading.Barrier(n_threads)
    results = [None] * n_threads

    def worker(i):
        barrier.wait()
        results[i] = lock.acquire("res", f"client-{i}", TTL)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    winners = [r for r in results if r is not None]
    assert len(winners) == 1
    assert all(r is None for r in results if r not in winners)
    # The winner holds the only valid lease.
    current = lock.current_lease("res")
    assert current is not None and current.token == winners[0].token


# --------------------------------------------------------------------- renew


def test_renew_extends_lease_and_rejects_others():
    lock, clock = make_lock()
    lease = lock.acquire("res", "alice", TTL)
    assert lease is not None

    clock.advance(6)
    renewed = lock.renew("res", "alice", lease.token, TTL)
    assert renewed is not None
    assert renewed.expires_at == pytest.approx(6 + TTL)
    assert renewed.token == lease.token  # renew keeps the token

    # A different holder cannot renew, even with a guessed token.
    assert lock.renew("res", "bob", lease.token, TTL) is None
    # Right holder, wrong token.
    assert lock.renew("res", "alice", lease.token + 1, TTL) is None
    # Resource stays locked past the original expiry.
    clock.advance(TTL - 1)
    assert lock.acquire("res", "bob", TTL) is None


def test_expired_lease_cannot_be_renewed():
    lock, clock = make_lock()
    lease = lock.acquire("res", "alice", TTL)
    clock.advance(TTL + 0.1)
    assert lock.renew("res", "alice", lease.token, TTL) is None
    # And someone else can now take over.
    takeover = lock.acquire("res", "bob", TTL)
    assert takeover is not None and takeover.token > lease.token


# ------------------------------------------- pause / resume / fencing tokens


def test_paused_client_loses_lease_and_is_fenced_out():
    lock, clock = make_lock()
    fenced = FencedStore()

    alice = LeaseClient(lock, "alice")
    bob = LeaseClient(lock, "bob")

    lease_a = alice.acquire("res", TTL)
    assert lease_a is not None
    fenced.write("balance", 100, lease_a.token)

    # Alice stalls (GC pause / partition) longer than the lease.
    alice.pause()
    with pytest.raises(ClientPaused):
        alice.renew(TTL)
    clock.advance(TTL + 1)

    # Bob acquires while alice is frozen, with a strictly higher token.
    lease_b = bob.acquire("res", TTL)
    assert lease_b is not None
    assert lease_b.token > lease_a.token
    fenced.write("balance", 200, lease_b.token)

    # Alice wakes up believing she still owns the lock.
    alice.resume()
    assert alice.renew(TTL) is None  # stale lease: renew rejected
    # Her stale fencing token is rejected by the downstream resource.
    with pytest.raises(StaleFencingToken):
        fenced.write("balance", 999, lease_a.token)
    # Bob's newer token was accepted; the data is his write.
    assert fenced.read("balance") == 200


def test_fencing_tokens_strictly_increase_across_holders():
    lock, clock = make_lock()
    tokens = []
    for i in range(5):
        lease = lock.acquire("res", f"client-{i}", TTL)
        assert lease is not None
        tokens.append(lease.token)
        clock.advance(TTL + 1)  # let it expire before the next acquire
    assert tokens == sorted(tokens)
    assert len(set(tokens)) == len(tokens)
    assert all(b > a for a, b in zip(tokens, tokens[1:]))


def test_fenced_store_rejects_equal_and_older_tokens():
    fenced = FencedStore()
    fenced.write("k", "v1", token=7)
    with pytest.raises(StaleFencingToken):
        fenced.write("k", "v2", token=7)  # equal is stale
    with pytest.raises(StaleFencingToken):
        fenced.write("k", "v3", token=3)
    fenced.write("k", "v4", token=8)
    assert fenced.read("k") == "v4"


# ------------------------------------------------------------------- release


def test_release_then_reacquire():
    lock, _ = make_lock()
    lease = lock.acquire("res", "alice", TTL)
    assert lock.acquire("res", "bob", TTL) is None

    # Wrong holder / wrong token cannot release.
    assert not lock.release("res", "bob", lease.token)
    assert not lock.release("res", "alice", lease.token + 1)

    assert lock.release("res", "alice", lease.token)
    assert lock.current_lease("res") is None

    lease2 = lock.acquire("res", "bob", TTL)
    assert lease2 is not None
    assert lease2.token > lease.token  # token still monotonic after release


# --------------------------------------------------------------- persistence


def test_restart_does_not_resurrect_expired_lease(tmp_path):
    state_file = str(tmp_path / "state.json")
    log_file = str(tmp_path / "events.log")
    clock = FakeClock()

    lock1 = LeaseLock(FileStore(state_file), clock, log_path=log_file)
    lease = lock1.acquire("res", "alice", TTL)
    assert lease is not None

    # Restart while the lease is still valid: it survives.
    lock2 = LeaseLock(FileStore(state_file), clock, log_path=log_file)
    assert lock2.acquire("res", "bob", TTL) is None
    assert lock2.current_lease("res").holder == "alice"

    # Time passes beyond the expiry, then another restart.
    clock.advance(TTL + 1)
    lock3 = LeaseLock(FileStore(state_file), clock, log_path=log_file)
    # The expired lease must NOT be restored as valid.
    assert lock3.current_lease("res") is None
    lease3 = lock3.acquire("res", "bob", TTL)
    assert lease3 is not None
    # Fencing token keeps increasing across restarts.
    assert lease3.token > lease.token

    # Events were logged locally.
    with open(log_file, encoding="utf-8") as fh:
        events = [json.loads(line)["event"] for line in fh]
    assert "acquire" in events
    assert "expire_on_recovery" in events


def test_state_file_is_valid_json_and_token_survives_restart(tmp_path):
    state_file = str(tmp_path / "state.json")
    clock = FakeClock()
    lock1 = LeaseLock(FileStore(state_file), clock)
    lease = lock1.acquire("res", "alice", TTL)

    with open(state_file, encoding="utf-8") as fh:
        saved = json.load(fh)
    assert saved["last_token"] == lease.token

    lock2 = LeaseLock(FileStore(state_file), clock)
    assert lock2.last_token == lease.token


# ------------------------------------------- threaded auto-renewing clients


class EventSink:
    def __init__(self):
        self._events = {}
        self._mu = threading.Lock()

    def __call__(self, event):
        with self._mu:
            self._events.setdefault(event, threading.Event()).set()

    def wait(self, event, timeout=5.0):
        with self._mu:
            ev = self._events.setdefault(event, threading.Event())
        return ev.wait(timeout)

    def clear(self, event):
        with self._mu:
            self._events.setdefault(event, threading.Event()).clear()


def test_auto_renew_client_keeps_lease_then_loses_it_when_paused():
    clock = FakeClock()
    lock = LeaseLock(MemoryStore(), clock)
    sink = EventSink()

    alice = AutoRenewClient(lock, clock, "alice", "res", TTL, on_event=sink)
    alice.start()
    assert sink.wait("acquired")
    token_a = alice.current_lease.token

    # Lease survives well past its TTL thanks to background renewals.
    # Each half-TTL advance is synchronized with the renewal it triggers:
    # with a fake clock, jumping further would expire the lease before the
    # worker thread gets a chance to renew it.
    for _ in range(5):
        sink.clear("renewed")
        clock.advance(TTL / 2)
        assert sink.wait("renewed")
    assert lock.current_lease("res").holder == "alice"
    assert lock.current_lease("res").token == token_a

    # Alice freezes; her lease expires; bob takes over with a higher token.
    alice.pause()
    clock.advance(TTL + 1)
    lease_b = lock.acquire("res", "bob", TTL)
    assert lease_b is not None and lease_b.token > token_a

    # Alice resumes, notices the loss, and cannot renew the stale lease.
    alice.resume()
    assert sink.wait("lost")
    assert lock.current_lease("res").holder == "bob"

    alice.stop()
    assert sink.wait("stopped")
