# 持久化归档与不可变快照设计

本文说明 `ranked-ballots-archive` 的磁盘布局、事务边界、并发协调与崩溃恢复策略，
以及这些选择的正确性依据和代价。仅使用 Python >= 3.11 标准库（`sqlite3`、
`hashlib`、`json`、`os`、`tempfile`）。

## 1. 磁盘布局

一个 store 目录只服务一场选举：

```
<store>/
  archive.sqlite3            # 单一 SQLite 数据库（WAL 模式）
  archive.sqlite3-wal
  archive.sqlite3-shm
  reports/
    revision-<r>.json        # 不可变 v2 报告，普通 JSON 文件，可直接拷走核验
    .revision-<r>.*.tmp      # 中断残留：没有任何数据库记录引用，可留可删
```

数据库表：

| 表 | 作用 |
| --- | --- |
| `meta` | 单行，保存 `head_revision` 与 `published_revision` |
| `candidates` | 固定候选名单（init 后不再变更，apply 无法新增候选） |
| `ballots` | 当前有效票档（id、ranking JSON、weight），即 head 修订的状态 |
| `batches` | 成功批次台账：batch_id、原始请求字节 SHA256、原始请求字节、parent/revision |
| `snapshots` | 每个被固定过的修订的规范票档字节及其 `snapshot_sha256` |
| `exports` | 导出任务：export_id、固定 revision、`pending`/`ready` |
| `report_artifacts` | 每个已安装报告的修订与报告字节 SHA256（损坏检测依据） |

报告路径只由修订决定：`reports/revision-<revision>.json`（无前导零）。调用方不能
指定报告路径，同一修订的不同 export_id 共享同一份字节。

## 2. 规范编码

票档快照与报告统一使用：UTF-8、`ensure_ascii=False`、`sort_keys=True`、
`separators=(",",":")`、末尾恰好一个换行（`canonical_bytes`）。

- `snapshot_sha256` = 规范票档 `{"candidates": ..., "ballots": ...}` 字节的 SHA256。
- 报告本身也按规范编码落盘，因此任何核验者拷贝单个报告文件即可：
  1. 用报告内 `candidates`/`ballots` 重算 `snapshot_sha256`，必须与报告字段一致；
  2. 对同一票档调用 v1 的 `analyze`，结果必须与报告内 `result` 逐字节语义相等。
- 报告不写 export_id、时间戳或任何环境相关字段。

候选按 id 升序、选票按 id 升序；ranking 保留录入顺序（ranking 内部不排序）。

## 3. 事务边界

SQLite 以 WAL 模式打开，`synchronous=FULL`，所有写事务显式 `BEGIN IMMEDIATE`，
读一致性用普通 `BEGIN` 快照事务。长耗时工作（IRV 计票、JSON 编码、文件写入）
全部在写事务之外执行。

### apply（更正入账）

1. 在进程内完成 8MB 严格 JSON 读取、重复键/NaN 拒绝、信封与每条 change 的校验、
   全批模拟（未知候选、未知 delete、20000 票上限等）。任何错误都在打开写事务前失败。
2. 先做重放检查：`batches.batch_id` 命中且请求字节 SHA256 相同，直接回原回执；
   同 id 异字节立即冲突。该检查先于 `expected_revision` 基线检查。
3. `BEGIN IMMEDIATE` 后在锁内复查重放（覆盖并发），再校验
   `expected_revision == head_revision`，随后在同一事务内：
   重建整张 `ballots` 表、插入 `batches`（含原始请求字节与 SHA256）、
   `head_revision` 加一，一起 `COMMIT`。

因此“全批有效且基线匹配”与“票档+台账+head 推进”是原子的：失败没有部分更正，
也不会登记成功回执；即使 put 未改变业务值，也照常产生新修订。

### export（快照发布）

