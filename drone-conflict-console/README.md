# 无人机航迹冲突消解台（Drone Conflict Console）

一个**本地运行**的 Web 应用：导入以 **米制三维坐标** 与 **相对起飞秒数** 表示的
分段直线航迹，系统在连续时间上检测冲突，并用 OR-Tools 求出让所有架次
**仅通过整体平移起飞时刻**即可无冲突的整数秒方案；无解时给出包含关系极小的
无解架次集合与互斥时差。支持锁定部分起飞时刻重算、页面对照“最早起飞”与
“求解后”两套航迹，方案可保存并随时回看（保留输入快照）。

---

## 功能一览

- **导入 JSON**：每个架次含固定三维分段直线航迹、整数秒起飞窗口 `[0, window_hi]`、
  正整数优先级权重 `weight`；只允许整体平移起飞时刻。
- **连续冲突检测**：在飞行时间重叠区间内逐时刻检查（含所有航段端点），
  当且仅当
  - 水平距离 `< 安全半径 R`，**且**
  - 垂直距离 `< 安全高度 H`

  时判定为冲突。水平/垂直是“与”的关系（相当于竖直安全圆柱）。
- **精确整数冲突模型**：对每对架次、每对航段，用解析几何
  （垂直条带裁剪 + 水平椭圆柱相交）求出会造成冲突的**相对起飞时差整数集合**，
  得到每对架次的“互斥时差区间”。
- **CP-SAT 求解（OR-Tools）**：选择每个架次的整数秒起飞偏移 `x_i ∈ [0, window_hi]`，
  避开所有互斥时差。求解分三个阶段、**共用同一个墙钟时限预算**，并始终保留当前最佳见证：
  - 阶段 0（可行性探测）：所有架次参与，先拿到任何一份**无冲突可行起飞表**；
  - 阶段 1（第一目标）：**加权总延误** `Σ w_i · x_i`（相对于各自最早起飞时刻）最小；
  - 阶段 2（第二目标）：在不劣于当前最佳目标值的前提下，按**架次 ID 排序的起飞时刻序列**
    **逐坐标字典序最小化**，结果唯一、可复现。

  阶段见证只在“满足此前已证明的全部约束（已证最优目标值、先前已固定的坐标）且本阶段
  指标不变差”时才替换当前候选；某阶段超时（CP-SAT `UNKNOWN`）时回退到已有候选，**绝不
  丢弃阶段 0 已找到的可行表**。仅当加权目标阶段与**每个**字典序轮次都返回 `OPTIMAL` 时，
  结果才标 `optimal`；已找到有效起飞表但优化/字典序证明未完成时返回 `feasible`；
  可行性探测本身超时（既无见证又无证明）返回 `unknown`，与**确实无解**（`infeasible`，
  附极小无解集合）严格区分。字典序轮次若只拿到 `FEASIBLE`，该坐标**不会被当作已证最优
  固定**，只加“不变差”上界并继续后续轮次，因此不会再出现“锁定未证实偏移却标 optimal”。
  所有阶段共享同一个截止时刻（按剩余时间构造每次求解），总耗时不超过请求时限。
- **无解诊断**：无解时列出若干**包含关系极小**的无解架次集合（删去其中任意一架次，
  其余架次即可消解），并给出每对架次的**互斥时差**（后者起飞 − 前者起飞，秒）。
- **锁定重算与超时旧表**：在页面勾选锁定并填写某些架次的整数秒偏移、或改动安全阈值后
  重算，新锁定/阈值会送入冲突约束与求解器。若本次重算返回 `unknown`（超时未证），
  **响应如实报告 `unknown`**：顶层阈值/约束是本次送入的新值，`solution` 即本次的
  `unknown` 结果；SQLite 中已存的可行旧表**不会被覆盖**，并作为 `prior_solution`
  （附旧表求解时的**原阈值**）随响应回带——旧表仍可查看，但不会被当成“本次求解成功”。
  这次“最近一次求解未验证”的状态（连同本次尝试的新阈值/锁定）还会**持久记录在
  SQLite**（`scenarios.latest_solve_status` / `unverified_solve_json`，响应字段
  `unverified_solve`），重启服务、重开页面（`GET /api/scenarios/{id}` 同样回带
  `unverified_solve` 与 `prior_solution`）都不会丢失；页面保留本次锁定与阈值输入，
  地图/时间轴继续画出旧表并标明“旧表 · 原阈值”。
  此时创建执行批次区分“最近一次求解未验证”与“正常可行”：**只有显式选用旧表**
  （`use_stored_plan: true`）**且请求确认的原阈值与库中旧表冻结的原阈值逐一致**
  （`confirmed_safety_radius/height`）才冻结旧输入、原阈值与旧偏移；省略标志、传
  `false`、缺少阈值确认或确认值与库中旧表不一致，一律 `409` 拒绝且**不建批次**
  （原因码分别为 `latest_solve_unverified`、
  `stored_plan_threshold_confirmation_required`、
  `stored_plan_threshold_mismatch`）。**改名/保存旧表（`/save`）不会解除该限制**；
  之后重新求解得到**新的可行解**会清除标记并恢复无标记创建（不带标志即成功）；
  确证 `infeasible` 会落库并无标记但仍因“无可行表”而 `400` 拒绝；从无可行旧表时
  同样拒绝。已冻结的批次不受任何重算影响。页面与直连 API 走同一裁决，既有成功响应
  与已建批次保持不变。**该裁决不在事务外预读**：批次接口在**同一个
  `BEGIN IMMEDIATE` 写事务内**重新读取场景的权威行（可行表、unknown 标记、旧表原
  阈值），把“旧表原阈值确认”和“快照/阈值/偏移冻结写入”绑定在这一次提交里——多进程
  共用同一 SQLite 文件时，即使 `/solve` 在建批次请求的“预读与插入间隙”提交，也不会
  再出现过期裁决：`unknown` 先提交则未显式选用旧表的请求按权威标记 `409` 拒绝且不
  留批次；新的已验证可行解先提交则批次冻结事务内读到的**新表**而非过期旧表；批次先
  提交则保留其提交时有效的快照，交错的求解随后再落库。
- **执行批次与偏差预警**：从已保存的可行方案创建**执行批次**，冻结输入航迹快照、
  安全阈值与计划偏移；之后对原场景如何重算都不会改变批次。各架次以**非负整数秒
  实际起飞偏移 + 修订号**（首版 1、严格递增）上报；同号同值重试幂等，同号异值或
  旧号返回冲突。上报裁决在存储层用**数据库级条件写入**完成，**多个进程共用同一
  SQLite 文件并发上报时也不会发生修订回退**：高修订号先提交则低号得到带
  `current_revision` 的旧号冲突，低号先提交则高号仍可更新，终态恒为最高修订号
  （并发首报同号也按幂等/冲突处理）。给定非负当前秒 `t` 做预警：早于任一已
  上报实际起飞时刻则拒绝；已上报按实际起飞、未上报且计划起飞不早于 `t` 按计划
  起飞、其余标为**逾期**并排除预测（结果标明“预测不完整”）；沿用连续时间冲突
  判定返回 `t` 及之后的冲突区间、首个冲突时刻与已上报架次的偏离计划秒数。
- **预警变更复盘**：选择一次**已接受的上报节点**（幂等重试与被拒绝的旧号不生成
  节点）与非负当前秒 `t`，系统从 SQLite 还原该节点写入**前后两份完整上报状态**，
  用**同一个 `t`** 分别送入连续时间预警，按架次对给出**新增 / 解除**的冲突时间段
  ——前后区间相交时只标记不重叠的变化部分、完全相同则不列出；**仅一侧存在的孤立
  冲突时刻**（区间退化为 `[t, t]`，例如两架次仅在航段端点同一时刻相遇）同样列为
  新增/解除，两侧共有的时刻不误报；同时给出前后首个
  冲突时刻、预测完整性、逾期架次的新增/解除，以及触发上报的架次、修订与各架次
  状态变化。节点与上报写入在**同一个 `BEGIN IMMEDIATE` 事务**内提交，因此跨进程
  共库时“上报写入 ↔ 复盘节点”严格原子、节点顺序就是实际提交顺序；既有批次升级时
  只补一个以**当时可知的最新状态**为内容的基线节点，不补造更早历史。
