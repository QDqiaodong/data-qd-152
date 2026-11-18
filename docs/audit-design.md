# 合成纸票抽核活动设计（`ranked-ballots-audit`）

本文说明新增的独立抽核活动：它只读一份不可变 v2 计票报告和一份物理箱位清单，
输出待取的物理位置、人工解释、e 过程判定以及 `continue` / `certified` /
`not_certified` 决策。它是核验员把电子计票材料与另行抽取、人工查看的纸票证据
分开管理的工作文件；**不更正电子票档，不修改归档库、任务或发布状态，也不改变
原有 `ranked-ballots` / `ranked-ballots-archive` 入口**。

## 1. 范围与边界

- 单一操作者的本地工作文件（一个 JSON 文件），一次只对一场选举的**一份固定
  v2 报告**做抽核。
- 更新采用“完整重写 + 原子替换”（临时文件、`fsync`、`os.replace`、目录 `fsync`）；
  目的地为符号链接时拒绝；`start` 只创建新文件，绝不覆盖。
- 不要求多写者协调，不新增数据库，不访问网络或外部服务。
- 不提供任何票面更正功能：人工事实一旦登记即不可改，只能以完全相同的事实重试。

## 2. 抽样制度（已选定，事后不可更换）

**实现并固定为无放回均匀抽取（`without_replacement`）。** 每次从尚未发出的物理
位置中均匀抽一个，位置绝不重复。`docs` 与活动头部都记录该字段，`start` 之后不可
变更。

> 未实现：有放回独立均匀抽箱位（`with_replacement`）。两套规则**绝不混用**；
> 活动文件只接受 `design == "without_replacement"`。题面列出的有放回数值验收
> （α=1/4、连续 x=1 第 7 抽通过、E=78125/16384）**不执行**，因为本实现不提供该
> 制度。

### 2.1 随机流

随机块按题面统一规则生成：

```
u = SHA256( seed(32 字节) || counter(16 字节大端无符号) )   # 256 位无符号
counter 从 0 逐块递增
L = 2^256 - (2^256 mod m)            # 当前基数 m 下的接受上界
u >= L 丢弃；否则 k = u mod m
```

当前基数 `m` 是无放回制度下**尚未发出的位置数** `N - t + 1`。`k` 映射为“按原全局
位置排序的剩余位置中的第 k 个（0 基）”。

- 拒绝状态跨 `draw` 调用连续：每次 `draw` 都从固定的 `counter=0` 前缀**重算**整个
  已发出序列，因此不存在“重置 counter”的问题，分批调用永远得到同一前缀。
- `seed` 规范为 64 个小写十六进制字符（输入允许大写，落盘统一小写）。

### 2.2 工作量

`N` 可达 `2×10^13`，实现**从不按 N 或 weight 展开物理票数组**。剩余位置以
“箱 × 剩余数量”的 Fenwick 树表示（≤10000 个箱），单次选择 O(log 箱数 + log 箱容量)，
抽次预算 ≤2000。清单按箱 id 升序连接，箱内 1..count 号位置得到全局 `0..N-1`。

## 3. 输入契约

### 3.1 v2 报告（最多 32 MB）

顶层字段恰为 `schema_version`、`revision`、`candidates`、`ballots`、
`snapshot_sha256`、`result`：

- `schema_version == 2`；`revision` 为非负整数（布尔拒绝）。
- `candidates` 为 1..12 个唯一、升序标识符；`ballots` ≤20000、按 id 升序，
  满足旧 `analyze` 契约（id/ranking/weight，ranking 无重复或未知候选，
  weight ∈ 1..10^9）。
- `snapshot_sha256` = 规范 `{candidates, ballots}` 字节的 SHA256（沿用
  `archive.canonical_bytes`：UTF-8、`ensure_ascii=False`、`sort_keys`、
  无空白、末尾一个换行）。
- `result` 必须逐字节语义等于 `analyze` 对该票档的输出；`winner` 非 `null`；
  `N = result.total_weight` 为正整数。

所有输入都是严格 JSON：拒绝重复键、非有限数、浮点数值、布尔冒充整数、未知字段、
非法标识符。

### 3.2 物理清单（最多 8 MB）

顶层恰为 `{containers:[{id,count},...]}`：1..10000 个唯一箱 id；`count` 为
`1..20000000000000` 的整数；**合计必须恰等于 N**。清单按 id 升序规范化，规范清单
的 SHA256 作为 `manifest_sha256` 随状态保留。清单输入顺序不影响任何抽次序列。

## 4. 断言：把电子 IRV 路径编译成严格正差检验

路径在 `start` 时、任何人工证据之前固定。对每个淘汰轮：

