# data-qd-152 · 排序选票逐轮计票

Auditable instant runoff counting with exhausted ballots。业务输入、计算和当前边界详见 [数据契约](docs/contract.md)；
持久化归档与不可变快照的设计依据见 [归档设计](docs/archive-design.md)；
独立纸票抽核活动的协议、统计假设与设计取舍见 [抽核设计](docs/audit-design.md)；
扫描转录与固定 v2 票档的容量对账语义、Hall 见证与算法依据见 [对账设计](docs/link-design.md)；
两两偏好矩阵、严格多数图与 Condorcet winner 的语义与实现取舍见 [两两偏好设计](docs/pairwise-design.md)；
首选票公开汇总表的遮盖规划、可推断性判据与算法依据见 [遮盖规划设计](docs/disclosure-design.md)。

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

## 纸票抽核活动 `ranked-ballots-audit`

独立的单操作者工作文件，只读**一份固定 v2 报告**和一份**物理箱位清单**，输出待取
物理位置、人工解释与 `continue` / `certified` / `not_certified` 决策。它不更正电子
票档，不修改归档库、任务或发布状态，也不改动上面的两个入口。协议、统计假设与
设计取舍见 [抽核设计](docs/audit-design.md)。

```sh
ranked-ballots-audit start  --report V2_JSON --manifest JSON --seed HEX \
                            --risk P/Q --max-draws M --session FILE
ranked-ballots-audit draw   --session FILE --count K
ranked-ballots-audit record --session FILE --source JSON
ranked-ballots-audit status --session FILE
```

成功输出状态 JSON；可解释失败退出 2 并保留活动文件字节。固定抽样制度为
**无放回均匀抽取（`without_replacement`）**，有放回制度不实现、不混用。

### 一轮可复现示例 start → draw → 乱序 record → status

```sh
mkdir -p run/audit
# 1) 由 fixtures 生成一份规范 v2 报告（N=9）。真实场景可直接用
#    run/election/reports/revision-<r>.json。
.venv/bin/python - <<'PY'
import hashlib, json
from ranked_ballots import analyze, canonical_bytes
data = json.load(open("fixtures/input.json"))
ledger = {"candidates": sorted(data["candidates"]),
          "ballots": sorted(data["ballots"], key=lambda b: b["id"])}
report = {"schema_version": 2, "revision": 0,
          "candidates": ledger["candidates"], "ballots": ledger["ballots"],
          "snapshot_sha256": hashlib.sha256(canonical_bytes(ledger)).hexdigest(),
          "result": analyze(ledger)}
open("run/audit/report.json", "wb").write(canonical_bytes(report))
PY

# 2) 物理清单合计必须恰为 N=9；箱序在内部按 id 升序规范化。
cat > run/audit/manifest.json <<'JSON'
{"containers":[{"id":"box-b","count":4},{"id":"box-a","count":5}]}
JSON

# 3) start：固定报告、清单、seed、制度与预算；只建新文件。
.venv/bin/ranked-ballots-audit start \
  --report run/audit/report.json \
  --manifest run/audit/manifest.json \
  --seed 00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff \
  --risk 1/4 --max-draws 6 --session run/audit/session.json
# {"design":"without_replacement","consumed":0,"verdict":"continue", ...}

# 4) draw：发出两个物理位置，展开箱号与箱内 1 基位置。
.venv/bin/ranked-ballots-audit draw --session run/audit/session.json --count 2

# 5) 先解释第 2 抽（第 1 抽仍缺）—— consumed 仍为 0，等待缺口补齐。
cat > run/audit/obs-2.json <<'JSON'
{"observations":[{"draw_index":2,"ranking":["B"]}]}
JSON
.venv/bin/ranked-ballots-audit record --session run/audit/session.json \
  --source run/audit/obs-2.json
# {"consumed":0,"reason":"awaiting_explanation_of_earlier_draws","verdict":"continue", ...}

# 6) 再解释第 1 抽，前缀从 1 开始顺序消费；missing 是逐断言最坏下界，
#    空 ranking 不是 missing。
cat > run/audit/obs-1.json <<'JSON'
{"observations":[{"draw_index":1,"missing":true}]}
JSON
.venv/bin/ranked-ballots-audit record --session run/audit/session.json \
  --source run/audit/obs-1.json
# consumed=2；每条断言给出定义/pending|passed/E/y_sum/first_pass_draw/passing_rule

# 7) status：只读重现同一状态（含每抽位置、是否解释、是否消费）。
.venv/bin/ranked-ballots-audit status --session run/audit/session.json
```

