# Phase A — Work Log

目标：让 Hypatia 支持把 compute satellite 作为 TCP 流量目的端点，并跑
一个最小实验验证流量到得了 SAT。

## Step 0 — 环境核对（DONE）

- 工作根目录：`/home/mark/spacesim/`（不是任务文档里写的 `~/hypatia-repro/`，
  原因见根目录的"使用手册.md"——原 `hypatia-repro/` 已迁移）。
- Hypatia commit：`0ac531c313eba2335f6344b46347140c3a0d4230`
- `basic-sim` 子模块：`3b32597c183e1039be7f0bede17d36d354696776`
  （`v0.1-alpha-25-g3b32597`，不要升级）
- venv：`/home/mark/spacesim/venv/`，Python 3.8.10
- `main_satnet` 二进制：
  `/home/mark/spacesim/hypatia/ns3-sat-sim/simulator/build/debug_all/scratch/main_satnet/main_satnet`
- Phase A 工作目录：`/home/mark/spacesim/hypatia/extensions/phase_a/`（已建）
- 已有 baseline：`integration_tests/test_manila_dalian_over_kuiper/` 跑通过，
  输出仍在 `temp/`（reduced Kuiper-630，17 sat + 2 GS，Manila→Dalian，
  TcpNewReno @ 10 Mbps，200 s）。其 fstate 是 100 ms / 200 s 间隔。

## Step 1 — 调研已有 baseline 模板（DONE）

参考 `integration_tests/test_manila_dalian_over_kuiper/` 与
`paper/ns3_experiments/a_b/templates/`，Hypatia 跑一个 ns-3 实验需要：

1. 一个 state 目录（`gen_data/<network_name>/`），由 satgenpy 生成，含：
   - `tles.txt`（首行 `<num_planes> <num_sats_per_plane>`，余行 sat name + 2 行 TLE）
   - `isls.txt`、`ground_stations.txt`、`gsl_interfaces_info.txt`、`description.txt`
   - `dynamic_state_<int_ms>ms_for_<dur_s>s/fstate_<t_ns>.txt`、
     `gsl_if_bandwidth_<t_ns>.txt`
2. 一个 run 目录，含：
   - `config_ns3.properties`：键值对，至少包括
     `satellite_network_dir`、`satellite_network_routes_dir`、
     `dynamic_state_update_interval_ns`、`simulation_end_time_ns`、
     `enable_<x>_logging`、`tcp_socket_type` 等；
   - `schedule.csv`：TCP flow 列表
3. 启动命令：
   `./waf --run="main_satnet --run_dir='<run dir 绝对路径>'"`

集成测试的具体配置已经看过：`simulation_end_time_ns=200e9`，
`tcp_socket_type=TcpNewReno`，data rate 10 Mbps，queue 100 pkts。

## Step 3（提前做）— fstate 是否包含 SAT 作为 dst？

> 这一步原本是任务文档的 Step 3，但它是后续所有步骤能否进行的分水岭，
> 必须最先回答。

### 数据层观察（reduced Kuiper-630，17 sat + 2 GS）

取 `temp/gen_data/.../dynamic_state_100ms_for_200s/fstate_0.txt`：

```
0,17,1,0,0
0,18,4,1,0
1,17,17,2,0
...
```

- 总行数：36 = 18 (src=sat 0..16 + src=gs 17) × 2 (dst=17 或 18) — sat 17 是
  Manila，sat 18 是 Dalian。
- **dst 字段 unique = {17, 18}，即两个 GS 节点。零条以 SAT 为 dst 的路由。**

### satgenpy 源码确认（不是巧合，是设计）

`satgenpy/satgen/dynamic_state/fstate_calculation.py`：

- 函数 `calculate_fstate_shortest_path_without_gs_relaying` 第 38–40 行：
  ```python
  for curr in range(num_satellites):
      for dst_gid in range(num_ground_stations):
          dst_gs_node_id = num_satellites + dst_gid
  ```
- 同文件 `calculate_fstate_shortest_path_with_gs_relaying` 第 185–187 行：
  ```python
  for current_node_id in range(num_satellites + num_ground_stations):
      for dst_gid in range(num_ground_stations):
          dst_gs_node_id = num_satellites + dst_gid
  ```

无论是否走 GS relay，dst 永远只遍历 ground_stations。这是 4 个 algorithm 文件
共享的底层调用——`algorithm_free_one_only_over_isls.py`、
`algorithm_free_gs_one_sat_many_only_over_isls.py`、
`algorithm_free_one_only_gs_relays.py`、`algorithm_paired_many_only_over_isls.py`
都从这两个函数其中之一出 fstate。算法文档注释也明示：
`(src gs) - (sat) - (sat) - ... - (sat) - (dst gs)`。

### ns-3 端是否能接受 SAT-as-dst？— 能

`ns3-sat-sim/simulator/contrib/satellite-network/helper/
arbiter-single-forward-helper.cc`：

