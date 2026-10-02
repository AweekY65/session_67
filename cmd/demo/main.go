// demo 模拟两个客户端竞争一把 lease 锁：A 拿到锁后被“暂停”超过租约，
// B 接管，A 恢复后其旧 fencing token 被下游资源拒绝。
package main

import (
	"errors"
	"fmt"
	"time"

	"leasesim/lease"
)

func main() {
	clock := lease.RealClock{}
	store, err := lease.NewFileStoreInDir(".", "lease-state.json")
	if err != nil {
		panic(err)
	}
	mgr, err := lease.NewManager(clock, store)
	if err != nil {
		panic(err)
	}
	res := lease.NewFencedResource()
	ttl := 2 * time.Second

	a := lease.NewClient("A", mgr, ttl)
	b := lease.NewClient("B", mgr, ttl)

	tokA, err := a.Acquire("job")
	must(err)
	fmt.Printf("A acquired lease, fencing token=%d\n", tokA)
	must(a.Write(res, "output", "A's result"))
	fmt.Println("A wrote to fenced resource")

	// A 暂停（模拟长时间 GC / 网络分区），租约在此期间过期。
	a.Pause()
	fmt.Printf("A paused; sleeping past the %v lease...\n", ttl)
	time.Sleep(3 * time.Second)

	tokB, err := b.Acquire("job")
	must(err)
	fmt.Printf("B acquired lease, fencing token=%d (> %d)\n", tokB, tokA)
	must(b.Write(res, "output", "B's result"))

	// A 恢复：续约失败，旧 token 的写被 fencing 拒绝。
	a.Resume()
	if err := a.Renew("job"); err != nil {
		fmt.Printf("A renew after resume rejected: %v\n", err)
	}
	if err := a.Write(res, "output", "A's stale write"); errors.Is(err, lease.ErrStaleFencingToken) {
		fmt.Printf("A stale write rejected by fencing token: %v\n", err)
	}
	v, _ := res.Read("output")
	fmt.Printf("final resource value: %q (stale write blocked)\n", v)
}

func must(err error) {
	if err != nil {
		panic(err)
	}
}