活动文件是纯 JSON，内嵌固定报告、清单、seed、制度与已发出抽次；重开时从头重算
判定，原报告/清单文件删除后仍可继续。

### 统计假设与活动字段（摘要）

- 每条预定断言（淘汰轮的严格正差、终轮多数）各跑一个精确有理 e 过程；`E`、`y` 和
  以约分 `numerator/denominator` 字符串输出，阈值用整数交叉相乘判断。
- 无放回制度下 `μ = min(1,(N/2-S)/(N-t+1))`；`S>N/2` 由总体界通过，`μ` 为负表示零
  假设已不可能，不作负乘子。`missing` 对每条断言取最坏 `x=-1`。
- 仅当**全部**预定断言通过才 `certified`；错误路径至少有一条真零假设，故不把 α
  机械除以断言数。有缺口时只能消费从 1 开始的连续前缀，认证在最早停止抽次冻结。
- 活动文件字段：`protocol`、`protocol_version`、`design`、`report_path`、
  `report_sha256`（报告输入字节 SHA256）、`report`、`manifest_path`、
  `manifest_sha256`（规范清单 SHA256）、`manifest`、
  `seed`、`risk`、`max_draws`、`n`、`budget`、`assertions`、`draws`、
  `observations`、`derived`。`derived` 只是可读缓存，读取时必与重算结果完全一致。
- `seed`/SHA 流只保证可复现，不证明外部 seed 随机独立或未被挑选，也不是现实选举
  安全保证；活动文件不承担恶意篡改鉴定。本协议是题面定义的合成抽核，不声称官方
  或法定审计。

### 可直接测试的 Python API

```python
from ranked_ballots import start, draw, record, status, AuditError
start("run/election/reports/revision-1.json", "/tmp/manifest.json",
      "0011...eeff", "1/4", 6, "run/audit/session.json")
draw("run/audit/session.json", 2)
record("run/audit/session.json", "/tmp/obs-1.json")
status("run/audit/session.json")
```

## 扫描转录容量对账 `ranked-ballots-link`

独立的**只读**对账链路：把一份扫描转录清单（只看清票面部分排名位置）与一份固定 v2
电子票档（相同排序聚合成带 weight 的票组）做组合一致性核对。它回答两个问题：这些
转录能否**同时**由给定票档容纳；每条扫描在**全部**合法完整分配中实际可能归属哪些
票组。它不修改票档或抽核活动，不产出计票胜者或统计认证，也不证明扫描来源真实或票档
正确。协议、算法与 Hall 见证依据见 [对账设计](docs/link-design.md)。

### 全局容量语义

- 扫描 `slots` 的长度就是完整 ranking 长度；候选 id 是已读位置，`null` 是该位置
  无法辨读。`null` 不伸缩长度，空数组只与空 ranking 相容。
- 相容当且仅当等长且每个非 `null` 位置相等。每条扫描恰分给一个相容组，每组接收数
  不得超过 weight；**未使用电子容量允许保留**。
- 同 ranking 的不同票组 id 是**不同身份**，不按 ranking 合并，也不丢失组 ID 后借用
  原 count 结果代替关系核对。
- `forced` 只针对**给定相容信息与容量约束**成立；多个可能 id 为 `ambiguous`。
  可能集合覆盖由未用容量产生的替代，而不只是封闭交替环。
- **合法但不相容**（能解析、能核验，但无完整分配）成功输出 `infeasible` 与可独立
  核验的 Hall 见证；**输入损坏**（格式错误、规范字节/摘要/result 不符、超界）退出 2
  并保留旧输出，二者语义不同。

### 可复制示例：取得一份 v2 报告后调用对账

