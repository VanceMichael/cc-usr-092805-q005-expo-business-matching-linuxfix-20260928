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

## 展后供需对接（expo）

路演供需意向交接业务在既有平台能力之上实现：需求/产品均为不可变版本，
匹配只引用明确版本号且只使用双方通过 `disclosures` 授权披露的字段；多渠道
线索按需求身份合并并保留每个来源；会谈在同一事务内占用场地与人员，纪要
双方各自确认，改范围保留旧版本并生成待确认新版本；样品、报价、框架协议、
正式合同、退出按份额由对应责任人推进，业务回执重复到达不再次推进，标识
相同而内容冲突进入主管核对队列；部分成交不关闭剩余机会；样品期限与履约
回访进入持久任务队列，中断恢复后可通过工作清单继续处理，并可从任一成交
反查需求版本、会谈纪要、披露授权与责任交接。

```bash
PYTHONPATH=src python3 -m civicflow.cli --db /tmp/expo-demo.sqlite3 expo-demo
PYTHONPATH=src python3 -m civicflow.cli --db /tmp/expo-demo.sqlite3 expo-worklist
```
