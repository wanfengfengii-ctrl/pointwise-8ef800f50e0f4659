# 桥岬轨检车班次录制

跟车员按名册采录双缓冲设备的原片。初核员按号领正式测线，次班质检员凭固定班单离线核对原序读值、逐片出处和毫米班账。

## 运行

需要 Python 3.11，仅用标准库。在项目目录运行：

```sh
python3 -B check_project.py
python3 -B demo.py
python3 -B demo.py --rollover
```

`check_project.py` 由运行平台提供。直接运行完整回归的命令为 `python3 -B -m unittest discover -s tests -t . -v`。演示和回归各自使用临时目录，演示输出实际领取及汇总值。`demo.py` 在原录制、跨卷和班单之后继续演示测线勘误与班单改签：等值异出处、引用同一旧版的两份不相交勘误、跨原换卷点段以及多线同次签出冲突。

## 公开调用

| 调用 | 结果 |
| --- | --- |
| `shift.open_shift.connect_station(path)` | 打开站端库，幂等初始化表结构。调用方负责关闭连接。 |
| `shift.open_shift.open_shift(db, shift_id, line_ids, unit='mm')` | 固定班次配置。名册按测线标识排序，同一集合的输入顺序不影响等价性。 |
| `device.recording.RecordingDevice(path, recording_id, events, ...)` | 创建或重开设备端口。重开须使用相同的原录制，运行时的查询可用性可变化。 |
| `intake.capture.capture(db, device, shift_id, event)` | 返回原片实收的退出状态，或原封卷实收。停源后可用 `device=None` 重投原封卷或续办已确认的原片。 |
| `intake.retest.receive_retest(db, device, shift_id, event)` | 采录次班复测件（片型 `retest`）。复测件只入实收、不进发布归档，班单终结后仍可办理；返回实收回执和校验后的复测片。 |
| `intake.resume.resume(db, shift_id, event_id, device=None)` | 按原事件查回已收原件的退出结局或封卷原记录。未知结局返回 `pending`。 |
| `archive.publication.publish(db, shift_id, line_id)` | 核对并提交一条正式原行，返回固定成果和首执。`publication_number(shift_id, line_id)` 可预先得到查询号。 |
| `archive.errata.errata_number(shift_id, request_id)` | 预先得到勘误单号 `errata/<shift>/<request>`。 |
| `archive.errata.file_errata(db, shift_id, request_id, base_number, line_id, event_id, start, stop)` | 引用已签班单和复测实收件办理勘误，审核通过后固定勘误单和首执。同号同参复用原结果，同号异参拒绝。 |
| `archive.errata.effective_line_state(db, number, line_id)` | 按签发号领取某版班单中一条测线当前的读值和逐段出处，未替换点仍指向原实收。 |
| `handover.revision.reissue(db, shift_id, request_id, root_number, errata_numbers)` | 同一班单沿版本链改签，多测线勘误同次签出并签出新班单；任一处冲突整单回滚并抛出 `ReissueConflict`。 |
| `handover.revision.get_reissue(db, number)` | 按改签号领回版本链信息、所引勘误、各线状态和复算后的班账。 |
| `review.lookup.read(db, number)` | 按成果号、原班单号或改签号离线领取原序读值、出处、统计及正式成果首执。 |
| `handover.issue.issue(db, shift_id, request_id, line_ids)` | 核对全部申请项后固定班单。同号同集合复用，同号异参拒绝。 |
| `handover.accept.accept(db, number)` | 确认接收，返回固定班单内容和原接收首执。 |

组装设备事件的可运行示例见 `recording_sample.py`。`next_event()` 每次最多公开一个事件，返回 `None` 表示游标已结束或下一事件尚被占槽或封卷前置条件阻塞。可用 `position` 和 `event_count` 区分。调用方保管已经交付的事件身份；进程重开后，`replay_event(event_id)` 可重取仍占槽的原片或已公开的封卷。已经退出的原片只能从站端实收读取。`query_unload(key)` 按原任次查回，退出日志不会因槽位复用而改变。`lost_unload_replies` 可指定在哪些事件的退出首次生效后丢失应答。

## 录制与取件口径