```sh
mkdir -p run/link
# 1) 由归档导出（或自行构造）一份规范 v2 报告。
ranked-ballots-archive export --store run/election --export-id count-2026-11-18
cp run/election/reports/revision-1.json run/link/report.json

# 2) 扫描转录：顶层恰为 {"records":[{"id","slots"},...]}；null 表示该位置不可辨读。
cat > run/link/scans.json <<'JSON'
{"records":[
  {"id":"s1","slots":["A",null]},
  {"id":"s2","slots":["A","B"]}
]}
JSON

# 3) 对账：成功输出 feasible/infeasible，失败退出 2 且保留旧输出。
ranked-ballots-link --report run/link/report.json \
                    --scans  run/link/scans.json \
                    --output run/link/result.json
```

若报告含 `g1=([A,B],weight1)`、`g2=([A,C],weight1)`，全局容量强制
`s1→g2`、`s2→g1`（两条都 `forced`）；只保留 `s1` 时两组皆有未用容量，`s1` 变为
`ambiguous`，可能集合为 `["g1","g2"]`。三条 `["A",null]` 仅邻接这两个单位组时无
完整分配，输出 `infeasible`，见证给出 `record_count=3`、`capacity_sum=2`、
`deficit=1`，且 `ballot_ids` 恰为这两个邻接组。

输出顶层含 `schema_version=1`、`report_sha256`（报告输入字节摘要）、
`scans_sha256`、`snapshot_sha256`、`status`。可行时附覆盖全部扫描的 `assignment`
与按 scan_id 升序的 `records`（`possible_ballot_ids` 升序、去重、非空，
`classification` 为 `forced`/`ambiguous`），`witness=null`；不可行时
`assignment=null`、`records=[]`，`witness` 含 `scan_ids`、`ballot_ids`、
`record_count`、`capacity_sum`、`deficit`。见证仅凭输入即可重建邻接关系复核，
同输入重复运行字节一致。

### 可直接测试的 Python API

```python
from ranked_ballots import link_records, LinkError
payload = link_records("run/link/report.json", "run/link/scans.json")
payload["status"]              # "feasible" 或 "infeasible"
```

## 两两偏好分析 `ranked-ballots-pairwise`

独立的**只读**分析链路：在与 v1 相同的严格 `{candidates, ballots}` 契约上，为
**每个无序候选对**累计精确整数表达权重，输出完整两两矩阵、严格多数有向图、
强连通分量和 Condorcet winner。它不替代 IRV 计票或纸票抽核，不修改归档库与
任何既有结果；输入错误、计算失败、输入/输出同路径或输出为符号链接时退出 2
并保留既有输出字节。语义与实现取舍见 [两两偏好设计](docs/pairwise-design.md)。

### 两两表达权重与缺省语义

- 对票组 ranking `r`、权重 `w`：只有候选对**双方都出现在** `r` 中时，该组才对
  这一对贡献权重，排名靠前者获得全部 `w`；任一方缺失时该组对这一对不贡献给
  任一方。缺失候选**不**解释成末位偏好、平票或弃权后仍计入；空 ranking 不表达
  任何一对；相同 ranking 的不同票组各自贡献自己的 weight。
- 输出 `comparisons` 覆盖全部 C(n,2) 个无序对且每对恰一次（含双方均未表达的
  候选对），按 `(candidate_a, candidate_b)` 字典序排列；每行恰为
  `candidate_a`、`candidate_b`、`a_over_b_weight`、`b_over_a_weight`、
  `unexpressed_weight`、`pairwise_winner`。恒有
  `unexpressed_weight = total_weight - a_over_b_weight - b_over_a_weight`；
  两侧权重相等（含双方为零）时 `pairwise_winner=null`，否则为权重较高一方。
- 所有累计值为精确整数（20000 组 × 10^9 允许合计 2×10^13），不进行浮点换算。

### 严格多数图、强连通分量与 Condorcet winner

- `majority_graph`：`nodes` 为升序候选 ID（孤立候选也在其中），`edges` 按
  `(from, to)` 字典序；仅当某候选对一侧表达权重**严格**大于另一侧时才有从
  较高方指向较低方的边，相等或双方为零没有边。未表达权重、IRV 淘汰顺序与
  候选 ID 顺序均不产生边。
- `strongly_connected_components`：多数图的强连通分量，覆盖每个候选恰好一次；
  分量内按 ID 升序，分量按各自最小 ID 升序；孤立候选单独形成分量。