- `InitialEmptyForwardingState`（line 55–65）：为每个 src 节点开
  `m_nodes.GetN()` 长度的 next-hop list，覆盖**所有节点**作为潜在 dst，
  默认 `(-2, -2, -2)`（无效）。
- `UpdateForwardingState`（line 92, line 100–101）：
  ```cpp
  int64_t target_node_id = parse_positive_int64(comma_split[1]);
  NS_ABORT_MSG_IF(target_node_id < 0 || target_node_id >= m_nodes.GetN(),
                  "Invalid target node id.");
  ```
  对 target 只校验范围。SAT id ∈ [0, N_sat) 完全是合法 target。

### 结论

- 数据层（satgenpy 写出的 fstate）：**只有 GS-as-dst 条目**。
- 运行时（ns-3 转发表）：**预留了 SAT-as-dst 槽位但默认全是无效**。
- 直接用 schedule 写 `to_node_id = <某 SAT>`，ns-3 起来后 IP 包到第一个
  GSL 节点时该节点查 `fstate[(curr, SAT-Y)]` 得到 `(-2,-2,-2)`，包会被
  `arbiter-single-forward.cc` 当作"无路由"丢掉（或 abort，取决于具体路径）。

## STOP — 这正是任务文档的停止条件

任务文档原文：

> 如果只有 GS-to-GS 路由，需要修改 satgenpy 重新生成包含 GS-to-SAT 路由的
> fstate（这是唯一允许在 extensions/ 外做修改的地方，但仍要先停下来报告，
> 等待确认）。

我没有继续往下做。下一步是哪条路，等用户拍板。

### 三个可行方案（按改动范围从小到大）

#### 方案 A：fstate 增量写出，零修改 satgenpy

在 `extensions/phase_a/` 下新写一个 `augment_fstate.py`：

1. 读取已有的 satgenpy state（tles / isls / ground_stations / gsl_interfaces_info）；
2. 对**每个 compute SAT** C_y、**每个其它节点** n，用同样的 ISL+GSL 图跑
   shortest path，得到 (next_hop, my_if, next_if) 三元组；
3. **追加**到现成的 `fstate_<t>.txt` 文件后面（或写到一个 sibling state dir）。
4. ns-3 端无需任何改动，它读 fstate 时会把这些 SAT-dst 条目填进
   `m_arbiters.at(...)->SetSingleForwardState(...)` 的预留槽位。

优点：满足"不改 Hypatia 核心代码"硬约束；改动局部，可回滚。
缺点：拓扑/路由算法重新实现一遍，要小心和 satgenpy 保持口径一致（GSL
判断、ISL 最短路、断链时的丢弃语义）。

#### 方案 B：扩 satgenpy（任务文档说"允许，但要先报告"）

在 `satgenpy/satgen/dynamic_state/fstate_calculation.py` 加一个旁路函数：

```python
def calculate_fstate_shortest_path_for_compute_sat_dsts(...):
    # 在已有 GS-dst 输出之后，再迭代一遍 compute sat 作为 dst，
    # 追加写入同一个 fstate_<t>.txt
```

并在 `algorithm_free_one_only_over_isls.py` 末尾接一次调用（条件：state
目录里存在 `satellite_roles.txt` 文件就启用）。

优点：算法层一处改动，所有 algorithm 自动受益；与 satgenpy 内部口径
天然一致。
缺点：动了核心代码，未来 Hypatia 升级要小心 cherry-pick。

#### 方案 C：把 compute SAT 包装成 ground station

每个 compute SAT 处虚拟一个 "GS" 节点附在它上面，所以路由仍走 GS-to-GS。

优点：完全不动 fstate 算法。
缺点：要在 ground_stations.txt 里造假地面站、要保证它的 GSL 只连到对应
SAT、要保证仿真时该假 GS 不在地面而是跟着 SAT 跑（位置随 SGP-4 变化）。
**这远比方案 A/B 复杂、且语义不对**——后续 Phase B/C 要在 compute SAT
上做计算时，把它当 GS 会引入更多 hack。不推荐。

### 我的倾向

**推荐方案 A**。理由：

- 严格遵守"不改核心代码"约束（任务文档第二段是硬约束）；
- 改动可单独审查、可单独 rollback；
- 后续 Phase B–F 要扩 LLM workload / 策略层时，所有改动都集中在
  `extensions/phase_*/`，整个项目结构干净；
- 实现成本不高：拓扑信息全在 tles+isls 里，shortest-path 在 networkx 一行；
  ns-3 端连一个判断都不用加。

但如果你认为 satgenpy 那点小改是可接受的（A 方案要写 ~150 行 Python 来
基本复制 satgenpy 的图构建逻辑；B 方案是 ~30 行修改 + 一次性），那 B 也合理。

**请拍板。** 我会按你选择的方案继续。

### 用户答复

