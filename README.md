# data-qd-152 · 排序选票逐轮计票

Auditable instant runoff counting with exhausted ballots。业务输入、计算和当前边界详见 [数据契约](docs/contract.md)；
持久化归档与不可变快照的设计依据见 [归档设计](docs/archive-design.md)。

## 安装与演示

Python >=3.11，运行和测试仅用标准库；先确认 `python3 --version`。
每仓建立独立环境，构建工具锁版本。首次安装无公共缓存时可能联网；业务运行不访问网络。

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-build.lock
.venv/bin/python -m pip install --no-build-isolation --no-deps .
.venv/bin/python -m pip check
.venv/bin/ranked-ballots --source fixtures/input.json --output run/report.json
.venv/bin/python -m unittest discover -s tests -v
```

本地已有wheel时，安装构建工具可加 `--no-index --find-links <公共wheel缓存目录>`。
修改源码后重新安装。本仓不共享其他仓环境，不依赖Docker、外部账号或持续服务。

## v1 单次计票（保持不变）

`ranked-ballots --source JSON --output JSON` 仍是原有的单操作者计票链路：
输入 JSON 最多 8MB，拒绝重复键、非有限数值和未定义字段；输出 `schema_version=1`
的独立 JSON，成功后原子替换，输入/计算失败退出 2 并保留旧报告。
输入和输出不得相同，输出符号链接拒绝。它不承担归档的并发或恢复语义。
同输入重跑不累加。测试使用临时目录，运行产物在忽略的 `run/`。

`src/ranked_ballots/model.py` 的 `analyze(data)` 契约完全保留，归档报告中的
`result` 严格等于对快照票档调用该函数的输出。

## 归档命令 `ranked-ballots-archive`

一个 store 目录只服务一场选举。所有子命令成功时向 stdout 输出一个 JSON 对象；
失败以退出码 2 结束并在 stderr 给出明确原因。

### 1. 初始化票档

```sh
ranked-ballots-archive init --store run/election --source fixtures/input.json
# {"ballot_count":3,"candidate_count":3,"head_revision":0,"published":null,"store":"run/election"}
```

init 只创建新票档（已存在则失败，绝不覆盖）。候选名单此刻固定，之后不支持变更；
初始 `head_revision=0`、`published=null`。输入沿用原 `{candidates, ballots}` 契约。

### 2. 接收更正批次

批次文件顶层恰为 `{batch_id, expected_revision, changes}`：

```sh
cat > /tmp/batch1.json <<'JSON'
{"batch_id":"b-001","expected_revision":0,
 "changes":[{"op":"put","id":"four","ranking":["A","B"],"weight":7},
            {"op":"delete","id":"two"}]}
JSON
ranked-ballots-archive apply --store run/election --source /tmp/batch1.json
# {"batch_id":"b-001","parent_revision":0,"request_sha256":"...","revision":1}
```

- `expected_revision` 是非负整数（布尔拒绝），必须等于当前 head 才入账；
  修订恰增 1，业务值未变的 put 也形成新修订。
- 每批 1..20000 条变更，单个选票 id 每批至多出现一次；put 是完整新增/替换，
  delete 必须引用当前存在的票。提交后仍满足候选 ≤100、选票 ≤20000 及全部原约束。
- 整批原子：任一条非法则无任何更正、无成功回执。
- 幂等：同 `batch_id` 同字节重放返回原回执（优先于基线冲突检查）；同 id 异字节
  直接冲突，且不登记任何入账。

### 3. 发布不可变快照报告

```sh
ranked-ballots-archive export --store run/election --export-id count-2026-11-17
# {"export_id":"count-2026-11-17","report_path":"reports/revision-1.json",
#  "report_sha256":"...","revision":1,"state":"ready"}
```

首次出现的 export_id 先在一个短事务内固定当时的 head 修订和完整票档，再在事务外
计票并写报告；同一 id 重放或 recover 永远只完成该修订，不会重新捕获更新的票档。
报告是 store 内 `reports/revision-<r>.json` 的普通 JSON（`schema_version=2`，
含 `revision`、完整 `candidates`/`ballots`、`snapshot_sha256`、`result`），
可直接拷给另一位核验者独立复算。文件安装后不可改写，符号链接目的地被拒绝，
同修订现存文件只有字节完全一致才复用。计票耗时不会阻塞后续 apply，另一个修订的
导出也可以先完成。

### 4. 查看状态

```sh
ranked-ballots-archive status --store run/election
```

输出至少包含：

- `head_revision`：当前票档修订；
- `published`：`null` 或 `{revision, report_path, report_sha256}`，只在完整报告
  可读且摘要核验通过后推进，修订严格递增；
- `tasks`：按 export_id 排序，每项含 id、固定修订与 `pending`/`ready`；
  ready 项附报告相对路径与摘要。

`ready` 只表示该修订的报告可读，不代表它仍是最新发布报告；旧修订任务可以 ready，
但 `published` 始终指向已发布的最高修订。任何引用文件缺失或摘要不符都会让
status 明确失败，不会返回虚假的正常状态，也不会静默修复文件。

### 5. 崩溃后恢复

```sh
ranked-ballots-archive recover --store run/election
# {"recovered":[...完成的任务...],"status":{...与 status 相同的一致视图...}}
```

recover 完成所有已登记但未完成的导出任务。它不回滚已确认的入账、不重基旧任务、
不把 `.tmp` 暂存文件当作报告；缺失的报告可从固定快照按确定字节重建，损坏或符号
链接一律拒绝。可与其他命令并发调用。

### 可直接测试的 Python API

```python
from ranked_ballots import (
    init_store, apply_batch, export_report, store_status, recover, ArchiveError
)
init_store("run/election", "fixtures/input.json")
apply_batch("run/election", "/tmp/batch1.json")
export_report("run/election", "count-2026-11-17")
store_status("run/election")
recover("run/election")
```

## 测试

```sh
# 无需安装时（环境无 pip 也可运行）
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

- `tests/test_business.py`：原 v1 业务与 CLI 回归。
- `tests/test_archive.py`：归档 API、真实 CLI 子进程、进程间并发（同基线争用、
  重放、非法批次原子性、导出不阻塞 apply）、以及用真实 SIGKILL + 重开验证的
  崩溃注入（登记前、写文件中、文件就位后、发布提交前后）。
- 测试屏障/崩溃点只通过环境变量 `RB_ARCHIVE_BARRIER`、`RB_ARCHIVE_CRASH_POINT`
  暴露给测试 worker，生产命令没有延时参数。

`src/ranked_ballots/model.py` 为业务模型，相关算法模块独立组织；`common.py` 提供
受限 JSON 及 v1 报告发布，`archive.py` 实现持久化归档，`cli.py` / `archive_cli.py`
衔接两条数据链路。fixtures 为明确自制样例，代码与样例采用 MIT 许可。

## 业务规则摘要

为离线计票核验者处理 1..100 个唯一候选人、最多 20000 份唯一 id 排序选票。
ranking 可不完整或为空，但不得重复或含未知候选；weight 为 1..10^9 整数。
每轮计入仍在竞争者中的首选，已无可用候选的票耗尽；多数分母是当轮非耗尽权重，
严格大于半数才胜出。无多数则淘汰最低者一名，最低并列时按候选 ID 字典序最小者
淘汰；此为显式合成规则，不冒充任何地区法定制度。无非耗尽票时 winner=null。
样例首轮 A4/B3/C2，淘汰 C，次轮 B5/A4，B 胜。每轮
continuing+exhausted=total。无外部投票系统、身份校验、选举安全或多席位 STV。