- **临时作业区开工窗口**：在执行批次中指定一个**竖直圆柱保护区**（水平中心
  `(cx, cy)`、正半径 `r`、上下高度 `z_lo < z_hi`）、非负整数当前秒 `t`、整数
  截止秒 `T` 与正整数作业时长 `d`，系统从 **SQLite 冻结批次与最新上报**还原
  航迹（已上报按实际起飞；未上报且计划起飞不早于 `t` 的按计划；逾期则结果
  **不可确认且不提供安全起点**；`t` 早于任一已上报实际起飞则整次拒绝），在
  **连续时间**上计算每架次分段直线与圆柱相交的占用区间（触及圆柱侧壁/上下平面、
  恰好位于航点时刻、作业闭区间 `[s, s+d]` 与占用只在端点相切都算占用，不做整秒
  采样），排除所有与占用相交的开工秒，返回候选范围 `[t, T-d]` 内的**安全整数秒
  开工起点区间与最早起点**；`T < t`、`d <= 0`、`r <= 0`、`z_lo >= z_hi` 等
  非法参数拒绝；没有候选秒或确无安全起点时区间为空、最早起点为 `null`。修订
  上报后重查、批次重载后重查结果完全一致；批次页与 HTTP API 随
  `docker compose up` 一键启动即可访问。
- **可视化对照**：
  - 水平航迹图（最早起飞虚线 / 求解后实线，冲突见证点用红色 ✕ 与安全圆标出）；
  - 甘特式时间轴（红色段为最早起飞时的冲突时间）；
  - 高度剖面图；
  - 时间游标可拖动，观察各机实时位置。
- **方案留存**：保存方案时连同**输入 JSON 快照**、安全参数与求解结果一并存入
  本地 SQLite，之后可随时打开回看或删除。

---

## 目录结构

```
drone-conflict-console/
├── app/
│   ├── main.py          # FastAPI 路由（含执行批次 / 上报 / 预警 / 作业区开工窗口 API）
│   ├── model.py         # 航迹/航段数据模型
│   ├── geometry.py      # 连续冲突检测 + 互斥时差解析几何
│   ├── solver.py        # OR-Tools CP-SAT 建模、字典序择优、无解核枚举
│   ├── batch.py         # 批次预警纯逻辑：状态分类 + 连续时间冲突投影 + 复盘差分
│   ├── workzone.py      # 临时作业区：连续时间圆柱占用 + 安全整数秒开工窗口
│   ├── schemas.py       # 导入/求解/上报请求的校验
│   ├── storage.py       # SQLite 持久化（场景、方案、批次、上报、复盘节点）
│   └── static/          # 原生 JS + Canvas 前端（无构建步骤）
├── examples/crossroads.json
├── examples/endpoint_meet.json  # 两架次仅在航段端点同一时刻相遇（瞬时冲突复盘演练）
├── scripts/
│   └── verify_multiprocess.py  # 双进程共库：并发上报 + 建批次/求解交错（case E，提交屏障强制顺序）的 HTTP 级验证脚本
├── tests/               # 几何交叉验证 + 求解器暴力验证 + 批次/预警语义 + 建批次/求解交错裁决
├── requirements.txt
├── Dockerfile
└── docker-compose.yml
```

---

## 一键启动（Docker Compose，推荐）

需要安装 Docker 与 Docker Compose（v2）。

```bash
docker compose up --build
```

启动后用浏览器访问：

- **应用页面**：<http://localhost:8000/>
- **重算与旧表选用流程**：在页面导入/载入示例并求解后，改动
  **安全半径/安全高度** 或勾选锁定，再点 **重算（应用锁定）**；若本次
  超时返回 `unknown`，结果面板如实显示 `unknown` 并列出 **已保留的旧表
  （原阈值 R/H）**，阈值输入与锁定保持本次新值（刷新页面仍如此——状态已
  持久化），**执行批次**卡片的按钮变为
  **“选用已保存旧表创建批次（原阈值…）”**——确认对话框展示旧表偏移与原
  阈值，显式确认后请求带 `use_stored_plan: true` 及一致的
  `confirmed_safety_radius/height`，才用旧表及原阈值冻结批次；省略确认
  会被服务端 `409` 拒绝且不建批次。
- **批次页（含临时作业区开工窗口查询）**：<http://localhost:8000/>
  → 打开任意执行批次（或先从可行方案创建批次），在批次详情的
  **临时作业区开工窗口** 卡片填写圆柱参数、截止秒 `T` 与作业时长 `d`
  （当前秒 `t` 沿用上方预警输入），点 **查询开工窗口**。
- 作业区 HTTP API（批次 1 示例）：
  `http://localhost:8000/api/batches/1/workzone?cx=120&cy=0&radius=30&z_lo=15&z_hi=25&t=0&T=30&d=4`
- API 健康检查：<http://localhost:8000/api/health>
- OpenAPI 文档（可选）：<http://localhost:8000/docs>（其中列出 `workzone` 接口）

命令行核对“超时 `unknown` + 旧表保留”契约（极小求解时限可强制 `unknown`）：

```bash
curl -s -X POST http://localhost:8000/api/import \
  -H 'Content-Type: application/json' --data @examples/crossroads.json   # => scenario_id 1
curl -s -X POST http://localhost:8000/api/scenarios/1/solve \
  -H 'Content-Type: application/json' -d '{"time_limit":20}'
# => solution.status "optimal"，offsets {"UAV-A":4,"UAV-B":0,"UAV-C":2,"UAV-D":6}
curl -s -X POST http://localhost:8000/api/scenarios/1/solve \
  -H 'Content-Type: application/json' \
  -d '{"time_limit":0.0001,"safety_radius":45,"safety_height":12,
       "locks":[{"sortie_id":"UAV-B","offset":5}]}'
# => solution.status "unknown"（本次新阈值 45/12 与锁定如实回显，顶层阈值即新值）；
#    unverified_solve 记录本次未验证尝试（45/12、锁定 UAV-B=5，持久落库）；
#    prior_solution 携带旧表 offsets 与原阈值 30/10；SQLite 旧表未被覆盖

# 最近一次求解未验证（unknown）时，批次路由严格裁决，且不建批次：
curl -i -X POST http://localhost:8000/api/scenarios/1/batches \
  -H 'Content-Type: application/json' -d '{"name":"直接凭旧表"}'
# HTTP/1.1 409 Conflict  {"detail":{"reason":"latest_solve_unverified",
#    "stored_plan_original_safety_radius":30.0,
#    "stored_plan_original_safety_height":10.0, ...}}
curl -i -X POST http://localhost:8000/api/scenarios/1/batches \
  -H 'Content-Type: application/json' -d '{"use_stored_plan":true}'
# => 409 stored_plan_threshold_confirmation_required（缺少原阈值确认）
curl -i -X POST http://localhost:8000/api/scenarios/1/batches \
  -H 'Content-Type: application/json' \
  -d '{"use_stored_plan":true,"confirmed_safety_radius":45,"confirmed_safety_height":12}'
# => 409 stored_plan_threshold_mismatch（确认成了本次新阈值，与库中旧表 30/10 不符）

curl -s -X POST http://localhost:8000/api/scenarios/1/batches \
  -H 'Content-Type: application/json' \
  -d '{"name":"旧表批次","use_stored_plan":true,
       "confirmed_safety_radius":30,"confirmed_safety_height":10}'
# => 显式选用旧表且原阈值一致：planned_offsets 仍为 {4,0,2,6}，冻结阈值 30/10（201）
# 改名/保存旧表不解除该限制；之后任意一次可行重算会清除标记，
# 不带任何标志 POST /batches 即恢复 201。
```

数据保存在 Docker 命名卷 `dcc-data`（容器内 `/data/console.db`），
停止/重建容器不会丢失已保存方案。

停止：

```bash
docker compose down          # 保留数据
docker compose down -v       # 同时删除已保存数据
```

### 双进程共用同一数据库（验证并发上报）

默认 `docker compose up` 只启动一个服务进程。若要验证**两个进程同时读写同一份
SQLite**（例如多副本部署或多 worker）时的修订裁决，可加 `multiproc` profile
额外启动第二个进程 `web2`：它挂载同一个 `dcc-data` 卷、使用同一个
`DCC_DB_PATH=/data/console.db`，对外暴露在 **8001** 端口：

```bash
docker compose --profile multiproc up --build
# 进程 1 -> http://localhost:8000   （容器内 /data/console.db）
# 进程 2 -> http://localhost:8001   （同一个文件）
```

然后在宿主机（装有 Python 3 即可，仅用标准库）运行验证脚本，它会分别向
8000 / 8001 两个进程并发提交上报，并交叉核对批次详情、预警与**复盘节点**：

```bash
python3 scripts/verify_multiprocess.py
# 自定义地址： BASE1=http://... BASE2=http://... python3 scripts/verify_multiprocess.py
```

也可以**全程在 Docker Compose 里一键完成**双进程核对：`verify` profile 会
同时拉起 `web` 与 `web2`（同一份数据卷），等两个进程都通过健康检查后，在
`verify` 容器内运行同一脚本（以容器名 `web`/`web2` 互访），脚本的退出码即
整条命令的退出码：