用户选 **方案 A**：在 `extensions/phase_a/` 内自己写 fstate 增量，不动
Hypatia 核心代码。

## Step 2 + Step 3.5 + Step 5/6/7 工具实现（DONE，代码层）

工具放在 `extensions/phase_a/` 下，跨 phase 复用 satgenpy 库（仅 import，
不修改）：

| 文件 | 用途 |
|---|---|
| `satellite_roles.py` | 读 tles.txt → 写 `<sat_id>,<C|T>`。支持 `by_plane`（默认：planes 0,8,...,56）与 `random` 两种策略 |
| `augment_fstate.py` | 读 satgenpy state + 指定 dst sat 列表，对每个 fstate_<t>.txt **追加** SAT-dst 路由行（同 5-元组 CSV 格式，前置 `# PHASE_A_AUGMENT` 注释行作标记），逻辑镜像 `fstate_calculation.py` 的 `calculate_fstate_shortest_path_without_gs_relaying`，只是 dst 循环从 `range(num_ground_stations)` 改成用户给的 SAT id 列表 |
| `config_ns3_phase_a.properties` | 5 秒仿真、Starlink-550 state、10 Mbps 链路、100 包队列、TcpNewReno —— 与集成测试同口径方便对照 |
| `run_phase_a_experiment.sh` | 编排：prereq 检查 → 在 `runs/<name>/` 软链 config + schedule → `./waf --run main_satnet --run_dir=...` |
| `analyze_phase_a.py` | 离线遍历 fstate 还原路径、读 `tcp_flow_0_rtt.csv`、计算几何 RTT 下界、写 `phase_a_result.md` |

为何 augment 不做 delta 编码：Phase A 一次仿真只 5 s × 100 ms = 50 个
timestep × 单一 dst SAT × ~1684 src 节点 ≈ 84 k 行，几 MB，懒得复杂化。
后续 phase 若要把整组 type=C 都加进来再优化。

接口编号约定（写 augment 时验证过，否则 ns-3 端会 ABORT）：

- 卫星 ISL 接口下标 0..num_isls_per_sat - 1，按 `isls.txt` 中出现顺序分配
- 卫星 GSL 接口下标 = `num_isls_per_sat[sat]`（在所有 ISL 之后）
- 地面站只有 1 个 GSL 接口，下标 0
- fstate 文件里写 0-indexed，ns-3 内部读完后 `+1` 跳 lo 接口

## Step 1 → 已有 baseline 调研（DONE）

我直接参考已跑通的 `integration_tests/test_manila_dalian_over_kuiper/`：
- `temp/runs/.../config_ns3.properties` 提供完整字段清单
- `temp/runs/.../schedule.csv` 是 `0,17,18,100000000000,0,,`
- `temp/data/.../tcp_flow_0_rtt.csv` 表明 RTT 日志 schema 是 `flow_id, time_ns, rtt_ns`
- ns-3 用 `./waf --run "main_satnet --run_dir='...'"` 启动

没有额外重跑——这步是模板调研，不是新跑一次仿真。集成测试已经在前期
全 Hypatia 测试里通过过（"Hypatia tests were run and passed"）。

## Step 4 + 数据态：等 state 生成

- 后台命令：`python main_starlink_550.py 10 100 isls_plus_grid
  ground_stations_top_100 algorithm_free_one_only_over_isls 2`
- 工作日志：`extensions/phase_a/state_gen.log`
- 静态文件已就绪：
  - `gen_data/starlink_550_.../tles.txt`：72 planes × 22 sats（header `72 22`）
  - `ground_stations.txt`：100 行，**GS-0 = Tokyo (35.6895, 139.69171)**
  - `description.txt`：`max_gsl_length_m=1089686.4`、`max_isl_length_m=5016591.2`
  - `isls.txt` / `gsl_interfaces_info.txt`
- 动态状态进度：截至最后一次抽样 31/100 个 fstate 文件（每个 100 ms 一份），
  生成完毕后才能跑 augment / pick-far-sat / 仿真。

## 待跑的步骤（state ready 后）

1. `python satellite_roles.py --tles <state>/tles.txt --output satellite_roles.txt`
2. 在 satellite_roles.txt 的 type=C 集合里挑离 Tokyo 大圆距离最远的卫星
   作为 SAT-Y，inline Python 计算（不单独文件，避免凑数）
3. 渲染 `schedule_gs_to_compute.csv`：`0,1584,<SAT-Y>,1000000,1000000000,,phase_a_gs0_to_compute`
4. `python augment_fstate.py --state-dir <state> --dynamic-state-dir <state>/dynamic_state_100ms_for_10s --dst-sats <SAT-Y>`
5. `bash run_phase_a_experiment.sh`
6. `python analyze_phase_a.py --run-dir runs/gs0_to_compute_sat --state-dir <state> --dynamic-state-dir <state>/dynamic_state_100ms_for_10s`