一个班次中的每条测线对应一条逻辑原行。原片是带 SHA-256 校验的 UTF-8 JSON 对象，携带班次、测线、逻辑行、原事件和物理任次，以及片型、起点、行总点数和读值。物理任次由录制号、卷号、槽号及装载号组成。槽号为 0 或 1。读值须为整数毫米或 `null`，位置从零起算，出处区间采用左闭右开。完整普通片覆盖整行，跨卷行由旧卷行首和封卷选定的一片续段严格连续拼接。相同读值的候选按各自物理任次保留。汇总使用整数计算，负值及零均为实测值，缺测保留原位且不参加点数及求和。

设备在本轮列明的候选全部退出后公开原封卷。封卷记录旧卷、原行、候选任次集合以及被选中的任次，允许明确声明无有效续段。采录先提交原字节、应答和归档索引，再向设备请求退出。掉线时按原任次查回，证实仍在原槽时可凭已有实收重试退出。封卷本地实收后，发布可脱离设备。资格按目标测线核定，未签行无正式行或成功首执，独立测线可以继续发布。

无效名册、错属或坏原片、同号异内容以 `ValueError` 拒绝。未知编号以 `LookupError` 拒绝。`publish` 在证据未齐或当前无资格时抛出其子类 `PublicationBlocked`，`status` 标明原因，含 `awaiting_seal`、`no_winner`、`missing_selected`、`invalid_span` 等。SQLite 写入错误由原调用抛出，调用方可使用已实收原件重投。坏片保留源槽；失败发布没有正式行或首执；旧成果始终按原号领取。

普通子集班单可在采录中签发。班末由 `handover.issue.finish_shift_handover(db, shift_id, number)` 以有效同班签发登记终结，保存原名册及口径。终结后可续办已有实收、发布已保全的原行、领取历史成果和班单。次班接收固定的是签发时的成果集合和整数班账，新成果可另行签发。样例范围为单车、单原行跨两卷及本地模拟设备。

## 勘误与改签口径

次班复测可能只补测连续几个点。复测件使用 `retest` 片型，携带与原片相同的班次、测线、逻辑行和行宽，并以自身物理任次和 SHA-256 校验固定为实收证据；复测件不进入 `staged_fragments`，不能直接发布，只能由勘误引用。

勘误单以复测实收件固定对应测线、原任次（物理任次与事件号）、起止点位和原读值，办理时同时引用该件和一张已签班单。审核逐项核对原测线身份（shift/line/row 一致）、点位（行宽一致、申报的左闭右开点段被复测件完整覆盖）和原任次。未完整覆盖申报点段、引用错线或引用未实收证据时整单拒绝（`ErrataRejected.status` 分别为 `span_not_covered`、`wrong_line`、`unreceived_evidence`），不留勘误记录。勘误只替换所申报的点段：相交的原出处在边界处裁开，未替换点保留原读值和原出处，缺测（`null`）原位保留且不计入点数与求和；跨原换卷边界也纯按点位办理。同勘误号同参可领回原结果，异参拒绝；同一复测件不能背两份勘误单。

同一班单的改签沿一条版本链生效：原班单为根，每版改签记录 `root/parent` 与版本号。引用旧版班单办理勘误时，只要该版以来目标点段没有再换过，就可继续沿链改签；目标点在链上已有新的复测出处时报告冲突（`ReissueConflict` 内含受影响测线、点位和当前/来件两份出处依据），哪怕新旧读值完全一致。相邻但不相交的点段互不阻塞；同一批内相交的点段同样冲突。多测线勘误必须同次签出，任一处冲突时整单回滚，当前班单、原首执和已签版本全部保持不变。

改签成功后签出新班单（与原班单同属 `handover/` 号段但号不同）和新的接收首执，并按替换后的读值逐线和全班整数复算统计；原班单和历史改签的读值、出处与首执始终可按原号领取。停源重开后可分别按签发号领取原班单和每一版改签，逐段核回出处；已签入某版的勘误不能再次签入新版本。原正式成果号 `row/...` 永不改变，改签版班单仍引用原成果号，同时在改签条目中保存逐点当前出处。