```bash
docker compose --profile verify up --build \
  --abort-on-container-exit --exit-code-from verify
# 退出码 0 = 全部通过；非 0 即脚本失败（日志见 verify 容器输出）
docker compose --profile verify down   # 结束（加 -v 同时清空数据卷）
```

脚本覆盖：修订 3 先提交时，另一进程提交修订 2 返回 `409 stale_revision` 且
`current_revision=3`；修订 2 先提交时，修订 3 仍可更新；两个进程**同时**提交
2 与 3（多轮，含两种提交先后）后，批次详情、页面数据与预警一律使用最高修订 3
的实际偏移；并发首报按同号规则处理（同值一插一幂等）；以及**复盘节点在两个进程
看来完全一致**——只有已接受的写入产生节点、被拒的旧号不产生、节点顺序即实际
提交顺序、任一进程/重复加载对同一节点同一 `t` 的复盘结果逐字节相同、未知节点
返回 404。

**建批次与 `/solve` 交错（case E）**不再靠“多跑几轮随机并发”碰提交顺序，而是
用**提交屏障**确定地控制顺序：每个写接口都在**同一个请求内**先提交 SQLite
写事务、再返回 HTTP 响应，因此**收到响应即代表已提交**——先等第一个请求的
响应、再从另一进程发出第二个请求，就强制了唯一确定的提交顺序，与机器快慢
无关。脚本据此强制并逐一核验三种顺序（任一期望不成立即失败）：

- **unknown 先提交**（E-U1）：先在进程 1 重算出 `unknown`（新阈值 45/12 +
  锁定，极小求解时限）并等响应，再在进程 2 发**无标志**建批次——必须
  `409 latest_solve_unverified` 且**不留批次**；确认成本次新阈值（45/12）
  `409 stored_plan_threshold_mismatch`、缺确认
  `409 stored_plan_threshold_confirmation_required`，均不留批次；只有
  `use_stored_plan:true` 且确认旧表**原阈值**（30/10）才 201 并冻结旧表；
- **批次先提交**（E-B1）：先在进程 2 无标志建批次（201，冻结旧表
  30/10、`{A:4,B:0,C:2,D:6}`）并等响应，再在进程 1 提交 `unknown` 重算——
  已建批次在两个进程读来都仍是旧快照（随后的标记不能追溯修改它），而标记
  落库**之后**再发的无标志请求一律 `409` 且不新增批次；
- **新可行解先提交**（E-F1/E-F2）：新重算用**可区分的第二份方案**——阈值
  换成 25/8 并锁定 `UAV-B=5`，解得 `{A:3,B:5,C:8,D:11}`，阈值与偏移都与旧表
  不同（脚本先断言“可区分”成立，否则核验无意义并明确失败）。新解提交后无标志
  建批次必须冻结**新阈值 25/8 与新偏移**——错冻旧表（30/10、旧偏移）会被立刻
  识破；E-F2 还核验“标记在立 → 新可行解清标记 → 无标志创建恢复并冻新表”；
- **确证无解先提交**（E-I1）：`infeasible` 落库后建批次一律 `400`、不留批次。

此外还有**同时并发**轮次（E-R，屏障同步同刻发请求）作为锁竞争路径的压测：
每轮的提交顺序**从响应反推确认**（409 ⇒ unknown 先提交；201 ⇒ 批次先提交；
可行竞速中冻结的“阈值+偏移”三元组指明哪份表在批次提交时权威），并按所确认
顺序的期望逐轮核验——这些轮次只确认、不承担覆盖，三种顺序的覆盖完全由上面
的强制用例保证，**不靠增加随机轮数声称覆盖**。
结束后 `docker compose --profile multiproc down` 停止两个进程。

---

## 本地直接运行（不使用 Docker）

需要 Python 3.11+。

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