- `active` = 该轮 `tallies` 的全部键；被淘汰者 `e` 与每个其他 active 候选 `c`
  生成一条断言，单张纸票分数
  `x = I(active 内首选为 c) - I(active 内首选为 e)`。
- 终轮（多数轮）为胜者 `W` 生成多数断言
  `x = I(active 内首选为 W) - I(active 内首选为其他人)`。
- 合法纸票在 active 无首选时 `x = 0`；`missing` 对每条断言都取 `x = -1`
  （各自最坏下界，不假设同一张票同时实现所有 -1）。
- 每条断言稳定输出 `id`、`round`、`kind`、`active`、`preferred`、`opposed` 与
  `definition`；id 如 `r1-elimination-C-vs-A`、`r2-majority-B`。

所有严格正差成立足以支持同一 IRV 路径与胜者。原始字典序规则允许的零差淘汰不算
统计通过，因此**正确电子结果也可能无法认证**。

## 5. e 过程（精确有理）

每条断言独立运行一个非负 e 过程：

- `y = (x + 1) / 2`，`E` 从 `1` 开始。
- 第 t 抽指同一已解释连续前缀中的第 t 项（无放回下各位置仅一次）。
- 对尚未通过的断言，以第 t 抽前的 `S`（该断言已消费 `y` 之和）计算
  `μ = min(1, (N/2 - S) / (N - t + 1))`；原始值 >1 取 1；`S = N/2` 时 `μ = 0`
  正常计算；原始 μ 为负表示零假设已不可能，直接按总体界通过，**不作负乘子**。
- 每抽先按该 `μ` 乘因子 `1 + (y - μ) / 2`，再累计当前 `y`。
- 无放回累计 `S > N/2` 时按 `population_bound` 通过；否则 `E ≥ 1/α` 时按
  `threshold` 通过。单张（无放回最后一张）分母为 1。
- 每条通过后冻结其 `E`、`first_pass_draw` 与 `passing_rule`，不再随后续证据变化；
  其他未过断言继续各自更新 `S`。
- 全部断言通过的最早抽次即 `stop_draw`，判定逐抽进行，而非整批末判断。
- `E` 与 `y_sum` 以约分后的十进制 `numerator/denominator` 字符串输出，避免浮点与
  JSON 数值精度损失。阈值比较用整数交叉相乘，不用浮点。

数值自检（无放回制度，本实现实际执行的验收）：N=4、α=1/4、单断言连续 `x=1`：

| 抽次 t | 抽前 S | μ | 因子 `1+(y-μ)/2` | 抽后 E | 抽后 S | 结果 |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | 0 | 1/2 | 5/4 | 5/4 | 1 | 继续 |
| 2 | 1 | 1/3 | 4/3 | **5/3** | 2 | 继续（`S = N/2`，不上界） |
| 3 | 2 | 0 | 3/2 | 5/2 | 3 | `S > N/2` → `population_bound` 通过 |

即：**第 2 抽 `E=5/3`**（并非有放回的 78125/16384），**第 3 抽因总体下界
首次通过**，`stop_draw = 3`。有放回制度“第 7 抽、E=78125/16384”的数值**不在本实现
执行范围内**。

`tests/test_audit.py::NumericAcceptanceTests` 覆盖 `5/3`、`S>N/2` 的总体界通过、
`S = N/2` 的 `μ=0`、`μ>1` 的钳位、最后一张分母、阈值恰相等与零差不通过；这些数字
均由独立参考实现逐抽重算。

## 6. 活动文件与派生视图

活动文件保存足以重开复算的**事实**：

| 字段 | 含义 |
| --- | --- |
| `protocol` / `protocol_version` | `ranked-ballots-audit` / 1 |
| `design` | 固定为 `without_replacement` |
| `report_path` / `report_sha256` | 报告输入路径与输入字节的 SHA256（来源溯源） |
| `report` | 固定的完整 v2 报告（重开复算所需） |
| `manifest_path` / `manifest_sha256` | 清单路径与**规范清单** SHA256 |
| `manifest` | 固定的规范清单 |
| `seed` | 规范化小写 seed |
| `risk` | 约分后的 `{p,q}` |
| `max_draws` / `n` / `budget` | 预算 M、总体 N、有效预算 `min(M,N)` |
| `assertions` | 固定的断言定义 |
| `draws` | 已发出的抽次（编号 1..连续，含箱 id 与箱内 1 基位置） |
| `observations` | 以物理全局位置为身份的不可变人工事实 |
| `derived` | 由事实重算出的缓存视图（`consumed`、`verdict`、每断言状态等） |