- `condorcet_winner`：唯一对每个其他候选都有直接严格多数边的候选 ID，否则为
  null（仅一个候选时该候选满足条件）。**它不是 IRV winner**：IRV 胜者由逐轮
  淘汰规则产生，两两多数环（A>B、B>C、C>A）下 IRV 仍有胜者而
  `condorcet_winner=null`；也不要把强连通分量的顺序当作 Condorcet 结论。

### 可复制示例

```sh
mkdir -p run/pairwise
cat > run/pairwise/input.json <<'JSON'
{"candidates":["A","B","C"],
 "ballots":[{"id":"g1","ranking":["A","B"],"weight":4},
            {"id":"g2","ranking":["B","C"],"weight":4},
            {"id":"g3","ranking":["C","A"],"weight":4}]}
JSON
ranked-ballots-pairwise --source run/pairwise/input.json \
                        --output run/pairwise/report.json
# majority_graph.edges: A->B, B->C, C->A；strongly_connected_components:
# [["A","B","C"]]；condorcet_winner: null（IRV 对此输入仍产生胜者 B）
```

同一有效输入重跑字节一致；重排输入 `candidates` 与 `ballots` 不改变规范输出
字节，改变 ranking 中的候选顺序只影响实际表达的 pairwise 方向。

### 可直接测试的 Python API

```python
from ranked_ballots import pairwise_analysis
result = pairwise_analysis({
    "candidates": ["A", "B", "C"],
    "ballots": [{"id": "g1", "ranking": ["A", "B"], "weight": 4},
                {"id": "g2", "ranking": ["B", "C"], "weight": 4},
                {"id": "g3", "ranking": ["C", "A"], "weight": 4}],
})
result["condorcet_winner"]   # None
```

## 候选退出敏感性分析 `ranked-ballots-sensitivity`

独立的**只读**反事实报告链路：在与 v1 相同的严格 `{candidates, ballots}` 契约上，
先用既有 `analyze` 计算基准 IRV 结果，再对**每个候选**建立恰好一个“该候选退出”
场景并复算。它不替代单次计票、归档、抽核或对账，不修改票档、扫描票组分配及任何
既有输出；输入错误、场景/计票失败、输入/输出同路径或输出为符号链接时退出 2 并
保留既有输出字节。

### 场景语义

- 基准场景直接使用**未经修改**的已验证输入调用 `analyze`，因此严格多数、非多数
  时淘汰最低票候选、并列时淘汰候选 ID 字典序最小者、没有继续票时 `winner=null`
  等规则与既有实现完全一致，不另行复制计票规则。
- 每个退出候选 `c` 的场景都从**同一份原始已验证输入**构造，不累积前一个场景的
  变更：从 `candidates` 删除 `c`，从每个 ballot ranking 删除 `c`，保留全部 ballot
  id 与 weight；删除后变空的 ranking **仍保留为票组**，不删票组、不调整权重、不
  改变其余候选的相对顺序。
- 场景按退出候选 ID 升序处理。权重始终按聚合票组计算，**绝不**按 weight 展开成
  单张票；所有轮次、权重和与差值均为精确整数。
- 单候选输入的退出场景候选列表为空；该边界按本题定义返回 `winner=null`、空
  `rounds`、`total_weight=0`。其余场景一律走既有 `analyze`。

### 输出字段

规范 JSON 对象（UTF-8、`ensure_ascii=False`、`sort_keys=True`、
`separators=(',',':')`、恰一个末尾换行）顶层恰为：

- `format`：固定为 `1`。
- `source_sha256`：原始已验证业务输入的规范序列化 SHA256——候选按 ID 升序、票组按
  id 升序、ranking 保持原顺序，规范 JSON 加一个换行，与归档 `snapshot_sha256` 的
  做法一致。因此同一有效输入的 `candidates`/`ballots` 数组重排会得到逐字节相同的
  报告；改变 ranking 内顺序会改变摘要，并按既有计票语义影响结果。报告中的
  `baseline`、每个 `input`/`result`/`changes` 均可独立复算，不依赖该摘要。