uvicorn app.main:app --host 127.0.0.1 --port 8000
# 或： python -m uvicorn app.main:app --port 8000
```

浏览器打开 <http://127.0.0.1:8000/>。
数据库默认写在 `./data/console.db`，可用环境变量 `DCC_DB_PATH` 改路径。

---

## 使用方式

1. 打开页面，点右上角 **导入航迹 JSON**（或点 **载入示例** 体验
   `examples/crossroads.json`）。
2. 在左侧调整 **安全半径 / 安全高度**（修改后会自动重新求解）。
3. 右侧查看：
   - 水平航迹、时间轴、高度剖面；拖动顶部时间游标看实时位置；
   - 底部结果面板显示是否有解、加权总延误、各架次起飞偏移；
   - 无解时面板列出极小无解架次集合与互斥时差。
4. 如需固定某些架次：在 **架次与锁定** 中勾选“锁定”，填整数秒偏移，
   再点顶部 **重算（应用锁定）**。若本次重算超时返回 `unknown`：
   结果面板如实显示“本次求解超时（unknown）”，本次的锁定与阈值**保留在
   输入中**；已保存的旧可行表继续画在地图/时间轴上，并在结果面板与图例中
   标明“旧表 · 原阈值 R/H”（本次新约束未经验证，不会被当成求解成功）。
5. 点 **保存当前方案** 命名存档；之后在左下 **已保存方案** 中点击即可
   连同输入快照重新载入，🗑 可删除。
6. 得到可行方案后，在 **执行批次与偏差预警** 卡片点 **从当前方案创建执行批次**
   （输入航迹、安全阈值、计划偏移随即冻结）。若上一次重算为 `unknown`（该状态已
   持久记录，刷新/重开页面依旧如此），该按钮变为
   **“选用已保存旧表创建批次（原阈值 R/H）”**：点击后需确认，即**显式选用**
   SQLite 中保留的旧表，请求同时带 `use_stored_plan: true` 与库中旧表
   **原阈值** `confirmed_safety_radius/height`，一致才冻结批次；服务端在其他
   情况下（不带标志 / `false` / 原阈值不一致）一律 `409` 拒绝且不建批次。
   对旧表改名保存不会解除该限制；重新求解得到新可行解后按钮恢复普通创建。
   在批次详情中：
   - 每个架次一行，填写**非负整数秒实际起飞偏移**后点 **上报**，修订号自动取
     “当前 +1”（首版为 1）；同号同值重发安全（幂等），同号异值 / 旧号会弹冲突提示。
   - 填写 **当前秒 t**，点 **评估预警**：状态徽标显示“已上报 / 按计划 / 逾期”，
     面板给出预测是否完整、首个冲突时刻、各已上报架次偏离计划秒数，以及 `t` 及
     之后的连续时间冲突区间。页面与 HTTP API 读写同一批次数据。
   - 在 **预警变更复盘** 下拉框选择一次已接受的上报节点（同号同值重试、被拒绝
     的旧号不产生节点，因此下拉框里只有真正落库的上报；内部基线节点不在页面
     下拉框中，但仍可经 HTTP API 复盘），沿用上方同一个 **当前秒 t**
     点 **复盘（用上方 t）**：面板列出触发上报的架次/修订、前后首个冲突时刻、
     逾期新增/解除、各架次状态变化，以及按架次对分组的“🔺 新增 / 🟢 解除”冲突
     时间段；前后区间相交时只显示真正不同的部分，仅一侧存在的瞬时冲突
     （`[t, t]` 孤立时刻）也会列为新增/解除。
   - 在 **临时作业区开工窗口** 卡片填写圆柱水平中心 `(X, Y)`、正半径、
     上下高度（下界严格小于上界）、截止秒 `T` 与正整数作业时长 `d`
     （当前秒 `t` 沿用上方同一个输入），点 **查询开工窗口（用上方 t）**：
     面板先给出航迹是否可确认（存在逾期架次则标“不可确认”且不提供安全
     起点），再给出候选开工秒范围 `[t, T−d]`、**最早安全起点**、安全整数秒
     开工起点区间、被占用排除的开工秒，以及每架次航迹在圆柱内的连续时间
     占用区间（已裁剪到 `[t, T]`）。触及圆柱边界 / 航点 / 作业起止时刻均算
     占用。上报修订后再次查询会即时刷新，与 HTTP API 结果一致。

---

## 输入 JSON 格式

顶层为一个对象（也可以先用 `examples/crossroads.json` 对照）：

```json
{
  "safety_radius": 30,
  "safety_height": 10,
  "sorties": [
    {
      "id": "UAV-A",
      "weight": 3,
      "window_hi": 20,
      "waypoints": [
        {"x": 0,   "y": 0,  "z": 20, "t": 0},
        {"x": 120, "y": 0,  "z": 20, "t": 12},
        {"x": 240, "y": 0,  "z": 20, "t": 24}
      ]
    }
  ]
}
```

| 字段 | 含义 | 约束 |
| --- | --- | --- |
| `safety_radius` | 水平安全半径（米） | 正数 |
| `safety_height` | 垂直安全高度（米） | 正数 |
| `sorties[].id` | 架次唯一标识 | 非空、不重复 |
| `sorties[].weight` | 优先级权重 | **正整数**，越大延误代价越高 |
| `sorties[].window_hi` | 最晚相对起飞时刻（秒） | 非负整数；起飞窗口为 `[0, window_hi]` |
| `waypoints[].x/y/z` | 米制三维坐标 | 数值 |
| `waypoints[].t` | 相对本架次起飞的航点时刻（秒） | **非负整数，严格递增**，至少 2 个航点 |

说明：相邻航点之间以匀速直线运动相连；导入时每个架次的最早起飞时刻统一记为 0，
求解得到的 `offset` 即“相对最早起飞的整数秒平移量”，最终实际起飞时刻 = 该偏移。

导入接口：`POST /api/import`（页面导入时安全参数以 JSON 内的为准；
后续可在 `/api/scenarios/{id}/solve` 请求体中临时覆盖）。

---

## 求解与无解说说明（建模要点）

- **互斥时差**：对架次 A、B，若 `x_B − x_A` 落入某整数区间 `[lo, hi]` 就会冲突，
  则求解器施加析取约束 `x_B − x_A ≥ hi+1 ∨ x_B − x_A ≤ lo−1`。
- 这些区间来自**连续时间解析几何**（而非秒级采样），保证不漏检；
  代码中的检测对端点与擦边情形按安全侧（落在边界也判冲突）处理。
- **无解核（MUS）**：每个架次带一个“参与”布尔并作为 CP-SAT 假设；
  返回的不可满足核心天然是包含关系极小的。再通过“强制某架次退出”分支，
  枚举出更多极小集合，并附带每对架次的互斥时差。
- 目标采用两阶段优化：先最小化加权总延误（得到 `OPTIMAL` 才固定目标值），
  再按架次 ID 逐个最小化偏移，得到唯一的字典序最优时刻序列。
- **状态与超时回退（三状态区分）**：
  - `optimal`：加权延误阶段**与全部字典序轮次**均被 CP-SAT 证明最优；
  - `feasible`：已找到**经验证无冲突**的可行起飞表（阶段 0 或其后的见证），但目标最优性
    或某个字典序坐标尚未完成证明（如优化阶段超时）；此时给出的偏移表一定可执行，只是不
    保证最优；
  - `infeasible`：可行性探测被证明**确实无解**，返回极小无解架次集合；
  - `unknown`：时限内连可行性都未确定（无见证也无反证）。**超时永远不会覆盖已知可行
    方案**：一次重算返回 `unknown` 时，若 SQLite 中已存有可行方案，旧表保持原样；
    但响应**不再把旧表回填为本次结果**——`solution` 如实是本次的 `unknown`
    （顶层安全阈值/互斥时差也是本次请求送入的新值），旧表改由独立的
    `prior_solution` 字段承载并附**旧表原阈值**。“最近一次求解为 unknown”这一事实
    还会**持久落库**（`latest_solve_status` 标记 + `unverified_solve_json` 中本次
    尝试的阈值/锁定，响应字段 `unverified_solve`）：重启、页面重开（GET 同样回带
    `unverified_solve`/`prior_solution`）、对旧表改名保存都不会解除它。标记期间
    建批次必须**显式选用旧表及原阈值**：
    `POST /api/scenarios/{id}/batches` 带 `use_stored_plan: true` 且
    `confirmed_safety_radius/height` 与库中旧表原阈值一致才冻结旧表（201）；
    省略标志、传 `false`、缺确认或阈值不符都 `409` 拒绝且不建批次。**没有**已存
    可行方案时响应就是单纯的 `unknown`，建批次仍为 `400`；证明了 `infeasible` 的
    重算会正常覆盖旧结果并清除标记；之后一次可行重算保存新表并清除标记，建批次
    恢复“不带标志即可”的旧行为；已冻结的批次不受任何重算影响。

---

## API 摘要

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| `POST` | `/api/import` | 导入航迹 JSON，返回架次/互斥时差/最早起飞冲突 |
| `POST` | `/api/scenarios/{id}/solve` | 按（可覆盖的）安全参数与锁定重算；`unknown` 且有旧表时旧表经 `prior_solution`（附原阈值）回带、本次未验证尝试经 `unverified_solve` 持久标记，`solution` 如实为 `unknown` |
| `POST` | `/api/scenarios/{id}/save` | 命名保存当前方案（仅改名，不改写结果；不会清除 unknown 标记） |
| `GET` | `/api/scenarios` | 已保存方案列表 |
| `GET` | `/api/scenarios/{id}` | 回看某个方案（输入快照 + 求解结果；存在持久 unknown 标记时同样回带 `unverified_solve` 与 `prior_solution`） |
| `DELETE` | `/api/scenarios/{id}` | 删除 |
| `POST` | `/api/scenarios/{id}/batches` | 从可行方案创建执行批次（冻结快照/阈值/计划）。最近一次求解为 `unknown` 且保留旧可行表时，必须 `use_stored_plan:true` 且 `confirmed_safety_radius/height` 与库中旧表原阈值一致，否则 `409`（`latest_solve_unverified` / `stored_plan_threshold_confirmation_required` / `stored_plan_threshold_mismatch`）且不建批次；无可行表仍 `400` |
| `GET` | `/api/batches?scenario_id=` | 批次列表（可按场景过滤） |
| `GET` | `/api/batches/{id}` | 批次详情（冻结计划 + 全部上报） |
| `DELETE` | `/api/batches/{id}` | 删除批次及其上报 |
| `PUT` | `/api/batches/{id}/reports` | 上报某架次实际起飞偏移与修订号（幂等/冲突，多进程共库也按数据库级规则裁决） |
| `GET` | `/api/batches/{id}/warning?t=` | 在非负当前秒 `t` 评估偏差预警 |
| `GET` | `/api/batches/{id}/workzone?cx=&cy=&radius=&z_lo=&z_hi=&t=&T=&d=` | 临时圆柱作业区：返回 `[t,T-d]` 内安全整数秒开工起点区间与最早起点 |
| `GET` | `/api/batches/{id}/report-nodes` | 列出可复盘节点（基线 + 每次已接受的上报写入） |
| `GET` | `/api/batches/{id}/report-nodes/{nid}/review?t=` | 用当前秒 `t` 复盘该节点前后的连续时间预警变化（未知节点/批次返回 404） |
| `GET` | `/api/health` | 健康检查 |

命令行示例：

```bash
curl -X POST http://localhost:8000/api/import \
  -H 'Content-Type: application/json' \
  --data @examples/crossroads.json
```

---

## 执行批次 / 偏差预警：可复现演练

下面所有请求/响应均由本仓库 `examples/crossroads.json` 在 `t` 评估时实跑得到
（`created_at`、`reported_at` 为时间戳，每次运行会不同；冲突数值由连续时间几何
确定，稳定可复现）。先 `docker compose up` 启动服务。

### 1) 导入 → 求解 → 保存 → 建批次

```bash
# 导入
curl -s -X POST http://localhost:8000/api/import \
  -H 'Content-Type: application/json' \
  --data @examples/crossroads.json
# => {"scenario_id":1, ...}

# 求解（得到唯一字典序最优计划偏移）
curl -s -X POST http://localhost:8000/api/scenarios/1/solve \
  -H 'Content-Type: application/json' -d '{"time_limit":20}'
# => solution: {"feasible":true,"status":"optimal","weighted_delay":28,
#              "offsets":{"UAV-A":4,"UAV-B":0,"UAV-C":2,"UAV-D":6}}

# 保存
curl -s -X POST http://localhost:8000/api/scenarios/1/save \
  -H 'Content-Type: application/json' -d '{"name":"crossroads-plan"}'
# => {"scenario_id":1,"name":"crossroads-plan","saved":true}

# 从可行方案创建执行批次（冻结输入航迹 / 安全阈值 / 计划偏移）
curl -s -X POST http://localhost:8000/api/scenarios/1/batches \
  -H 'Content-Type: application/json' -d '{"name":"演练批次-1"}'
```

响应：

```json
{"batch_id":1,"scenario_id":1,"name":"演练批次-1",
 "safety_radius":30.0,"safety_height":10.0,
 "planned_offsets":{"UAV-A":4,"UAV-B":0,"UAV-C":2,"UAV-D":6},"reports":[]}
