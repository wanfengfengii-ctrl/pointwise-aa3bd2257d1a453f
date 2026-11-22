# 仪器读数复测交班

原卷、布局和普通勘误沿用 README.md。复测只补单项绑定的那次欠测。以下入口沿用 SQLite 库路径，使用 `intake.register.initialise_archive` 初始化或重开，已有卷和正式记录继续可用。

## 开单和查单

新增 `delivery.current.issue_retest(path, volume_id, ticket_id, fields=None)` 和 `delivery.current.read_retest(path, ticket_id)`。卷号、单号为非空字符串，字段选择语义与 `current` 相同。签发在同一正式截面选取缺测项，按原行序和布局字段序编号，允许空单。以相同卷号和等价字段选择重投单号，复领原单。等价以实际选择的字段集合为准。开单不占用字段，同一次欠测可以同时列在多个单里。

开单返回字典，键为 `ticket_id`、`volume_id`、`items`。`items` 为有序元组，每项是字典，含 `item_id`（本单从 1 起的整数）、`row_id`、`field_name`、`opened`（`FieldProvenance`）。`opened` 指向本次连续欠测的起点，可来自封存原件或首次使该字段进入本次欠测的正式勘误。连续正式签入 `MISSING` 沿用该起点。

查单返回相同结构，并给每项增加 `status` 和 `closed_by`。整单从同一读截面取得处置。`closed_by` 是首次将本次欠测签成实值的 `FieldProvenance`，待办时为 `None`。后续签发不会改写旧项的结案出处。

| `status` | 状态 |
| --- | --- |
| `pending` | 该次欠测仍待办 |
| `filled` | 正式回传实际使用了此单项 |
| `superseded` | 同次欠测已由另一单项的回传补值 |
| `corrected` | 普通勘误已正式填入实值 |

## 分批回传与复领

新增 `corrections.request.return_retest(path, ticket_id, return_id, items, *, hook=None)`。`items` 是非空 `(item_id, reading)` 序列，项号为本单的正整数，同一项仅出现一次。读数取布局允许的原刻度非缺测整数，零有效，保留 V1 对布尔、缺测码和越界读数的校验。回传号使用全库的勘误请求号空间，已绑定的号码在普通勘误和复测回传之间互斥。回传按单号及项号到读数的映射比较参数，忽略排列。已知回传号同参续办或复领原结果，异参抛 `ConflictError`。

新回传号先校验全部项。任一项已结案时整批拒收，不做字级预写或发布。返回字典 `{'status': 'rejected', 'ticket_id': ticket_id, 'return_id': return_id, 'blocked': (...)}`。`blocked` 按单项顺序列出拦截项，各含 `item_id`、`status`、`closed_by`。拒收回执按原号复领，即使当时未拦截的其他项后来也已结案。

全批符合结案资格时，沿用 V1 字级预写、字段占用和成组发布。活动占用按原规则抛 `FieldsBusy`，忙或非法输入不绑定新回传号。未知单、项、卷或字段沿用 `NotFound`，其他非法参数沿用 `ValueError`。只有正式发布才同时确认本批核销。预写期间仍可开单和读正式值，同字不相交的普通勘误可照常发布。已受理的回传可用原回传号调用 `corrections.recover.resume` 续成或撤销。预写和撤销均不核销。回传成功返回 V1 `CorrectionReceipt`，已撤销同号返回 V1 `CancelledCorrection`。拒收回执从回传入口领取。重开、发布失答及后来有新勘误时，已完成的回传仍可同参复领。`hook` 沿用 V1 的字提交和发布事件，包括 `after_word_commit` 和 `after_publish`。

## 从原件到夜班复领

`retest_fixture.seed(path)` 装入样例卷 `night`。首行 `R-20` 的两字为 23 和 7，x、y 欠测，z 为 2。次行 x、y 为 0。`python3 -B retest_fixture.py` 只使用 V1 入口，供核对字值、普通发布序位及失答时序。

1. 选择 y、x 开 P，所得项依布局序为 1 对应 x、2 对应 y，均指原件。普通勘误 `repeat-missing` 把 x 再记为缺测，再为 x 开 P2。P 和 P2 的 x 仍指同一原件欠测。
2. 普通勘误 `ordinary-fill` 填 x=2，再由 `missing-again` 签 x 缺测。为 x、y 开 Q，仅为 x 开 Q2。P、P2 的 x 已由 `ordinary-fill` 结案，Q、Q2 的 x 起于 `missing-again`。y 一直延续原件欠测。
3. 用同一回传批交回 P 的 x=5、y=4，整批拒收，只有 x 列入拦截项且出处为 `ordinary-fill`。y 仍待办。用 Q 另回传 x=0、y=4，在首字提交后失答。此时 Q 两项仍待办，x、y 的现行正式值仍缺测。另单可签 z=6，实物首字为 48，普通改单改 x 则报忙。
4. 重开续成，Q 两项同时已补，Q2 的 x 和 P 的 y 均被替代，三张单各自所列项可查对应出处。两字为 48 和 4。随后再次把 x 签为缺测，复领旧回传结果及各单的旧结案。P 的拒收回执仍只列 x。也可从第 3 步的暂停点另走撤销，两字应为 55 和 7，x、y 未被核销，z=6 保留。

保留全部原回归。沿用现有 .pointwise-runtime.json 接通交班核验和演示，`verify` 执行原回归及复测业务断言，失败时返回非零退出码，`demo` 实际办理并展示再次欠测与复领的结果。