- `baseline`：既有 `analyze` 对原输入的完整结果。
- `scenarios`：按退出候选 ID 升序的数组，每项恰为 `withdrawn_candidate`、
  `input`、`result`、`changes`。`input` 是该场景完整的规范业务输入，`result` 是对
  该 `input` 调用 `analyze` 的完整结果；`changes` 恰为 `winner_changed`、
  `winner_before`、`winner_after`、`continuing_weight_delta`、
  `exhausted_weight_delta`，后两者为场景结果最后一轮相应权重减去基准最后一轮相应
  权重，无 `rounds` 时按 0 处理。

报告中不含时间戳、随机值或机器相关字段。

### 可复制示例

```sh
mkdir -p run/sensitivity
ranked-ballots-sensitivity --source fixtures/input.json \
                           --output run/sensitivity/report.json
# baseline.winner = "B"；scenarios 依次为退出 A、B、C 的场景。
```

同一有效输入重跑字节一致；发布时在目标目录临时文件逐场景流式写出，写完 fsync 并
回读校验长度与 SHA256，再以单次 `os.replace` 更新输出。任何读取、校验、场景生成、
计票或写入失败都删除临时文件并保留原输出字节。CLI 成功后向 stdout 输出与文件相同
的规范字节。

### 可直接测试的 Python API

```python
from ranked_ballots import sensitivity_report  # 路径入口，与 CLI 一致

report = sensitivity_report("fixtures/input.json", "run/sensitivity/report.json")
report["baseline"]["winner"]                     # "B"
[s["withdrawn_candidate"] for s in report["scenarios"]]  # ["A", "B", "C"]
```

## 首选票汇总遮盖规划 `ranked-ballots-disclosure`

独立的**只读**规划链路：把 1..8 个选区各自的首选票（空 ranking 计入空白列）
汇总成一张公开表，判定哪些“可额外遮盖”的格必须一并遮盖，才能让每个必遮格在
相容表中仍可变化，并发布**只含公开汇总表、行列合计和遮盖位置**的正式报告。
它不替代任何计票、归档、抽核、对账、两两偏好或敏感性分析；输入错误、规划无解、
输入/输出同路径、输出为符号链接或写入失败时退出 2，stderr 说明原因且不泄露
格值，并保留既有输出字节。设计与判据见 [遮盖规划设计](docs/disclosure-design.md)。

### 输入与汇总语义

- 顶层恰为 `{precincts,protect,optional}`。`precincts` 为 1..8 个
  `{id,candidates,ballots}`，选区 ID 唯一；每区是一份独立的既有 v1 票组输入，
  由同一套规则校验（票组 `id`、`ranking`、`weight` 1..10^9）。各区候选集合相同、
  含 1..5 个候选，全部选区合计至多 20000 个票组；票组 ID 只需在**本区内**唯一，
  不同选区可复用。**禁止**按 weight 展开票组。
- 非空 ranking 的全部 weight 计入首位候选；空 ranking 的全部 weight 计入独立的
  空白列。公开表每选区一行，列为候选 ID 升序后接空白列。
- `protect` 为非空必遮坐标数组，`optional` 为至多 10 项的可选坐标数组；每项
  `{precinct,candidate}`，`candidate:null` 代表空白列。坐标必须存在，两数组各自
  不重复且互不相交。沿用严格 JSON 解析与 8 MB 输入上限。

### 遮盖判据与最优方案

固定一个方案后，公开可见的是未遮盖格的值和全部行列合计；给定必遮格，若相容的
非负整数表能让它取到至少两个不同值，该格即受保护。**不同必遮格可以由不同的相容表
证明**；输入票组内容与权重不是公开约束，替代表只需满足公开格与边际合计。每个方案
都按新方案的可见格重新判断，不沿用上一方案的推断。

在所有允许方案中选额外遮盖最少的；同样少时取排序后坐标序列字典序最小者（坐标按
选区 ID、再按候选 ID 排序，空白列在最后）。判定基于真实表的剩余网络可达性——
只有遮盖格带剩余弧，必遮格可变当且仅当存在经过它的剩余环——**不逐一枚举格值**，
10^9 权重与 1 权重代价相同。若无任何方案合格（例如某行合计为零，其必遮格被迫为
零）则退出 2。

这是**单份发布表**的可推断性指标，不保证抵御外部信息、历史发布或对遮盖选择过程
的推断。

### 输出字段

规范 JSON（UTF-8）顶层恰为 `schema_version:1`、`columns`、`column_totals`、
`rows`、`suppressed`：

