# data-qd-152 · 排序选票逐轮计票

Auditable instant runoff counting with exhausted ballots。业务输入、计算和当前边界详见 [数据契约](docs/contract.md)。

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

输入JSON最多8MB，拒绝重复键、非有限数值和未定义字段；源字节SHA256随结果保留。
输出是可独立读取的JSON；成功后原子替换，输入/计算失败退出2并保留旧报告。
输入和输出不得相同，输出符号链接拒绝。单操作者批处理，不承诺并发发布或断电恢复。
同输入重跑不累加。测试使用临时目录，运行产物在忽略的run/。

src/ranked_ballots/model.py 为业务模型，相关算法模块独立组织；common.py 提供受限JSON及报告发布，
cli.py 衔接数据链路。fixtures 为明确自制样例，代码与样例采用MIT许可。

## 当前能力和样例
# Instant-runoff ballot audit v1

为离线计票核验者处理1..100个唯一候选人、最多20000份唯一id排序选票。ranking可不完整或为空，但不得重复或含未知候选；weight为1..10^9整数，表示相同排序票的聚合权重。
每轮计入仍在竞争者中的首选，已无可用候选的票耗尽；多数分母是当轮非耗尽权重，严格大于半数才胜出。无多数则淘汰最低者一名，最低并列时按候选ID字典序最小者淘汰；此为显式合成规则，不冒充任何地区法定制度。无非耗尽票时winner=null。
样例首轮A4/B3/C2，淘汰C，次轮B5/A4，B胜。输出每轮票数、淘汰和耗尽数；每轮continuing+exhausted=total。无外部投票系统、身份校验、选举安全或多席位STV。