```

对场景 1 再做带锁定的重算（`locks=[{"sortie_id":"UAV-B","offset":5}]`）会得到新的
计划偏移 `{"UAV-A":4,"UAV-B":5,"UAV-C":10,"UAV-D":11}`，但批次 1 的
`planned_offsets` 仍是 `{4,0,2,6}`——**原方案重算不改变批次**。

### 2) 上报实际起飞（修订协议）

```bash
# 首版修订号必须为 1：UAV-A 实际晚 3 秒起飞（实际偏移 7）
curl -s -X PUT http://localhost:8000/api/batches/1/reports \
  -H 'Content-Type: application/json' \
  -d '{"sortie_id":"UAV-A","actual_offset":7,"revision":1}'
# => {"batch_id":1,"report":{"sortie_id":"UAV-A","actual_offset":7,"revision":1},
#     "accepted":true,"idempotent":false}

# 同号同值重试：幂等
curl -s -X PUT http://localhost:8000/api/batches/1/reports \
  -H 'Content-Type: application/json' \
  -d '{"sortie_id":"UAV-A","actual_offset":7,"revision":1}'
# => ... "accepted":true,"idempotent":true

# 同号异值：409 冲突
curl -i -X PUT http://localhost:8000/api/batches/1/reports \
  -H 'Content-Type: application/json' \
  -d '{"sortie_id":"UAV-A","actual_offset":8,"revision":1}'
# HTTP/1.1 409 Conflict
# {"detail":{"reason":"revision_value_conflict","sortie_id":"UAV-A","revision":1,
#            "stored_actual_offset":7,"received_actual_offset":8}}
```

纠错（更高修订号）与旧号冲突：

```bash
# 修订号 2 纠正为实际偏移 4（回到计划）
curl -s -X PUT http://localhost:8000/api/batches/1/reports \
  -H 'Content-Type: application/json' \
  -d '{"sortie_id":"UAV-A","actual_offset":4,"revision":2}'
# => ... "report":{"sortie_id":"UAV-A","actual_offset":4,"revision":2}

# 再发旧修订号：409
curl -i -X PUT http://localhost:8000/api/batches/1/reports \
  -H 'Content-Type: application/json' \
  -d '{"sortie_id":"UAV-A","actual_offset":4,"revision":1}'
# HTTP/1.1 409 Conflict
# {"detail":{"reason":"stale_revision","sortie_id":"UAV-A",
#            "received_revision":1,"current_revision":2}}
```

约束：`actual_offset`、`revision` 为整数，`actual_offset >= 0`、`revision >= 1`，
否则 `422`；未知批次/架次返回 `404`。

#### 并发与多进程共库的修订裁决

裁决由**存储层在数据库内**完成，而不是“先读当前修订号、再无条件写回”，因此在
**多个进程共享同一个 SQLite 文件**时也正确（进程内的线程锁无法跨进程互斥，
而且 SQLite 的 WAL 快照会让两个并发事务都读到旧值）。`submit_report` 在单个
`BEGIN IMMEDIATE` 事务里只执行**带条件的写**：

- 首报：`INSERT ... SELECT ... WHERE revision = 1 AND NOT EXISTS(该架次的行)`，
  无行且修订号为 1 才插入；
- 更高修订：`UPDATE ... WHERE 入库修订号 < 本次修订号`，仅当现有修订号**严格
  更小**才覆盖；同号写入不会改变既有值。

写条件针对的是“写时刻已提交的行”，所以无论两个重叠事务以何种顺序提交，低修订号
都不可能覆盖高修订号。写后在同一事务内重新读取权威行来判定结果，落败的一方据此
拿到赢家的当前修订号。于是：

- 修订 3 先提交，另一进程提交修订 2 → `409 {"reason":"stale_revision",
  "current_revision":3}`；
- 修订 2 先提交（覆盖 1），修订 3 随后仍可更新；
- 两个进程**同时**提交 2 与 3 → 终态恒为修订 3 的实际偏移；
- 并发首报（都为修订 1）按同号规则处理：同值一“插入”一“幂等”，异值一方
  `revision_value_conflict`。

两次请求结束后，`GET /api/batches/{id}` 批次详情、页面（`/app`）与
`GET .../warning` 预警都只读取这一条最高修订对应的实际偏移。数据库以 WAL 打开，
并设置了 `busy_timeout`，第二个进程与首个进程同时初始化时会在锁上重试。

##### 建批次与 `/solve` 交错：权威状态只在写事务内读取

建批次接口曾经也有“先读、再另起事务插入”的间隙：在事务外读到可行旧表且没有
`unknown` 标记后，另一进程的 `/solve` 若在插入前提交（新锁定/阈值超时产生的
`unknown` 标记，或一份新的已验证可行表/确证无解），旧读值就会变成**过期裁决**——
未选用旧表的请求仍可能 `201`（未验证旧表被当成已验证方案冻结），或新可行解先
提交时批次错冻过期旧表。修复后 `Store.create_batch` 在**同一个 `BEGIN IMMEDIATE`
写事务**内完成全部动作：

1. 取得写锁后读取场景**权威行**（`result_json`、`latest_solve_status`、
   `unverified_solve_json` 与旧表原阈值）；
2. 当场裁决：无可行表（含刚提交的确证无解）→ `400`；标记为 `unknown` → 仅当
   `use_stored_plan=true` 且确认值与事务内读到的旧表原阈值一致才放行，否则
   `409`（三种原因码不变）；
3. 裁决所用的同一行随即在**同一事务**冻结为批次（输入快照、R/H、计划偏移）并
   写入基线复盘节点，一次提交完成。

因此写锁把“建批次提交”与“求解落库提交”**串行化**：`unknown` 先提交时，未显式
选用旧表的请求在事务内看到标记并被拒，**不留批次**；新可行解先提交时，批次冻结
的是事务内读到的新表；批次先提交时保留提交时有效的快照，随后落库的求解既不能
追溯修改也不能撤销该批次。拒绝（`400/409`）在事务内抛出并回滚，绝不会留下半成品
批次；响应状态码、`detail` 字段与成功响应结构均保持不变。该交错的确定性双进程
核对见 `tests/test_batch_solve_race.py`（用一个进程持有未提交写锁，强制另一进程
的批次事务在其提交后才读取权威行，分别覆盖 unknown 先提交、新可行解先提交、确证
无解/无表先提交、改名穿插以及批次先提交四种顺序），HTTP 级核对见
`scripts/verify_multiprocess.py` 的 case E——它利用“写接口先提交、后返回响应”
这一**提交屏障**，用“等响应再发下一请求”强制三种提交顺序逐一核验（新可行解用
阈值与偏移都可区分的第二份方案，错冻旧表必被识破），另以同刻并发轮次从响应
反推确认顺序做锁竞争压测（只确认、不承担覆盖）。

### 3) 评估预警（含拒绝、逾期、不完整标记）

当前秒 `t` 早于任一已上报实际起飞时刻则拒绝（`t` 本身要求非负整数，否则 `400/422`）：

```bash
curl -i "http://localhost:8000/api/batches/1/warning?t=0"
# HTTP/1.1 400 Bad Request
# {"detail":{"reason":"t_before_reported_takeoff","t":0,
#            "violations":[{"sortie_id":"UAV-A","actual_takeoff":4}]}}
```

未上报且计划起飞已严格早于 `t` 的架次被判**逾期**、排除预测，并标记预测不完整
（全新批次，计划为 A=4/B=0/C=2/D=6，`t=3`）：

```bash
curl -s "http://localhost:8000/api/batches/3/warning?t=3"
```

```json
{"t":3,"complete":false,"incomplete":true,
 "overdue_sorties":["UAV-B","UAV-C"],
 "sorties":[
   {"id":"UAV-A","status":"planned","planned_offset":4,"actual_offset":null,
    "revision":null,"takeoff_offset":4,"deviation_seconds":null},
   {"id":"UAV-B","status":"overdue","planned_offset":0,"actual_offset":null,
    "revision":null,"takeoff_offset":null,"deviation_seconds":null},
   {"id":"UAV-C","status":"overdue", "...": "..."},
   {"id":"UAV-D","status":"planned","planned_offset":6,"takeoff_offset":6, "...": "..."}],
 "conflicts":[],"first_conflict_time":null,"reported_deviations":[]}
```

### 4) 偏差引发的连续时间冲突预警

新建批次并上报：A 实际 4（计划 4）、**B 实际 4（计划 0，偏离 +4 秒）**、
C 实际 2（计划 2）；D 未上报。在 `t=4`（D 的计划起飞 6 不早于 4，故按计划预测）：

```bash
curl -s -X POST http://localhost:8000/api/scenarios/1/batches \
  -H 'Content-Type: application/json' -d '{"name":"演练批次-2（含偏差）"}'