每次打开都会：重验报告、清单、断言、`seed`、抽次（重新生成对照固定随机流）、
事实形状与顺序，并**从头重算** `derived`；缓存与之不完全相等即拒绝。因此伪造的
`passed`/`verdict` 缓存不能制造认证，状态总是从事实重新推导。活动**不依赖**
原报告或清单文件继续存在。

## 7. draw / record / status 的消费语义

- `draw --count K`：先拒绝终态活动、拒绝超出剩余有效预算的整次请求，再登记
  K 个位置；随后用已知物理解释解析抽次（无放回下不重复）。已发出序列始终是同一
  固定前缀，分批调用不改变任何位置。
- `record --source JSON`：顶层恰为 `{observations:[...]}`，1..2000 条、`draw_index`
  不重复；每条仅 `{draw_index, ranking}` 或 `{draw_index, missing:true}`；
  `draw_index` 必须已发出；`ranking` 是无重复、无未知候选、可空的合法排序；
  没有 `weight`。它记录对应物理位置的唯一事实；同位置经不同 `draw_index` 录入的
  解释必须相同；missing 与 ranking 互斥且不可改；空 ranking ≠ missing。
  任一非法或矛盾项**整批拒绝，不留下部分事实**；完全相同重试幂等。
- 二者都只能消费**从 1 开始无缺口的前缀**，并在最早认证点停止。已接纳但处于停止
  点之后的事实与已发出抽次保留为未消费，不能用于该证书。
- 有缺口时 `consumed` 停在前缀末端；补齐缺口若使全部断言首次通过，则活动在该最早
  前缀停止新增抽次与新事实。
- 判定：全部通过 → `certified`（终态）；未全通过且已消费达有效预算 →
  `not_certified`（终态）；预算已发完但有缺口 → `continue`（等待解释）；
  否则 `continue`（尚可继续）。
- 终态下 `draw` 拒绝、新增/矛盾事实拒绝；**完全相同**的已知事实重放仍成功且不改字节。

`status` 只读并重现同一状态，至少给出：协议版本、源摘要、候选、固定制度/seed/α/N/
有效预算、`draw_count`、不同已解释物理位置数、`consumed`、`verdict`、`stop_draw`
（无认证则 `null`）、每抽的位置/是否已解释/是否已消费；每条断言给出定义、
pending/passed、`E`、已消费 `y` 和、`first_pass_draw` 与 `passing_rule`。

## 8. 命令行与 Python API

```sh
ranked-ballots-audit start  --report V2_JSON --manifest JSON --seed HEX \
                            --risk P/Q --max-draws M --session FILE
ranked-ballots-audit draw   --session FILE --count K
ranked-ballots-audit record --session FILE --source JSON
ranked-ballots-audit status --session FILE
```

成功输出状态 JSON 到 stdout；可解释失败退出 2、在 stderr 给出原因，并保留活动文件
字节。等价的 Python API：

```python
from ranked_ballots import start, draw, record, status, AuditError
start(report, manifest, seed, "1/4", 10, session)
draw(session, 3)
record(session, source)
status(session)
```

## 9. 统计假设与范围（务必阅读）

- 模型：**固定纸票解释、按声明制度理想均匀抽取**。在此条件下，每条断言的零假设
  （全体 N 张真实纸票的 `x` 之和 ≤ 0）的条件均值不高于 `μ`；非负 e 过程可用
  Ville 型可选停界。
- 认证要求**所有预定断言通过**。错误路径至少有一条真零假设，因此不把 α 机械地
  除以断言数（那样会过度保守且不匹配预注册口径）。
- `seed` 与 SHA 流只保证**可复现**；代码**不能**证明外部 seed 随机独立或未被挑选，
  “可复现”不是现实选举安全保证，也不替代任何法定审计或人工监督。
- 活动文件不承担恶意篡改鉴定：它防止误用与缓存伪造，但持有可写副本的攻击者仍可
  重写整个文件。需要更强保证时应把状态摘要离线另行登记。
- 这是题面明确定义的**合成**抽核协议，不声称官方或法定审计。

## 10. 设计取舍

- **只实现无放回**：避免同一物理位置重复计数、简化“同位置唯一事实”的语义；代价是
  拒绝有放回制度及其停止点。规则在文档和活动头部固定，看到人工证据后不能更换。
- **前缀重算而非缓存随机状态**：每次 `draw` 从头重算已发出序列，代价是 O(已发抽次)
  的哈希（≤2000），收益是分批调用天然得到同一前缀、无跨调用状态漂移。
- **事实 + 派生缓存双写并自检**：写入时冗余保存 `derived` 以便人类阅读；读取时永不
  信任它，而是重算并逐项比对。
- **不触碰归档**：抽核完全独立于 SQLite 归档与发布指针，避免把统计判定混入计票
  台账；也意味着抽核不改变、不“更正”任何电子票。
