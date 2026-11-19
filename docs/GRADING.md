# 定等应付领域说明（M8）

面向粮库结算员的到车定等流水。与 M1–M7 的一次性 CSV 批处理相互独立，使用
`DATA_DIR/grading.sqlite`，结论与事件只追加、不改写。

## 1. 合同与等级带

`POST /api/grading/contracts`

| 字段 | 含义 |
| --- | --- |
| contract_id / grain | 合同编号、单一粮种 |
| standard_moisture | 折标标准水分 %，须低于收购上限 |
| basis | `worst_point` 最差点 或 `composite` 整车综合，开秤后不可改 |
| max_moisture / max_impurity | 退收硬线，采信值任一超过即待复验 |
| bands | 等级带，**由严到宽**排列：grade、max_moisture、max_impurity、price(CNY/kg) |

等级带必须嵌套不交叉（后一带的两个阈值都不小于前一带），相邻带阈值不得完全相同，
否则返回 `BAND`。定等时取第一个同时满足水杂阈值的等级；都不满足为等外。
合同带自身指纹，结论记录合同编号与化验指纹，规则可回溯。

## 2. 车结论（最小可复核凭证）

`POST /api/grading/trucks/arrive`，一车一条结论，等级、折标量、应付来自同一次判定：

- points 为抽样点数组（moisture、impurity）。最差点口径取各自最大值；综合口径取算术平均。
- 折标链：净粮=净重×(1−杂质/100)，干物质=净粮×(1−水分/100)，折标重=干物质/(1−标准水分/100)。
- 金额：未舍入=折标重×等级单价；应付=未舍入 HALF_UP 到分，两者都入库并给出舍入尾差。
- 状态：`accepted` 已入仓锁单 / `pending_recheck` 待复验 / `rejected` 退收结案 /
  历史版本通过 `supersedes` 链标记作废，原始行不删除。
- `conclusion_id` 是结论全字段指纹；`assay_fingerprint` 是抽样点规范化指纹。

### 幂等与锁单

| 情形 | 行为 |
| --- | --- |
| 同车再报同一化验（含 "14" 与 "14.0"、键序不同） | 指纹一致，幂等返回原结论，不计第二笔 |
| 已 accepted 的车报新化验 | `409 PAYABLE_LOCKED`，原应付不变，提示走红冲 |
| 待复验车走 arrive 报新化验 | `409 NEED_RECHECK`，引导到复验接口 |
| 已 rejected 的车再报 | `409 TRUCK_CLOSED` |

抽样点**顺序不同视为不同化验**（点位本身有物理含义），不会被当作重试。

## 3. 复验

`POST /api/grading/trucks/recheck`，仅对待复验车开放：

- 同化验重复复验幂等返回，不新增版本。
- 新化验生成 version+1 结论，`supersedes` 指向旧结论号，并写 `CONCLUSION_SUPERSEDE` 事件。
- 复验只能换化验；改仓号或净重返回 `RECHECK_SCOPE`。
- 通过则入仓（同样过容量闸）；仍不合格置 `rejected` 结案，不占仓、不计价。

## 4. 仓批混仓与余量

仓登记 `POST /api/grading/bins`（bin_id、grain、可选 capacity_kg）。每次 accepted 后：

1. 仅汇总当前最新结论为 accepted 的车；待复验/退收/作废版本不占仓。
2. 混仓水杂按在仓车净重加权，用同一合同等级带重算仓批等级，另保留最差单车等级作对照。
3. 写事件：首车 `BIN_MIX_OPEN`；等级未变 `BIN_MIX`；等级被拉低 `BIN_MIX_INVALID`，
   事件负载记录 old/new 等级与新旧余量，原仓批定等因此可证失效。
4. 余量 remaining_kg = 容量 − 在仓净重；无容量登记时为空。超容量到车返回 `BIN_FULL`。
5. 仓与合同粮种不一致返回 `GRAIN_MISMATCH`，系统不做跨粮种混仓。

## 5. 查询页面

`GET /grading` 与下列接口只做查看，不做跨粮种、税费和付款：

- `GET /api/grading/batches/<contract_id>`：本批逐车结论（含历史版本）、仓批等级与余量、
  待复验车号、锁单笔数、应付合计与未舍入合计/尾差。
- `GET /api/grading/bins/<bin_id>`：仓批快照、在仓车清单、混仓失效历史。
- `GET /api/grading/pending`：待复验队列。
- `GET /api/grading/ledger`：全部事件流水（开合同、登仓、到车、作废、复验、混仓）。

## 6. 原因码

`BASIS` 定等口径非法；`BAND` 等级带交叉/重复；`SAMPLE` 抽样点结构问题；
`GRADE` 等外且未超硬线；`LIMIT` 超收购上限；`PAYABLE_LOCKED` 锁单拒改；
`NEED_RECHECK` 须走复验；`TRUCK_CLOSED` 已结案；`RECHECK_SCOPE` 复验越权改单；
`BIN_FULL` 超仓容量；`GRAIN_MISMATCH` 跨粮种；`NOT_FOUND` 合同/仓/车不存在。