# 批次 id=2
curl -s -X PUT http://localhost:8000/api/batches/2/reports -H 'Content-Type: application/json' \
  -d '{"sortie_id":"UAV-A","actual_offset":4,"revision":1}'
curl -s -X PUT http://localhost:8000/api/batches/2/reports -H 'Content-Type: application/json' \
  -d '{"sortie_id":"UAV-B","actual_offset":4,"revision":1}'
curl -s -X PUT http://localhost:8000/api/batches/2/reports -H 'Content-Type: application/json' \
  -d '{"sortie_id":"UAV-C","actual_offset":2,"revision":1}'
curl -s "http://localhost:8000/api/batches/2/warning?t=4"
```

响应（节选）：

```json
{"t":4,"complete":true,"incomplete":false,"overdue_sorties":[],
 "first_conflict_time":9.942324,
 "sorties":[
   {"id":"UAV-A","status":"reported","planned_offset":4,"actual_offset":4,
    "revision":1,"takeoff_offset":4,"deviation_seconds":0},
   {"id":"UAV-B","status":"reported","planned_offset":0,"actual_offset":4,
    "revision":1,"takeoff_offset":4,"deviation_seconds":4},
   {"id":"UAV-C","status":"reported","planned_offset":2,"actual_offset":2,
    "revision":1,"takeoff_offset":2,"deviation_seconds":0},
   {"id":"UAV-D","status":"planned","planned_offset":6,"actual_offset":null,
    "revision":null,"takeoff_offset":6,"deviation_seconds":null}],
 "conflicts":[
   {"sortie_a":"UAV-B","sortie_b":"UAV-C",
    "time_start":9.942324,"time_end":12.519215,"witness_time":11.230769,
    "a_pos":[120.0,12.307692,22.0],"b_pos":[110.769231,6.153846,24.0],
    "horizontal_distance":11.094004,"vertical_distance":2.0},
   {"sortie_a":"UAV-B","sortie_b":"UAV-D",
    "time_start":12.335899,"time_end":15.664101,"witness_time":14.0,
    "a_pos":[120.0,40.0,22.0],"b_pos":[120.0,40.0,18.0],
    "horizontal_distance":0.0,"vertical_distance":4.0}],
 "reported_deviations":[
   {"sortie_id":"UAV-A","planned_offset":4,"actual_offset":4,"revision":1,"deviation_seconds":0},
   {"sortie_id":"UAV-B","planned_offset":0,"actual_offset":4,"revision":1,"deviation_seconds":4},
   {"sortie_id":"UAV-C","planned_offset":2,"actual_offset":2,"revision":1,"deviation_seconds":0}]}
```

冲突只取 `t=4` 及之后的连续时间区间；当 `t` 落在某区间内部时，`time_start` 会被
裁剪到 `t`、`witness_time` 与见证位置相应重算，`first_conflict_time` 同步为裁剪后
的起点；区间整体早于 `t` 时不再返回。**纠错后重算**（对 B 再发更高修订号）或
**批次重载**（`GET /api/batches/2` 后重新 `warning`）结果完全一致——预警是冻结
批次与已存上报的纯函数。

### 5) 预警变更复盘（按已接受的上报节点）

每次**真正改变状态的上报写入**（首报插入、或更高修订覆盖）都会在数据库里留下
一个**复盘节点**；同号同值的幂等重试、同号异值冲突、旧号（`stale_revision`）与
非法首版（`invalid_first_revision`）一律**不生成节点**。节点与对应上报写在
**同一个 `BEGIN IMMEDIATE` 事务**里提交，节点内容包含该次写入**前/后两份完整
上报状态快照**：

- 因此跨进程共用同一个 SQLite 文件时，“上报写入 ↔ 复盘节点”严格**原子对应**，
  不可能看到“上报已提交但节点缺失”或反之；
- 节点在批次内按提交顺序连续编号（基线为 1，随后 2、3、…），**并发升版时复盘
  顺序就是事务实际提交顺序**（若低号在高号之后提交，它本就会被判旧号而不产生
  节点）；每个节点的 `before` 恰好等于上一节点的 `after`；
- 重新打开批次/换一个进程读取，节点链与复盘结果**完全一致**（快照是冻结的）。

列出节点：

```bash
curl -s "http://localhost:8000/api/batches/2/report-nodes"
```

```json
{"batch_id":2,"nodes":[
  {"node_id":4,"seq":1,"is_baseline":true,"trigger_sortie_id":null,
   "trigger_actual_offset":null,"trigger_revision":null,"created_at":...},
  {"node_id":5,"seq":2,"is_baseline":false,"trigger_sortie_id":"UAV-B",
   "trigger_actual_offset":3,"trigger_revision":1,"created_at":...},
  {"node_id":6,"seq":3,"is_baseline":false,"trigger_sortie_id":"UAV-B",
   "trigger_actual_offset":4,"trigger_revision":2,"created_at":...}]}
```

对其中一个节点，用与预警相同的非负当前秒 `t` 复盘（下例是把 B 从实际 3 秒
纠到 4 秒的那次修订）：

```bash
curl -s "http://localhost:8000/api/batches/2/report-nodes/6/review?t=4"
```

响应（节选）给出前后**首个冲突时刻**、**预测完整性**、**逾期集合变化**、触发
上报的架次与修订、各架次状态变化，以及按架次对分组的新增/解除时间段；每段都带
见证时刻与双方位置，`change` 取 `added`/`removed`：

```json
{"batch_id":2,"node":{"node_id":6,"seq":3,"is_baseline":false,
   "trigger_sortie_id":"UAV-B","trigger_actual_offset":4,"trigger_revision":2},
 "review":{"t":4,
   "node":{"sortie_id":"UAV-B","actual_offset":4,"revision":2,"baseline":false},
   "first_conflict_time_before":9.692308,"first_conflict_time_after":9.942324,
   "complete_before":true,"complete_after":true,
   "overdue_before":[],"overdue_after":[],
   "overdue_added":[],"overdue_removed":[],
   "pair_changes":[
     {"sortie_a":"UAV-B","sortie_b":"UAV-C",
      "added":[{"change":"added","time_start":12.0,"time_end":12.519215,
                "witness_time":12.259607,...}],
      "removed":[{"change":"removed","time_start":9.692308,"time_end":9.942324,
                  "witness_time":9.817316,...}]},
     {"sortie_a":"UAV-B","sortie_b":"UAV-D",
      "added":[{"change":"added","time_start":15.0,"time_end":15.664101,...}],
      "removed":[{"change":"removed","time_start":12.093492,"time_end":12.335899,...}]}],
   "sortie_changes":[{"id":"UAV-B","changed":true,
     "status_before":"reported","status_after":"reported",
     "takeoff_offset_before":3,"takeoff_offset_after":4,
     "revision_before":1,"revision_after":2,
     "deviation_seconds_before":3,"deviation_seconds_after":4}, ...],
   "warning_before":{ /* t=4 时节点前状态的完整预警，同 GET .../warning 结构 */ },
   "warning_after":{ /* t=4 时节点后状态的完整预警 */ }}}
```

差分规则与边界：

- **相交只标变化部分**：对每个架次对，把前后两套冲突区间分别取并集后做闭区间
  减法，`added = 后 − 前`、`removed = 前 − 后`。因此前后重叠的区间只冒出不重叠
  的“头/尾”，完全相同的区间不产生任何条目；两者只在单个端点相切时，共有的那一
  时刻不算变化。
- **孤立时刻也算变化**：冲突区间可能退化为单个时刻 `[T, T]`（例如一次上报使
  两架次仅在航段端点同一时刻相遇）。仅一侧存在该时刻时，复盘把它列为新增或
  解除的**点区间**（`time_start == time_end`，见证时刻即该点）；两侧共有同一
  时刻（含与另一侧区间的端点重合）不算变化，普通重叠区间仍只显示变化部分。
- **前后用同一个 `t` 评估**，两段都只考虑 `t` 及之后；逾期架次在对应那一侧照常
  排除并反映到完整性与 `overdue_added/removed`。
- **整次拒绝**：只要 `t` 早于**任一侧**（前或后）任一已上报的实际起飞时刻，复盘
  返回与预警一致的 `400 t_before_reported_takeoff`（`violations` 合并两侧去重），
  **不返回任何差异**。
- **未知节点返回 404**（未知批次同样 404）；`t` 非负，否则 `400`。
- **既有批次（升级前创建）**：升级后第一次打开数据库时，迁移会为每个还没有节点
  的批次补**唯一一个基线节点**，其前/后快照都取“升级时可知的最新上报状态”，
  **不补造更早记录**；全新批次的基线节点则是空状态，随批次在同一事务创建，可在
  列表中选择，但它不对应任何触发上报。复盘不改变任何上报或批次数据，可安全
  反复调用，重复调用结果一致。

### 6) 瞬时（孤立时刻）冲突的复盘核对

`examples/endpoint_meet.json` 中两架次同速向东：UAV-A 的航段在自己的 `t=10`
到达 `(100, 0, 20)` 并结束，UAV-B 的航段在自己的 `t=0` 从同一点出发。因此当
B 的实际起飞恰好比 A 晚 10 秒时，两段飞行窗口只在**一个时刻**（全局 `T=10`）
相接，两机在该瞬间同处一点——冲突区间退化为 `[10, 10]`。下面命令在全新部署
上依次执行（ID 以实际返回为准），可核对“最新预警列出 `[t,t]`，复盘也把该孤立
时刻列为新增/解除”：

```bash
# 导入 -> 求解（计划 {UAV-A:0, UAV-B:0}，两机全程相距 100 米，无冲突）-> 建批次
curl -s -X POST http://localhost:8000/api/import \
  -H 'Content-Type: application/json' --data @examples/endpoint_meet.json
