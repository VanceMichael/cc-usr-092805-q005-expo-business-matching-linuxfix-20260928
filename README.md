# 协同事务平台

协同事务平台为需要多机构参与的业务提供统一的机构、授权、案件、证据、审批、资源预约、资金分录、消息归并、通知、定时任务和历史回放能力。项目只使用 Python 标准库与 SQLite，适合在单个 Linux 应用容器内运行。

## 目录

- `src/civicflow/`：领域服务、SQLite 持久化、权限和命令行入口。
- `tests/`：核心流程、边界条件和异常路径测试。
- `examples/`：本地演示输入。

## 配置

通过 `CIVICFLOW_DB` 指定 SQLite 文件路径；不设置时命令行使用当前目录下的 `civicflow.sqlite3`。所有时间使用带时区的 ISO 8601 字符串。

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 编译或构建

```bash
PYTHONPATH=src python3 -m compileall -q src
```

## 使用

初始化数据库并运行离线演示：

```bash
PYTHONPATH=src python3 -m civicflow.cli --db /tmp/civicflow-demo.sqlite3 demo
```

查看当前案件：

```bash
PYTHONPATH=src python3 -m civicflow.cli --db /tmp/civicflow-demo.sqlite3 list-cases
```

## 展后供需对接（展后专班）

`MatchmakingService`（`src/civicflow/matchmaking.py`）在平台既有能力上实现路演供需意向的可追溯交接：

- **需求与产品版本**：采购商按品类、数量区间、交付地区、认证、时间窗口维护 `match_demands`，参展商以明确产品版本维护 `match_offerings`；两者都走平台版本化实体链，修订追加新版本、历史永不覆盖，回应（`match_responses`）与机会（`match_opportunities`）必须绑定具体版本号。
- **授权披露匹配**：匹配与回应只投影双方通过 `disclosures`（published、未过期、受众覆盖对方机构）授权的字段；未授权的联系人、备注等不会出现在匹配视图中。
- **多渠道线索**：会务、展商、地方交易团回执先经 Inbox 通道幂等（重复回执不再次推进；同来源序号内容不同进入冲突），再按业务指纹合并；`match_lead_sources` 逐源留痕，合并后仍可看到每个来源；字段级冲突写入 `match_lead_conflicts`，由主管 `resolve_lead_conflict` 裁决。
- **会谈与纪要**：`book_meeting` 在单事务内同时占用场地与每位参会人员（任一冲突整体回滚）；纪要按版本保存，买卖双方分别确认；一方改变范围时旧版保留并形成待双方重新确认的新版本。
- **阶段份额**：机会按份额推进样品→报价→框架协议→正式合同（或退出），各阶段由对应责任人负责，交接全程留痕；份额之和守恒，部分成交只关闭对应份额，剩余机会保持开放。
- **恢复与反查**：样品期限与履约回访问作为持久任务与到期点随事务落库，`worklist` 汇总未双方确认纪要、到期事项和崩溃时悬挂的任务租约（`JobQueue.recover_stale` 回收）；`trace_deal` 可从任一成交（份额或合同参考号）反查需求版本、产品版本、披露授权、会谈纪要过程与责任交接。

所有写操作都要求显式 `request_key`：同键同内容返回首次结果，同键不同内容报冲突。

```bash
# 演示包含线索合并→授权匹配→版本回应→会谈纪要→部分成交的完整链路（可重复执行）
PYTHONPATH=src python3 -m civicflow.cli --db /tmp/civicflow-demo.sqlite3 demo
# 中断恢复工作台：未确认纪要、样品期限、履约回访、悬挂任务
PYTHONPATH=src python3 -m civicflow.cli --db /tmp/civicflow-demo.sqlite3 match-worklist
# 从成交反查全链路
PYTHONPATH=src python3 -m civicflow.cli --db /tmp/civicflow-demo.sqlite3 trace-deal --share <share_id>
```
