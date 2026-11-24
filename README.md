# 桥岬轨检车班次录制

跟车员按名册采录双缓冲设备的原片。初核员按号领正式测线，次班质检员凭固定班单离线核对原序读值、逐片出处和毫米班账。

## 运行

需要 Python 3.11，仅用标准库。在项目目录运行：

```sh
python3 -B check_project.py
python3 -B demo.py
python3 -B demo.py --rollover
```

`check_project.py` 由运行平台提供。直接运行完整回归的命令为 `python3 -B -m unittest discover -s tests -t . -v`。演示和回归各自使用临时目录，演示输出实际领取及汇总值。

## 公开调用

| 调用 | 结果 |
| --- | --- |
| `shift.open_shift.connect_station(path)` | 打开站端库，幂等初始化表结构。调用方负责关闭连接。 |
| `shift.open_shift.open_shift(db, shift_id, line_ids, unit='mm')` | 固定班次配置。名册按测线标识排序，同一集合的输入顺序不影响等价性。 |
| `device.recording.RecordingDevice(path, recording_id, events, ...)` | 创建或重开设备端口。重开须使用相同的原录制，运行时的查询可用性可变化。 |
| `intake.capture.capture(db, device, shift_id, event)` | 返回原片实收的退出状态，或原封卷实收。停源后可用 `device=None` 重投原封卷或续办已确认的原片。 |
| `intake.resume.resume(db, shift_id, event_id, device=None)` | 按原事件查回已收原件的退出结局或封卷原记录。未知结局返回 `pending`。 |
| `archive.publication.publish(db, shift_id, line_id)` | 核对并提交一条正式原行，返回固定成果和首执。`publication_number(shift_id, line_id)` 可预先得到查询号。 |
| `review.lookup.read(db, number)` | 按成果号或班单号离线领取原序读值、出处、统计及正式成果首执。 |
| `handover.issue.issue(db, shift_id, request_id, line_ids)` | 核对全部申请项后固定班单。同号同集合复用，同号异参拒绝。 |
| `handover.accept.accept(db, number)` | 确认接收，返回固定班单内容和原接收首执。 |

组装设备事件的可运行示例见 `recording_sample.py`。`next_event()` 每次最多公开一个事件，返回 `None` 表示游标已结束或下一事件尚被占槽或封卷前置条件阻塞。可用 `position` 和 `event_count` 区分。调用方保管已经交付的事件身份；进程重开后，`replay_event(event_id)` 可重取仍占槽的原片或已公开的封卷。已经退出的原片只能从站端实收读取。`query_unload(key)` 按原任次查回，退出日志不会因槽位复用而改变。`lost_unload_replies` 可指定在哪些事件的退出首次生效后丢失应答。

## 录制与取件口径

一个班次中的每条测线对应一条逻辑原行。原片是带 SHA-256 校验的 UTF-8 JSON 对象，携带班次、测线、逻辑行、原事件和物理任次，以及片型、起点、行总点数和读值。物理任次由录制号、卷号、槽号及装载号组成。槽号为 0 或 1。读值须为整数毫米或 `null`，位置从零起算，出处区间采用左闭右开。完整普通片覆盖整行，跨卷行由旧卷行首和封卷选定的一片续段严格连续拼接。相同读值的候选按各自物理任次保留。汇总使用整数计算，负值及零均为实测值，缺测保留原位且不参加点数及求和。

设备在本轮列明的候选全部退出后公开原封卷。封卷记录旧卷、原行、候选任次集合以及被选中的任次，允许明确声明无有效续段。采录先提交原字节、应答和归档索引，再向设备请求退出。掉线时按原任次查回，证实仍在原槽时可凭已有实收重试退出。封卷本地实收后，发布可脱离设备。资格按目标测线核定，未签行无正式行或成功首执，独立测线可以继续发布。

无效名册、错属或坏原片、同号异内容以 `ValueError` 拒绝。未知编号以 `LookupError` 拒绝。`publish` 在证据未齐或当前无资格时抛出其子类 `PublicationBlocked`，`status` 标明原因，含 `awaiting_seal`、`no_winner`、`missing_selected`、`invalid_span` 等。SQLite 写入错误由原调用抛出，调用方可使用已实收原件重投。坏片保留源槽；失败发布没有正式行或首执；旧成果始终按原号领取。

普通子集班单可在采录中签发。班末由 `handover.issue.finish_shift_handover(db, shift_id, number)` 以有效同班签发登记终结，保存原名册及口径。终结后可续办已有实收、发布已保全的原行、领取历史成果和班单。次班接收固定的是签发时的成果集合和整数班账，新成果可另行签发。样例范围为单车、单原行跨两卷及本地模拟设备。