# => {"scenario_id":1, ...}
curl -s -X POST http://localhost:8000/api/scenarios/1/solve \
  -H 'Content-Type: application/json' -d '{"time_limit":20}'
# => solution.offsets {"UAV-A":0,"UAV-B":0}, weighted_delay 0
curl -s -X POST http://localhost:8000/api/scenarios/1/batches \
  -H 'Content-Type: application/json' -d '{"name":"瞬时相遇演练"}'
# => {"batch_id":1, ... "planned_offsets":{"UAV-A":0,"UAV-B":0}}

# 上报：A 按计划 0 起飞；B 实际 10（计划 0，偏离 +10 秒）
curl -s -X PUT http://localhost:8000/api/batches/1/reports \
  -H 'Content-Type: application/json' \
  -d '{"sortie_id":"UAV-A","actual_offset":0,"revision":1}'
curl -s -X PUT http://localhost:8000/api/batches/1/reports \
  -H 'Content-Type: application/json' \
  -d '{"sortie_id":"UAV-B","actual_offset":10,"revision":1}'

# 当前秒 t 恰在相遇时刻：最新预警列出 [10, 10]
curl -s "http://localhost:8000/api/batches/1/warning?t=10"
```

```json
{"warning":{"t":10,"complete":true,"first_conflict_time":10.0,
  "conflicts":[{"sortie_a":"UAV-A","sortie_b":"UAV-B",
    "time_start":10.0,"time_end":10.0,"witness_time":10.0,
    "a_pos":[100.0,0.0,20.0],"b_pos":[100.0,0.0,20.0],
    "horizontal_distance":0.0,"vertical_distance":0.0}], "...": "..."}}
```

```bash
# 复盘 B 的那次上报节点（节点列表中 trigger_revision=1、trigger_sortie_id=UAV-B，
# 全新部署上为 node_id=3）：孤立时刻 [10,10] 进入“新增”
curl -s "http://localhost:8000/api/batches/1/report-nodes"
curl -s "http://localhost:8000/api/batches/1/report-nodes/3/review?t=10"
```

```json
{"review":{"t":10,
  "first_conflict_time_before":null,"first_conflict_time_after":10.0,
  "overdue_removed":["UAV-B"],
  "pair_changes":[{"sortie_a":"UAV-A","sortie_b":"UAV-B",
    "added":[{"change":"added","time_start":10.0,"time_end":10.0,
              "witness_time":10.0,"a_pos":[100.0,0.0,20.0],
              "b_pos":[100.0,0.0,20.0],"horizontal_distance":0.0,
              "vertical_distance":0.0,"...":"..."}],
    "removed":[]}], "...": "..."}}
```

（节点前 B 计划 0 已早于 `t=10`，故前侧 B 为逾期、预测不完整；上报后 B 以实际
10 参与预测，既解除逾期又引入该瞬时冲突。）纠错场景同样覆盖：再发
`{"sortie_id":"UAV-B","actual_offset":0,"revision":2}` 把 B 纠正回计划，新节点
的复盘（`t=10`）在 `removed` 中列出 `[10.0, 10.0]` 且 `added` 为空；而
`t=9` 早于 B 已上报的实际起飞 10，复盘整次拒绝（`400
t_before_reported_takeoff`）。页面上打开该批次、在 **预警变更复盘** 选择对应
节点并沿用同一 `t`，可看到同样的 “🔺 新增冲突 [10.0, 10.0] s”。

### 7) 临时作业区开工窗口（连续时间圆柱占用）

接着使用前面的**批次 1**（`crossroads`，计划 A=4/B=0/C=2/D=6，全新批次无上报）。
在十字路口中心 `(120, 0)` 设一个半径 30 m、高度 15–25 m 的竖直圆柱保护区，
当前秒 `t=0`、截止秒 `T=30`、作业时长 `d=4` 秒（作业闭区间 `[s, s+4]` 必须在
`T=30` 前结束，因此候选开工秒为 `[0, 26]`）：

```bash
curl -s "http://localhost:8000/api/batches/1/workzone?cx=120&cy=0&radius=30&z_lo=15&z_hi=25&t=0&T=30&d=4"
```

响应（节选）给出各架次**在圆柱内的连续时间占用区间**、被占用排除的开工秒、
**安全整数秒开工起点区间与最早起点**：

```json
{"workzone":{
  "zone":{"center_x":120,"center_y":0,"radius":30,"z_lo":15,"z_hi":25},
  "t":0,"T":30,"duration":4,
  "candidate_window":{"start_lo":0,"start_hi":26,"has_candidates":true},
  "confirmable":true,"incomplete":false,"overdue_sorties":[],
  "occupancy":[
    {"sortie_id":"UAV-A","status":"planned","takeoff_offset":4,
     "occupied_intervals":[{"time_start":13.0,"time_end":19.0}]},
    {"sortie_id":"UAV-B","status":"planned","takeoff_offset":0,
     "occupied_intervals":[{"time_start":3.0,"time_end":9.0}]},
    {"sortie_id":"UAV-C","status":"planned","takeoff_offset":2,
     "occupied_intervals":[{"time_start":9.919874,"time_end":14.080126}]}],
  "forbidden_start_intervals":[[-1,19]],
  "safe_start_intervals":[[20,26]],
  "earliest_safe_start":20}}
```

占用由分段直线与圆柱在**连续时间**上求交得到（同时满足水平距离 `≤ r` 且高度在
`[z_lo, z_hi]` 内）：例如 B 在全局 3–9 s 位于圆柱内，任何 `[s, s+4]` 与 `[3,9]`
相交的开工秒（`s ∈ [−1, 9]`）都被排除；三架次占用合并后覆盖到 19 s，故安全
起点为 `[20, 26]`、最早 **20 s**。这些数值由冻结航迹与计划偏移解析确定，稳定
可复现。边界规则：

- **闭区间相切也算占用**：作业区间与占用只在端点相接（`s + d = 占用起点` 或
  `s = 占用终点`）时该开工秒同样排除；航迹**触及圆柱侧壁或上下平面**、占用退化为
  单个时刻（如擦边、恰在航点时刻进入）也计入；实现带极小容差，按保守（安全）侧
  处理，**不按整秒采样**。
- **占用裁剪到 `[t, T]`**：早于 `t` 或晚于 `T` 的占用不可能与 `[t, T−d]` 内开始
  的作业相交，自动忽略。

逾期与拒绝（与预警同一套状态规则）：

```bash
# 全新批次 t=3：B(计划0)、C(计划2) 已严格早于 t 且未上报 -> 逾期，
# 结果不可确认且不提供任何安全起点
curl -s "http://localhost:8000/api/batches/1/workzone?cx=120&cy=0&radius=30&z_lo=15&z_hi=25&t=3&T=30&d=4"
# => {"workzone":{"confirmable":false,"incomplete":true,
#                 "overdue_sorties":["UAV-B","UAV-C"],
#                 "safe_start_intervals":[],"earliest_safe_start":null, ...}}

# 对批次 2（B 已实际上报 4）在 t=4 查询：已上报按实际起飞、D 按计划(6>=4)，
# 全部可确认；B 的占用随实际起飞平移到 [7,13]，合并占用仍到 19，
# safe_start_intervals=[[20,26]]、earliest=20
curl -s "http://localhost:8000/api/batches/2/workzone?cx=120&cy=0&radius=30&z_lo=15&z_hi=25&t=4&T=30&d=4"