1. **登记事务**（短）：`BEGIN IMMEDIATE`，读取当前 head，把该 head 的完整规范票档
   写入 `snapshots`，插入 `exports(export_id, revision, 'pending')`，一起提交。
   这是 export_id 唯一变得可见的点。两个进程争用同一 id 时，唯一约束保证只有一个
   修订被固定；败者在锁内看到已存在任务后回滚并复用该修订。
2. **事务外计票与写文件**：从 `snapshots` 读取固定票档（不再碰 live `ballots`），
   调用原 `analyze`，在 `reports/` 下用临时文件 + `fsync` + `os.replace` + 目录
   `fsync` 原子就位。此阶段不持有任何数据库写锁或全局锁，apply 与其他修订的
   export 都可继续。
3. **完成事务**（短）：把报告字节 SHA256 写入 `report_artifacts`、任务置 `ready`，
   并仅当新修订严格大于 `published_revision` 时推进发布指针，三者同一事务提交。

## 4. 崩溃恢复（SIGKILL 模型）

按崩溃时点逐一分析：

| 崩溃点 | 数据库状态 | 文件状态 | 重开后的行为 |
| --- | --- | --- | --- |
| 登记事务提交前 | 无任务、无快照 | 无报告 | 视同未登记，可重新捕获当前 head |
| 登记提交后、写文件中 | 任务 `pending`、快照已固定 | 可能有 `.tmp` 残留 | 修订永久不变；replay/recover 用旧快照完成；残留永不被引用 |
| 文件就位后、完成事务提交前 | 任务仍 `pending`、指针未推进 | 完整报告已在 | 恢复时按字节复用，不重写不可变文件，再提交 ready/发布 |
| 完成事务提交后 | `ready`、指针已推进 | 完整报告 | 仅做存在性与摘要核验 |

恢复原则：

- 不回滚任何已确认的 apply（apply 事务已提交即持久）。
- 不自动重基旧任务：pending 任务永远绑定登记时的修订；旧导出完成不会回退
  `published_revision`（完成事务只允许指针严格前移）。
- `status` 在一个数据库快照事务内读取 head、发布指针与全部任务，并对每个
  ready/published 引用核验：拒绝符号链接；文件缺失则明确失败（status 不静默修复）；
  文件存在则与 `report_artifacts` 记录的 SHA256 严格比对，任何不一致都报错。
- `recover`/重放 export 对“缺失”文件允许从固定快照重建（字节由快照与 `analyze`
  唯一决定），但对存在却不一致或为符号链接的文件一律拒绝，绝不改写不可变文件。
- 已发布状态只能引用存在且摘要正确的完整报告。

## 5. 并发正确性

- 所有写串行化在 SQLite 的单一写者上，`busy_timeout=30s` 让争用进程等待而非立即失败。
- apply 的入账验收：两个同 `expected_revision` 的不同批次，一个提交成功推进 head，
   另一个在锁内发现基线陈旧而失败；成功批次的同字节重放始终返回同一修订。
- 导出固定快照后立即释放写锁，apply 不需要等待计票或报告写入；不同修订的导出可以
  乱序完成，发布指针取已完成修订中的最大者且只增不减。
- 测试通过文件屏障（`RB_ARCHIVE_BARRIER`）和崩溃点（`RB_ARCHIVE_CRASH_POINT`）
  注入协调，均为测试专用环境变量，生产命令没有任何延时或注入参数。

## 6. 代价与范围限制

- 每个成功批次保留原始请求字节，且每次 apply 重建整张选票表；在 20000 票上限内
  这是可预期的 O(n) 开销，换取“票档即 head 状态”的简单恢复模型（无需事件重放）。
- 快照按修订保存完整规范票档；长期运行会线性增长，但每个快照都是自描述、可独立
  核验的材料，符合离线核验优先可移植性的目标。
- 范围限定为同一主机本地文件系统上的独立进程并发与 SIGKILL 中止；不覆盖机器掉电
  （依赖 WAL + `synchronous=FULL` + 文件 fsync 的本地语义）、网络共享盘或恶意改库。
