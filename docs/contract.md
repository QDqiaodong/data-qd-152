# Instant-runoff ballot audit v1

为离线计票核验者处理1..100个唯一候选人、最多20000份唯一id排序选票。ranking可不完整或为空，但不得重复或含未知候选；weight为1..10^9整数，表示相同排序票的聚合权重。
每轮计入仍在竞争者中的首选，已无可用候选的票耗尽；多数分母是当轮非耗尽权重，严格大于半数才胜出。无多数则淘汰最低者一名，最低并列时按候选ID字典序最小者淘汰；此为显式合成规则，不冒充任何地区法定制度。无非耗尽票时winner=null。
样例首轮A4/B3/C2，淘汰C，次轮B5/A4，B胜。输出每轮票数、淘汰和耗尽数；每轮continuing+exhausted=total。无外部投票系统、身份校验、选举安全或多席位STV。

## 归档票档与 v2 报告

`ranked-ballots-archive` 在本地 SQLite 票档之上发布不可变快照报告。报告是
`reports/revision-<revision>.json`（修订为无前导零十进制）的普通 JSON 文件，
顶层恰为 `schema_version=2`、`revision`、`candidates`、`ballots`、
`snapshot_sha256`、`result` 六个字段：

- `candidates` 按候选 id 升序；`ballots` 按选票 id 升序，每张票保留 ranking 原顺序。
- `snapshot_sha256` 对规范票档 `{candidates, ballots}` 计算；规范 JSON 统一
  UTF-8、`ensure_ascii=False`、`sort_keys=True`、`separators=(",",":")`，末尾加一个换行。
- 报告文件本身采用同一规范编码，因此报告字节确定、不含 export_id 或时间戳。
- `result` 严格等于对报告内票档调用 v1 `analyze` 的输出；任何核验者仅持有该
  单个文件即可复算 `snapshot_sha256` 与 `result`。
- 同一修订的不同 export_id 共享同一份报告字节；文件安装后不可改写，符号链接
  目的地被拒绝，现存文件只有完整字节一致才允许复用。

apply 批次信封恰为 `{batch_id, expected_revision, changes}`，变更只有
`{op:"put",id,ranking,weight}` 或 `{op:"delete",id}`，沿用上述标识符、排序、
权重（1..10^9）、8MB 严格 JSON 与未知字段/重复键拒绝规则；有效票档始终满足
候选至多 100、选票至多 20000。

## 纸票抽核（`ranked-ballots-audit`，独立链路）

抽核是**只读**一份 v2 报告与其物理箱位清单的独立工作文件，不更正电子票档、不修改
归档库/任务/发布状态，也不改变上面的 v1 与归档契约。报告额外要求候选 1..12、按 id
升序，`winner` 非 `null`，`N = result.total_weight` 为正；清单顶层恰为
`{containers:[{id,count},...]}`，合计恰等于 N。固定抽样制度为无放回
（`without_replacement`），随机流、断言编译、e 过程与活动字段详见
[抽核设计](audit-design.md)。
