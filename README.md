# leasesim — 完全本地的 Lease 分布式锁模拟器

一个纯本地的 lease 分布式锁模拟器：多客户端以 goroutine 模拟，锁状态、租约、
fencing token 只保存在内存或本地 JSON 文件中，**不依赖 Redis / etcd / ZooKeeper
或任何外部服务**。

## 目录结构

```
lease/            核心库
  clock.go        可注入时钟（RealClock / FakeClock）
  manager.go      锁管理器：Acquire / Renew / Release
  store.go        本地 JSON 文件持久化（原子写：tmp + rename）
  resource.go     FencedResource：演示 fencing token 如何保护下游资源
  client.go       可暂停/恢复的模拟客户端
cmd/demo/         端到端演示（真实时钟，展示暂停-过期-接管-fencing 全过程）
```

## Lease 状态机

每个资源（resource）在任何时刻最多存在一个**有效** lease：

```
                 Acquire 成功
              ┌─────────────────┐
              ▼                 │
        ┌──────────┐  Renew     │
        │  HELD    │────────────┘ (holder+token 匹配且未过期，expiry 顺延)
        │ (holder, │
        │  token,  │  Release (holder+token 匹配)
        │  expiry) │────────────┐
        └────┬─────┘            │
  时钟越过   │ expiry            ▼
  expiry     ▼               ┌─────────┐
        ┌──────────┐         │  FREE   │
        │ EXPIRED  │         └────┬────┘
        └────┬─────┘              │
             │  Acquire 成功       │ Acquire 成功
             └────────────────────┴──► 新的 HELD（token 严格递增）
```

- `Acquire(resource, holder, ttl)`：仅当没有有效 lease 时成功，返回
  `(token, expiry)`；否则返回 `ErrLeaseHeld`。
- `Renew(resource, holder, token, ttl)`：holder 与 token 必须匹配当前 lease，
  且**当前时刻必须严格早于 expiry**（恰好在过期瞬间也算过期）。过期租约
  续约返回 `ErrLeaseExpired`，绝不“复活”。
- `Release(resource, holder, token)`：holder 与 token 匹配才释放；
  已过期/不存在的 lease 释放是幂等成功。

## Fencing token 原理

每次成功的 `Acquire` 都会从管理器的**全局单调递增计数器**发放一个新的
fencing token（计数器随状态一起持久化，重启后不回退）。这解决了经典的
“客户端暂停后复活”问题：

1. A 持有锁（token=1），发生长时间 GC 暂停 / 网络分区；
2. A 的 lease 到期，B 成功 `Acquire`，获得 token=2；
3. A 恢复，仍以为自己持有锁，尝试续约 → 管理器拒绝（lease 已易主）；
4. A 直接向下游资源写入（携带 token=1）→ `FencedResource` 记录已见过的
   最大 token（2），拒绝所有更小的 token，返回 `ErrStaleFencingToken`。

锁只能保证互斥的“进入”，fencing token 保证**延迟到达的旧持有者操作**
在下游资源处被识别并拒绝。二者缺一不可。

## 时间模型

所有时间判断都通过注入的 `lease.Clock` 接口：

```go
type Clock interface { Now() time.Time }
```

- 生产环境用 `RealClock`（包装 `time.Now`）；
- 测试用 `FakeClock`，通过 `Advance(d)` 手动推进，**全部测试零真实 sleep**，
  因此租约过期、暂停-恢复等时序场景都是确定性的；
- lease 过期判断为 `now < expiry`（严格小于），过期边界明确。

## 持久化与重启恢复

- 每次状态变更（acquire/renew/release）都把 `{last_token, leases}` 原子写入
  本地 JSON 文件（先写 `.tmp` 再 rename）；
- 重启时加载状态：**已过期的 lease 直接丢弃，绝不恢复为有效**；
  未过期 lease 继续有效，持有者可跨重启续约；
- `last_token` 持久化保证 fencing token 跨重启仍然单调递增。

## 运行测试

```bash
go test ./...          # 全部测试
go test -race ./...    # 带竞态检测
go test -v ./lease/    # 查看各场景
```

测试覆盖（`lease/manager_test.go`、`lease/client_test.go`）：

| 场景 | 测试 |
|---|---|
| 竞争获取（32 个 goroutine 抢锁，恰好 1 个成功） | `TestContendedAcquireExactlyOneWinner` |
| 续约延长租约 | `TestRenewExtendsLease` |
| 租约超时后旧持有者续约失败、新持有者可获取 | `TestLeaseExpiryBlocksRenewAllowsNewAcquire` |
| 恰好在过期瞬间无效 | `TestLeaseInvalidAtExactExpiry` |
| 旧客户端暂停-恢复，被 fencing token 识别 | `TestPausedClientLosesLeaseAndIsFencedOut` |
| 暂停会阻塞操作、恢复后继续 | `TestPauseBlocksOperations` |
| fencing token 严格单调递增（100 次获取） | `TestFencingTokenMonotonic` |
| 释放后立即重获 | `TestReleaseThenReacquire` |
| 越权续约/释放被拒绝 | `TestRenewAndReleaseAuthorization` |
| 重启后有效 lease 恢复、token 不回退 | `TestRestartRestoresLiveLease` |
| 重启后过期 lease 不被错误恢复 | `TestRestartDoesNotRestoreExpiredLease` |

## 运行演示

```bash
go run ./cmd/demo
```

输出示例：A 拿到 token=1 后暂停，租约过期，B 拿到 token=2；A 恢复后续约
被拒绝，携带旧 token 的写被下游资源以 `ErrStaleFencingToken` 拒绝。