- `columns`：候选 ID 升序，末尾 `null`；
- `column_totals`：对齐列的整数数组，含空白列合计；
- `rows`：按选区 ID 排序的 `{precinct,total,values}`，`values` 与列对齐，遮盖处
  `null`，未遮盖格（含零值）为整数；
- `suppressed`：按坐标顺序列出全部遮盖格（格式同输入坐标，不区分必遮与附加遮盖）。

报告不含内部可行表、隐藏格原值或源文件摘要。重排等价输入的选区、候选、票组及
两类坐标数组，报告字节不变。

### 可复制示例

两行 `(1,3,3)` 与 `(3,1,1)`，列为 A、B、空白；强制遮盖 `north/A`。单遮该格仍可
反推，故最优方案额外遮盖 A、B 两列组成的矩形其余三格。

```sh
mkdir -p run/disclosure
cat > run/disclosure/input.json <<'JSON'
{"precincts":[
  {"id":"north","candidates":["A","B"],"ballots":[
    {"id":"n1","ranking":["A"],"weight":1},
    {"id":"n2","ranking":["B","A"],"weight":3},
    {"id":"n3","ranking":[],"weight":3}]},
  {"id":"south","candidates":["B","A"],"ballots":[
    {"id":"s1","ranking":["A","B"],"weight":3},
    {"id":"s2","ranking":["B"],"weight":1},
    {"id":"s3","ranking":[],"weight":1}]}
 ],
 "protect":[{"precinct":"north","candidate":"A"}],
 "optional":[
   {"precinct":"north","candidate":"B"},
   {"precinct":"north","candidate":null},
   {"precinct":"south","candidate":"A"},
   {"precinct":"south","candidate":"B"},
   {"precinct":"south","candidate":null}]}
JSON
ranked-ballots-disclosure --source run/disclosure/input.json \
                          --output run/disclosure/report.json
cat run/disclosure/report.json
```

报告（节选）：`columns` 为 `["A","B",null]`，`column_totals` 为 `[4,4,4]`，
两行合计 `7` 与 `5`；`suppressed` 依次为 `north/A`、`north/B`、`south/A`、
`south/B`，两行的空白列值 `3`、`1` 保持可见：

```json
{"column_totals":[4,4,4],"columns":["A","B",null],
 "rows":[{"precinct":"north","total":7,"values":[null,null,3]},
         {"precinct":"south","total":5,"values":[null,null,1]}],
 "schema_version":1,
 "suppressed":[{"candidate":"A","precinct":"north"},
               {"candidate":"B","precinct":"north"},
               {"candidate":"A","precinct":"south"},
               {"candidate":"B","precinct":"south"}]}
```

### 可直接测试的 Python API

