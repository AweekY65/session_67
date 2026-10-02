# Lease 分布式锁模拟器（完全本地）

一个纯本地的 Lease 分布式锁模拟器：多客户端以本地线程模拟（goroutine 的
Python 对应物），锁状态、租约、fencing token 与事件日志只保存在**内存或本地
JSON 文件**中，不依赖 Redis / etcd / ZooKeeper 或任何外部服务。

## 结构

```
leaselock/
  clock.py    # 可注入时钟：SystemClock / FakeClock
  store.py    # 状态持久化：MemoryStore / FileStore（原子写）
  lock.py     # 核心：acquire / renew / release + fencing token
  fencing.py  # FencedStore：下游资源，按 fencing token 拒绝过期写
  client.py   # 模拟客户端：LeaseClient / AutoRenewClient（后台线程续约）
tests/
  test_lease_lock.py
```

## Lease 状态机

每个资源（resource）的租约状态机：

```
                 acquire 成功
        ┌──────────────────────────┐
        ▼                          │
     ┌───────┐   renew 成功    ┌───┴───┐
     │ FREE  │ ◄────────────── │ HELD  │◄──┐
     └───┬───┘                 └───┬───┘   │ renew（延长 expires_at）
         ▲                         │       │
         │ release 成功            │ 时钟越过 expires_at
         │                         ▼
         │                      ┌────────┐
         └──────────────────────│EXPIRED │（隐式状态：不再可 renew，
             仅持有者可释放       └────────┘  他人 acquire 时被视为 FREE）
```

- `acquire(resource, holder, ttl)`：仅当无租约或现有租约已过期时成功；
  成功时分配**严格递增**的 fencing token，并写入 `expires_at = now + ttl`。
- `renew(resource, holder, token, ttl)`：仅当持有者与 token 均匹配且租约
  **未过期**时成功；过期租约永远不可续期。
- `release(resource, holder, token)`：仅持有者可释放；释放后他人可立即获取，
  token 依旧单调递增。

## Fencing token 原理

`LeaseLock` 维护一个持久化的单调计数器 `last_token`，每次成功 `acquire`
加一并随租约发出。它解决经典的"停顿持有者"问题：

1. 客户端 A 持有锁（token=1），发生 GC 停顿 / 网络分区，超过租约时间。
2. 租约过期，客户端 B 成功获取锁（token=2）。
3. A 恢复后仍以为自己是持有者，但它的 `renew` 会被锁服务拒绝
   （租约已易主），它对下游资源的写入也会被 `FencedStore` 拒绝：
   下游只接受**大于已见最大 token** 的写（`token <= max_token` 即抛
   `StaleFencingToken`）。

因此旧持有者恢复后永远无法拥有更新的 token，其过期写会被识别并拒绝。

## 时间模型

- 所有时间判断都通过可注入的 `Clock` 接口（`now()` / `wait_until()`），
  核心代码不直接调用 `time`。
- `SystemClock` 基于 `time.monotonic`，用于真实运行。
- `FakeClock` 由测试手动 `advance()` 推进，并通过条件变量唤醒等待线程；
  **测试不依赖任何真实 sleep**，线程间同步只用 barrier / event。
- 时钟语义为单调时间：租约过期判断只依赖 `expires_at <= now`。

## 持久化与重启恢复

- `FileStore` 将 `{last_token, leases}` 以 JSON 原子写入本地文件
  （临时文件 + `os.replace` + `fsync`）。
- 重启（重新构造 `LeaseLock`）时：未到期的租约照常恢复；**已过期租约
  直接丢弃**（记录 `expire_on_recovery` 日志），绝不错误复活。
- `last_token` 跨重启保留，保证 fencing token 全局单调。
- 事件日志（acquire / renew / release / 拒绝 / 过期）以 JSON Lines 追加到
  本地日志文件。

## 运行测试

```bash
cd <本目录>
python3 -m pytest -q          # 或: python3 -m pytest -v
```

覆盖场景：竞争获取（16 线程仅 1 胜者）、续约延长与拒绝、租约超时不可续、
客户端暂停超时后恢复（fencing 识别旧持有者）、token 单调性（含跨重启）、
释放后重获、重启持久化恢复、后台自动续约线程保活与停顿丢锁。