# t=3 早于 B 的已上报实际起飞 4 -> 整次拒绝（与预警一致）
curl -i "http://localhost:8000/api/batches/2/workzone?cx=120&cy=0&radius=30&z_lo=15&z_hi=25&t=3&T=30&d=4"
# HTTP/1.1 400 Bad Request
# {"detail":{"reason":"t_before_reported_takeoff","t":3,
#            "violations":[{"sortie_id":"UAV-B","actual_takeoff":4}]}}
```

非法参数一律 `400`（非整数的 `t/T/d` 由 FastAPI 校验为 `422`），原因码：
`radius_not_positive`（`radius <= 0`）、`invalid_altitude_band`（`z_lo >= z_hi`）、
`t_negative`、`duration_not_positive`（`d <= 0`）、`deadline_before_t`（`T < t`）；
未知批次 `404`。当 `T - d < t` 时没有候选开工秒
（`candidate_window.has_candidates=false`），安全区间为空、最早起点为 `null`；
候选存在但每个开工秒都与占用相交时同样返回空区间与 `null`（确无安全起点）。
对某架次再发更高修订号纠正实际起飞后重查、或重新 `GET /api/batches/{id}`
重载批次后重查，结果逐字节一致；既有 `/warning` 与 `/report-nodes/.../review`
响应不受影响。页面上在批次详情的 **临时作业区开工窗口** 卡片操作，结果与
HTTP API 完全相同。

---

## 测试

测试不依赖容器，在本地虚拟环境中运行：

```bash
pip install -r requirements.txt
python tests/test_geometry_crossval.py     # 几何：解析法 vs 连续扫描 vs 密采样
python tests/test_solver_bruteforce.py 2026  # 求解器：对照暴力枚举（含无解核极小性）
python tests/test_solver_stages.py         # 分阶段见证/超时回退：UNKNOWN 不丢可行表、FEASIBLE 字典序轮不锁未证偏移、全 OPTIMAL 才 optimal、unknown≠infeasible
python tests/test_solve_keeps_plan.py      # 路由/SQLite：超时 unknown 如实上报且旧表经 prior_solution（附原阈值）保留、unknown 持久标记（重启/GET/改名不解除）、批次须显式选用旧表且确认原阈值否则 409 不建批次；可行重算清标记，infeasible 照常落库
python tests/test_batch_solve_race.py      # 双进程共库：建批次与 /solve 交错的确定性裁决（unknown/新可行解/确证无解先提交、批次先提交），拒绝不留批次
python tests/test_batches_warning.py       # 批次：修订协议/多进程共库并发/预警分类与冲突/复盘节点与区间差分
python tests/test_workzone_window.py       # 作业区：圆柱占用解析几何 vs 密采样/闭区间相切/逾期与拒绝/安全窗口
```

HTTP 级双进程共库核验（需先按上文“一键启动”起两个进程，或用
`docker compose --profile verify up --build --abort-on-container-exit
--exit-code-from verify` 一键完成）：

```bash
python3 scripts/verify_multiprocess.py   # 双进程共库：并发上报修订裁决 + 建批次/求解交错（提交屏障强制 unknown/批次/新可行解三种顺序）
```

- `test_geometry_crossval.py` 用三套相互独立的实现（多边形/椭圆柱解析、
  连续时间区间剖分、密采样神谕）对上千个随机航段对与擦边案例做一致性校验；
- `test_solver_bruteforce.py` 在小窗口下穷举所有起飞组合，验证最优目标、
  字典序择优、锁定约束以及无解核的可行性与包含极小性；
- `test_batches_warning.py` 验证批次冻结、首版为 1/严格递增/幂等/冲突、
  **两个 OS 进程共享同一 SQLite 文件时并发上报仍只留最高修订号**（用
  `multiprocessing` 真起两个进程对同一文件做条件写入，含修订 2/3 竞争与
  并发首报），以及预警的拒绝、已上报/按计划/逾期分类、不完整标记、连续时间
  冲突区间向 `t` 投影与“重载结果一致”；复盘部分验证**每次已接受写入恰好生成
  一个节点、幂等/被拒写不生成、节点按实际提交顺序连成快照链、跨进程竞争与重载
  一致**，复盘差分为相交区间只标变化部分、**仅单侧存在的孤立时刻 `[t,t]`
  计入新增/解除而双方共有的时刻不误报**、前后首个冲突时刻、逾期新增/解除、
  任一侧未来起飞即整次拒绝，以及旧批次升级只补一个最新状态基线。
- `test_workzone_window.py` 验证作业区查询的参数拒绝（`T < t`、非正半径/时长、
  `z_lo >= z_hi`、负 `t`）、连续时间圆柱占用与**密采样神谕**一致（含爬升穿高度
  带、转弯、擦边等随机航迹）、触及圆柱侧壁/航点端点产生点占用、**闭作业区间在
  端点相切也排除开工秒**、亚秒级占用不被整秒采样漏掉、已上报/按计划/逾期分类与
  **逾期即不可确认且不提供安全起点**、`t` 早于已上报实际起飞整次拒绝、候选为空
  与确无安全起点均返回空区间/`null`、截止 `T` 裁剪，以及**修订纠正后重查与从
  SQLite 重载后重查逐字节一致**、既有预警响应结构不变。

---

## 备注与边界

- 冲突为严格不等式（`水平 < R` 且 `垂直 < H`）；实现采用极小容差按安全侧处理，
  恰好落在边界的情形会被当作冲突，宁可保守。
- 起飞时刻与航点时刻均为整数秒；航迹在两段之间为连续匀速直线，
  冲突检测本身在连续时间上进行。
- 求解时限可在页面或请求中设置（默认 20 秒）。正常规模下加权延误优化通常
  很快即可得到 `optimal`；优化阶段超时时，先前已找到的无冲突起飞表作为 `feasible`
  返回并写入 SQLite，状态标明“可行但最优性未证完”；若连可行表都未找到则返回
  `unknown`，且**不会覆盖数据库中已有的可行方案**——此时响应的 `solution`
  如实为 `unknown`（顶层阈值为本次请求值），旧表经 `prior_solution`（附旧表
  原阈值）回带供查看，本次未验证尝试经 `unverified_solve` **持久标记**；标记期间
  不会被回填成本次成功结果，建批次也只有“显式选用旧表 + 原阈值确认一致”一条路，
  其余一律 `409` 且不建批次。只有加权目标
  与所有字典序轮次都被证明最优才标 `optimal`。
- 执行批次只允许从**可行方案**创建；批次保存的是独立的输入/阈值/计划快照，
  场景的后续重算、改名或删除不影响已建批次的预警计算（删除场景会级联移除其
  批次，依赖 SQLite 外键 `ON DELETE`）。注意“保存/改名”本身只改名称，既不
  重写旧表也不清除 unknown 标记；标记仅由后续可行求解（或确证无解）清除。
- 预警的“计划起飞不早于 `t`”按 `计划偏移 >= t` 判定（恰在 `t` 起飞仍可预测）；
  逾期架次不参与任何冲突对，因此只要存在逾期架次 `incomplete=true`。已上报架次
  的偏离秒数 = 实际偏移 − 计划偏移（负为提前、正为延误）。
- 临时作业区开工窗口沿用同样的状态分类与拒绝规则：占用在连续时间上对分段直线
  与**闭圆柱**（水平距离 `≤ r` 且 `z_lo ≤ z ≤ z_hi`）求交，边界相切按安全侧
  计入；开工秒 `s` 仅当闭作业区间 `[s, s+d]` 与任一占用时刻**完全不相交**时才
  安全，候选范围为整数秒 `[t, T-d]`（`T-d < t` 即无候选）。逾期架次存在时
  `confirmable=false` 且不返回安全起点；查询是冻结批次与已存上报的纯函数，
  修订重查与重载结果一致。
- 多个进程/副本可共用同一个 `DCC_DB_PATH` 指向的 SQLite 文件：数据库以 WAL 打开、
  设置了忙等待，上报用条件写入仲裁，并发修订不会回退；**建批次同样在写事务内读取
  权威状态并冻结快照**，与 `/solve` 交错时按提交顺序裁决（`unknown` 先提交则未选
  旧表者 `409` 不留批次、新可行解先提交则冻新表、批次先提交则保留当时快照）。可用
  `docker compose --profile verify up --build --abort-on-container-exit
  --exit-code-from verify` 一键起两个进程并自动运行
  `scripts/verify_multiprocess.py`（case E 用提交屏障强制三种提交顺序逐一核验），
  或 `docker compose --profile multiproc up` 起两个进程后手动运行该脚本，或
  `python tests/test_batch_solve_race.py`（确定性持锁交错）验证。复盘节点与上报写在同一事务内提交，
  因此共库并发时节点与写入原子对应、按实际提交顺序复盘，重载结果一致。