```python
from ranked_ballots import disclosure_analysis, disclosure_report, DisclosureError

report = disclosure_analysis({          # 已解析对象，无 I/O
    "precincts": [...], "protect": [...], "optional": [...],
})
report["suppressed"]                     # 按坐标顺序的 {precinct,candidate}

disclosure_report("run/disclosure/input.json",   # 路径入口，与 CLI 一致
                  "run/disclosure/report.json")
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
- `tests/test_audit.py`：抽核 API 与真实 CLI 子进程测试，覆盖清单排列不影响序列、
  大 N 不展开、跨调用拒绝映射连续、空排序与 missing 的区别、转移后的 active 首选、
  严格差为 0、多个断言在不同前缀首次通过、`μ=0`/`μ>1`/最后一张、阈值恰相等、
  已达预算但仍有缺口、停止后重试/拒绝、重开复算与缓存判定损坏拒绝；数值验收只用
  已实现的无放回制度。
- `tests/test_link.py`：对账 API 与真实 CLI 子进程测试。极小随机图对全部完整分配
  做穷举，独立核验可行分配的逐记录相容与容量、可能集合分类与不可行 Hall 见证；
  覆盖题面强制例（含未用容量替代与严格 Hall 缺口）、同 ranking 不同组身份、空
  ranking、零记录、10^9 容量不展开、被其他记录占满与仍有余量的组、同一连通块内
  并非所有相容边均可用、扫描/票组顺序不影响按 ID 展示、1000+ 扫描的长交替依赖大图
  （可行强制、全歧义与不可行见证）、相容边 200000 上限、格式错误与报告摘要/result/
  规范字节损坏，以及失败保留旧输出、符号链接与同路径拒绝、固定输入字节一致。
- `tests/test_pairwise.py`：两两偏好 API 与真实 CLI 子进程测试。随机实例对照
  独立重算（组合式矩阵 + 互可达分量）全字段一致；覆盖 A>B/B>C/C>A 环同一分量
  且 Condorcet 为 null、单侧/双侧未表达权重、平局（含双方为零）无边且严格多 1
  才出边、多分量图上环之上仍有 Condorcet winner、空票档/空 ranking/单候选/
  weight=10^9 与 2×10^13 精确合计、100 候选 4950 对全覆盖、输入重排字节一致、
  ranking 顺序只翻转表达方向，以及重复排名/未知候选/重复票组 id/布尔或越界
  weight/重复 JSON 键/未知字段拒绝且保留旧输出、同路径与符号链接拒绝。
- `tests/test_disclosure.py`：遮盖规划 API 与真实 CLI 子进程测试。小整数表对照
  独立穷举（枚举全部相容非负整数表、每必遮格可取值、最少且字典序最小的方案）逐格
  核对；覆盖题面两行矩形示例（并验证同样大小的另一方案未被选中）、零行无解、
  多必遮格共享额外遮盖与由不同相容表分别证明、空 ranking 与十亿权重、不同选区复用
  票组 ID、重排字节一致、非法/重复/相交坐标、超限与坏 JSON、无解、同路径、符号
  链接、保留旧输出，以及评估次数不随格值大小增长。
- `tests/test_sensitivity.py`：候选退出敏感性 API 与真实 CLI 子进程测试。每个场景
  由独立 from-spec 重算（原始输入单候选删除后再次调用 `analyze`）对照全字段；覆盖
  至少一个退出改变赢家、另一个退出清空非空 ranking 并增加耗尽权重、退出候选不在
  任何 ranking、空 ranking 保留 weight、weight=10^9 精确差值、单候选输入的空候选
  场景、100 候选 × 20000 聚合计票不展开 weight、输入数组重排字节一致、ranking
  顺序遵循既有 `analyze`，以及非法输入/重复 JSON 键/未知字段/布尔或越界 weight/
  同路径/符号链接/临时写入与替换失败均保留旧输出字节。

`src/ranked_ballots/model.py` 为业务模型，相关算法模块独立组织；`common.py` 提供
受限 JSON 及 v1 报告发布，`archive.py` 实现持久化归档，`audit.py` 实现独立的纸票
抽核活动，`link.py` 实现扫描转录与 v2 票档的容量对账（Dinic 一次最大流 + 残量图
一次 SCC 分类全部相容边，不展开 weight、不枚举完整分配），`pairwise.py` 实现独立
的两两偏好分析（逐票组只累计表达的候选对，由同一份完整矩阵推导严格多数图、强连通
分量与 Condorcet winner），`cli.py` / `archive_cli.py` / `audit_cli.py` /
`link_cli.py` / `pairwise_cli.py` / `sensitivity_cli.py` / `disclosure_cli.py` 衔接七条
数据链路，`sensitivity.py` 以既有 `analyze` 为唯一计票语义，逐候选构造单候选退出
场景，`disclosure.py` 以剩余网络可达性判定首选票汇总表的遮盖可推断性。
fixtures 为明确自制样例，代码与样例采用 MIT 许可。

## 业务规则摘要

为离线计票核验者处理 1..100 个唯一候选人、最多 20000 份唯一 id 排序选票。
ranking 可不完整或为空，但不得重复或含未知候选；weight 为 1..10^9 整数。
每轮计入仍在竞争者中的首选，已无可用候选的票耗尽；多数分母是当轮非耗尽权重，
严格大于半数才胜出。无多数则淘汰最低者一名，最低并列时按候选 ID 字典序最小者
淘汰；此为显式合成规则，不冒充任何地区法定制度。无非耗尽票时 winner=null。
样例首轮 A4/B3/C2，淘汰 C，次轮 B5/A4，B 胜。每轮
continuing+exhausted=total。无外部投票系统、身份校验、选举安全或多席位 STV。
