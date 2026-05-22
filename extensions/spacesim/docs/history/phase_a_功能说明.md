# Phase A 功能说明

本文是 Phase A 的中文功能文档，对应英文版 [`README.md`](README.md)。
覆盖：要解决什么、怎么解决的、各文件做什么、怎么跑、能调什么、
测试覆盖度、ns-3 补丁的语义、Phase B 接口。

> 项目根目录：`/home/mark/spacesim/`
> Phase A 工作目录：`/home/mark/spacesim/hypatia/extensions/phase_a/`

---

## 一、Phase A 到底解决什么

LLM-on-satellite 整体项目分 6 个 phase。**Phase A 的范围是单点的**：让
Hypatia 允许把 **compute satellite**（计算卫星）当作 TCP 流量的合法
端点，并通过一个最小实验证明 GS → SAT 的流量可以真正建立、传完一段
数据、经过 ISL 几跳。后续 phase 才做 workload generator、LLM request
application、策略层等。

### 一句话结果

| 项 | 值 |
|---|---|
| 星座 | Starlink-550 first shell（72 plane × 22 sat = **1584 颗**），100 个 GS |
| Compute 比例 | **11.1%**（176 颗，by-plane 策略选 plane 0, 8, …, 56） |
| 源点 | GS-0 = 东京（节点 ID `1584`） |
| 目的点 | **SAT-894**（plane 40，从东京几何 slant range 13,253 km） |
| 流 | 1 MB TCP New Reno，10 Mbps ISL/GSL，100-pkt 队列 |
| 路径 | 1 GSL + **11 ISL**，总长 **21,913 km** |
| 几何 RTT 下界 | **146.2 ms** |
| 测得 min RTT | **147.0 ms**（余量 +0.8 ms ✓） |
| 流完成 | **YES**，耗时 2.064 s |
| 仿真墙钟 | < 1 分钟 |

最终判定：**PASS**（见 [`phase_a_result.md`](phase_a_result.md)）。

---

## 二、为什么需要写代码——两个绕不开的限制

Hypatia 上游设计假定流量端点只能是地面站（GS）。这个假定在两个地方写死了，
Phase A 各自给出一条最小侵入的修复路径。

### 限制 1：satgenpy 写出的 fstate 只编码 GS-as-dst

`satgenpy/satgen/dynamic_state/fstate_calculation.py` 的两个函数
`calculate_fstate_shortest_path_without_gs_relaying` 和
`calculate_fstate_shortest_path_with_gs_relaying`，dst 循环都写死成
`for dst_gid in range(num_ground_stations)`，永远不会以卫星作为 dst
迭代。生成出来的 `fstate_<t>.txt` 没有任何 `dst = <某颗 SAT>` 的转发条目。

**ns-3 这一层其实支持**：`arbiter-single-forward-helper.cc` 把转发表
开成 `[N × N]` 二维数组（N 是所有节点数），SAT id 完全是合法 target，
只是没人给它喂条目，所以预留槽位永远是 `(-2, -2, -2)` 无效，包到了
SAT 这条路由查不出来直接丢。

**Phase A 解法（[`augment_fstate.py`](augment_fstate.py)）**：在 extensions
里写一个工具，读 satgenpy 已经写出来的 state，**追加** SAT-dst 路由
条目到每个 `fstate_<t>.txt`。算法逻辑严格镜像 satgenpy 的
`calculate_fstate_shortest_path_without_gs_relaying`——同一套 ISL 图、
同一套 Floyd-Warshall、同一套接口编号约定，**只是 dst 循环改为遍历
用户指定的 `--dst-sats`**。

**Hypatia 源码零修改。**

### 限制 2：ns-3 的 schedule reader 拒绝 SAT 当 endpoint

`ns3-sat-sim/.../topology-satellite-network.cc` 的构造函数里有：

```cpp
// Only ground stations are valid endpoints
for (uint32_t i = 0; i < m_groundStations.size(); i++) {
    m_endpoints.insert(m_satelliteNodes.GetN() + i);
}
```

`m_endpoints` 是个 set，作为 `IsValidEndpoint(node_id)` 的判据。
`tcp-flow-schedule-reader.cc` 和 `udp-burst-schedule-reader.cc` 都查这个
来校验 schedule 里的 `from` / `to`。schedule 写 `to = 894`（SAT）直接被拒：

```
terminate called after throwing an instance of 'std::invalid_argument'
  what():  Invalid to-endpoint for a schedule entry based on topology: 894
```

**Phase A 解法（~25 行 C++ patch）**：在
`topology-satellite-network.cc` 构造函数的 GS-endpoint 循环之后追加一段：
如果 run dir 下存在 `satellite_roles.txt`，逐行解析格式 `<sat_id>,<C|T>`，
把所有 `C` 角色的 sat id 也加进 `m_endpoints`。

补丁的关键性质：

- **完全向后兼容**：没有 `satellite_roles.txt` 时行为与上游 Hypatia 一致；
- **容错优先**：格式不对的行 silently 跳过，不 abort；
- **单点真相**：同一份 `satellite_roles.txt` 被 C++（决定哪些 SAT 是
  endpoint）、`augment_fstate.py`（决定要给哪些 SAT 加路由）、
  `pick_dst_sat.py`（在 type=C 集合里挑目标）共同读取，语义贯通。

**唯一对 ns-3 核心代码的改动**就这里。SGP-4 卫星模块（`src/satellite/`）、
arbiter、scheduler、fstate parser 全部没碰。

---

## 三、工具链与数据流

```
┌────────────────────────────────────────────────────────────────────┐
│ paper/satellite_networks_state/main_starlink_550.py (上游 satgenpy) │
│   → gen_data/starlink_550_.../tles.txt, isls.txt, ground_stations.txt│
│   → gen_data/starlink_550_.../dynamic_state_100ms_for_10s/           │
│         fstate_<t>.txt           ← 只含 GS-dst 条目 (上游限制 1)      │
│         gsl_if_bandwidth_<t>.txt                                     │
└────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌────────────────────────────────────────────────────────────────────┐
│ extensions/phase_a/                                                  │
│                                                                      │
│   satellite_roles.py ──→ satellite_roles.txt  (1584 行, 176 C/1408 T)│
│           │                       │                                  │
│           │                       │                                  │
│   pick_dst_sat.py <───────────────┘                                  │
│      (读 roles 选远端 type=C SAT 当目标)                              │
│           │                                                          │
│           │ DST_SAT = 894                                            │
│           ▼                                                          │
│   schedule_gs_to_compute.csv:  0,1584,894,1000000,200000000,,note   │
│                                                                      │
│   augment_fstate.py  ──→ 给 fstate_<t>.txt 追加 SAT-dst 行           │
│                          + .phase_a_augment.json (manifest 记录进度) │
│                                                                      │
│   run_phase_a_experiment.sh                                          │
│      ├─ prereq check (用 manifest)                                   │
│      ├─ runs/<name>/ 软链 config + schedule + satellite_roles.txt    │
│      └─ ./waf --run "main_satnet --run_dir=..."                      │
│                                                                      │
│   analyze_phase_a.py                                                 │
│      ├─ 读 tcp_flows.csv (流是否完成)                                │
│      ├─ 读 tcp_flow_0_rtt.csv (RTT 时序)                             │
│      ├─ 离线沿 fstate 还原路径                                       │
│      ├─ 算几何 RTT 下界对比                                          │
│      └─ 写 phase_a_result.md (PASS/FAIL)                             │
└────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌────────────────────────────────────────────────────────────────────┐
│ 已 patched 的 ns-3 (限制 2 已解)                                      │
│   topology-satellite-network.cc 构造函数读 run_dir/satellite_roles  │
│   把 type=C 的 sat 加入 m_endpoints,                                 │
│   schedule reader 不再拒 SAT 当 to-endpoint                          │
└────────────────────────────────────────────────────────────────────┘
```

---

## 四、文件清单

`phase_a/` 一级目录：

| 文件 | 类型 | 作用 |
|---|---|---|
| [`README.md`](README.md) | doc | 英文版完整文档 |
| [`功能说明.md`](功能说明.md) | doc | 本文件 |
| [`phase_a_log.md`](phase_a_log.md) | doc | 时间序工作日志（含两次 stop-and-ask 决策点） |
| [`phase_a_result.md`](phase_a_result.md) | doc | 实验结果摘要（PASS 判定） |
| [`satellite_roles.py`](satellite_roles.py) | 工具 | 生成角色文件，支持 `by_plane` / `random` 两策略 |
| [`satellite_roles.txt`](satellite_roles.txt) | data | 角色文件，1584 行；本仓库的**单点真相** |
| [`augment_fstate.py`](augment_fstate.py) | 工具 | 给 satgenpy fstate 追加 SAT-dst 路由 |
| [`pick_dst_sat.py`](pick_dst_sat.py) | 工具 | 选离源 GS 最远的 type=C SAT 当目标 |
| [`schedule_gs_to_compute.csv`](schedule_gs_to_compute.csv) | data | TCP flow 定义 |
| [`config_ns3_phase_a.properties`](config_ns3_phase_a.properties) | config | ns-3 仿真配置 |
| [`run_phase_a_experiment.sh`](run_phase_a_experiment.sh) | script | 一键 orchestrator |
| [`analyze_phase_a.py`](analyze_phase_a.py) | 工具 | 离线分析 + 生成 result.md |
| [`run_tests.sh`](run_tests.sh) | script | pytest 入口 |
| [`tests/`](tests/) | dir | 52 个 testcase，覆盖单元 + 集成 + 回归 |
| `runs/gs0_to_compute_sat/` | output | 上一次 ns-3 跑出来的产物（gitignore） |
| `logs/` | logs | 杂项执行日志（gitignore） |
| `.gitignore` | meta | 忽略 `runs/`、`logs/`、`__pycache__/`、`.pytest_cache/` |

ns-3 那一处补丁在 phase_a/ 之外：

```
hypatia/ns3-sat-sim/simulator/contrib/satellite-network/model/topology-satellite-network.cc
                                                            ↑ ~25 行追加
```

---

## 五、从零跑一遍的命令序列

前提：Hypatia 已构建（`bash hypatia_build.sh` 跑过、`/home/mark/spacesim/venv`
存在、`topology-satellite-network.cc` 的 patch 已经 `./waf` 增量编译进去）。

```bash
source /home/mark/spacesim/venv/bin/activate
cd /home/mark/spacesim/hypatia/extensions/phase_a

# 1. 生成 Starlink-550 星座状态（如果还没有）
cd ../../paper/satellite_networks_state
python main_starlink_550.py 10 100 isls_plus_grid ground_stations_top_100 \
    algorithm_free_one_only_over_isls 2
cd -

# 2. 生成角色文件（by_plane 默认选 plane 0,8,...,56 共 8 个）
STATE_DIR=../../paper/satellite_networks_state/gen_data/starlink_550_isls_plus_grid_ground_stations_top_100_algorithm_free_one_only_over_isls
python satellite_roles.py \
    --tles "$STATE_DIR/tles.txt" \
    --output satellite_roles.txt

# 3. 挑离 Tokyo 最远的 type=C SAT 当目标
DST_SAT=$(python pick_dst_sat.py \
    --state-dir "$STATE_DIR" \
    --roles satellite_roles.txt \
    --src-gs 0 --start-time-ns 200000000)
echo "DST_SAT=$DST_SAT"

# 4. 写 schedule
echo "0,1584,$DST_SAT,1000000,200000000,,phase_a_gs0_to_compute" \
    > schedule_gs_to_compute.csv

# 5. 给 fstate 追加 SAT-dst 路由（manifest 自动记录哪些 t 已做）
python augment_fstate.py \
    --state-dir "$STATE_DIR" \
    --dynamic-state-dir "$STATE_DIR/dynamic_state_100ms_for_10s" \
    --dst-sats "$DST_SAT"

# 6. 跑 ns-3
bash run_phase_a_experiment.sh

# 7. 分析
python analyze_phase_a.py \
    --run-dir runs/gs0_to_compute_sat \
    --state-dir "$STATE_DIR" \
    --dynamic-state-dir "$STATE_DIR/dynamic_state_100ms_for_10s" \
    --out phase_a_result.md
```

orchestrator `run_phase_a_experiment.sh` 在跑 ns-3 之前会做 prereq check：

- 从 config 读 `dynamic_state_update_interval_ns` 和 `simulation_end_time_ns`，
  推算 ns-3 会**实际去读**的 timestep 集合；
- 对每个需要的 timestep，验证 `fstate_<t>.txt` 和 `gsl_if_bandwidth_<t>.txt`
  都存在；
- 优先查 manifest `<dyn_dir>/.phase_a_augment.json` 看是否已 augment；
  manifest 没有时退回 CSV 探针（`awk '$2 == DST_NODE'`）兜底（兼容老 run）；
- **强制**拒绝任何 `^#` 注释行（ns-3 的 fstate parser 在注释行上 SIGIOT）。

---

## 六、可调参数

| 在哪 | 参数 | 说明 |
|---|---|---|
| `satellite_roles.py --strategy` | `by_plane` / `random` | 选 compute 的策略 |
| `satellite_roles.py --planes` | 逗号分隔的 plane 索引 | by_plane 选哪几个平面 |
| `satellite_roles.py --ratio` | (0, 1) 之间浮点 | random 比例 |
| `satellite_roles.py --seed` | 整数 | random 种子（复现性） |
| `schedule_gs_to_compute.csv` | 一行 7 字段 | flow_id, from, to, size, start_ns, params, metadata |
| `config_ns3_phase_a.properties` | `simulation_end_time_ns` | 仿真总时长（ns） |
| `config_ns3_phase_a.properties` | `dynamic_state_update_interval_ns` | ns-3 重读 fstate 的间隔 |
| `config_ns3_phase_a.properties` | `*_data_rate_megabit_per_s` | 链路带宽 |
| `config_ns3_phase_a.properties` | `*_max_queue_size_pkts` | 链路缓冲深度 |
| `config_ns3_phase_a.properties` | `tcp_socket_type` | `TcpNewReno` / `TcpVegas` / `TcpCubic` / `TcpBbr` |
| `pick_dst_sat.py --start-time-ns` | int | 用哪个 SGP-4 时刻测"最远" |
| `pick_dst_sat.py --src-gs` | int | 源地面站的 GID（0 = top-100 列表第一个） |
| `augment_fstate.py --dst-sats` | `<id>` / `<id1,id2,...>` / `all-compute` | 要给哪些 SAT 加路由 |
| `augment_fstate.py --rewrite` | flag | 强制按 dst-sat 剥掉已存在的行后再追加（也能清残留的 `^#` 行） |
| `augment_fstate.py --max-timesteps` | int | 调试用，只处理前 N 个 timestep |

---

## 七、测试套件

```bash
cd /home/mark/spacesim/hypatia/extensions/phase_a
./run_tests.sh            # 跑全部 (52 case，约 4 秒)
./run_tests.sh -k augment # 只跑名字含 augment 的
./run_tests.sh -v         # verbose
```

测试分三层：

| 层 | 文件 | case 数 | 跑得有多快 |
|---|---|---:|---|
| 单元 - 角色文件 | `tests/test_satellite_roles.py` | 11 | <0.1s |
| 单元 - 路径追踪 / fstate / schedule 解析 | `tests/test_analyze_phase_a.py` | 13 | <0.1s |
| 单元 + 集成 - augment 工具 | `tests/test_augment_fstate.py` | 18 | ~3s |
| 单元 + CLI - dst 选择器 | `tests/test_pick_dst_sat.py` | 4 | ~0.5s |
| 回归 - cached 跑出来的产物 | `tests/test_phase_a_regression.py` | 5 | <0.1s |
| **总计** | — | **52** | **~4s** |

集成测试用 `integration_tests/test_manila_dalian_over_kuiper/` 里 reduced
Kuiper-630 的 17-sat 状态做底，跑真 SGP-4 验证 `compute_augment_rows`
的输出行数、`dst` 字段、接口编号约定、与 `nbr_to_if` 的反查一致性。

回归测试断言 `runs/gs0_to_compute_sat/logs_ns3/tcp_flows.csv` 仍然
`completed = YES`、source 是 GS-0、size 是 1 MB、RTT 样本足够多。
如果未来某天 augment 或 C++ patch 出 bug、流完不成，这一组测试会先尖叫。

测试设计原则：

- **缺失资源不算失败，只是 skip**——清新 clone 没有 reduced Kuiper state
  或 cached run 时，集成/回归测试自动跳过，单元测试照样跑；
- **不调用 ns-3**——重新跑 ns-3 太慢，回归层就用 cached 产物；
- **集成测试只跑 1 个 timestep**——SGP-4 计算贵，1 个 timestep 已经足够
  覆盖路径选择 / 接口编号 / 反查一致这三件大事。

---

## 八、ns-3 patch 详细说明

文件：`hypatia/ns3-sat-sim/simulator/contrib/satellite-network/model/topology-satellite-network.cc`

补丁在原 GS-endpoints 那段循环之后：

```cpp
// Ground stations are always valid endpoints.
for (uint32_t i = 0; i < m_groundStations.size(); i++) {
    m_endpoints.insert(m_satelliteNodes.GetN() + i);
}

// Phase A extension (LLM-on-satellite):
// If the run dir ships a `satellite_roles.txt` file, also accept any
// satellite marked as type=C (compute) as a flow endpoint.
std::string roles_path = m_basicSimulation->GetRunDir() + "/satellite_roles.txt";
if (file_exists(roles_path)) {
    std::ifstream rf(roles_path);
    std::string line;
    size_t added = 0;
    while (std::getline(rf, line)) {
        if (line.empty() || line[0] == '#') continue;
        auto comma = line.find(',');
        if (comma == std::string::npos) continue;
        int sat_id;
        try {
            sat_id = std::stoi(line.substr(0, comma));
        } catch (const std::exception&) {
            continue;
        }
        if (sat_id < 0 || static_cast<uint32_t>(sat_id) >= m_satelliteNodes.GetN()) {
            continue;
        }
        size_t k = comma + 1;
        while (k < line.size() && std::isspace(static_cast<unsigned char>(line[k]))) ++k;
        if (k < line.size() && line[k] == 'C') {
            m_endpoints.insert(sat_id);
            ++added;
        }
    }
    std::cout << "  > Compute SATs from satellite_roles.txt added as endpoints: "
              << added << std::endl;
}
```

补丁特性：

| 性质 | 体现 |
|---|---|
| 向后兼容 | 没有 `satellite_roles.txt` 时和上游行为完全一致 |
| 容错 | 解析失败的行 silently 跳过 |
| 越界保护 | sat_id 越过 `m_satelliteNodes.GetN()` 不会污染 m_endpoints |
| 注释友好 | `#` 开头的行跳过（与 Python 工具的约定一致） |
| 无新增公开 API | `m_endpoints` 本来就是 `std::set<int64_t>`，没改头文件 |
| 改完编译 | `./waf` 增量重编 ≈ 13 秒 |

编译命令：

```bash
cd /home/mark/spacesim/hypatia/ns3-sat-sim/simulator
PATH=/home/mark/spacesim/venv/bin:$PATH ./waf
```

---

## 九、已知坑（务必知道，写给后续 phase）

下面这几条是 Phase A 实际踩到、并已写进 [`phase_a_log.md`](phase_a_log.md) 的。

### 坑 1：fstate 不允许任何注释行

`arbiter-single-forward-helper.cc` 对每行 fstate 做 `split_string(",", 5)`，
要求**精确 5 个字段**，遇到任何不满足的行（包括 `#` 开头的注释行）
立刻抛 `std::invalid_argument`，进程 SIGIOT。

→ Phase A 早期版本的 `augment_fstate.py` 在 SAT-dst 段前写过一行
`# PHASE_A_AUGMENT begin: ...` 作为"已处理"标记，**已经废弃**。现在改用
sidecar `.phase_a_augment.json` 记录 (dst_sat, t) 处理状态。

→ Phase B+ 任何写 fstate 的工具，**不要写注释行**。需要标记请用 sidecar
文件、文件名前缀、或单独的元数据文件。

### 坑 2：truncated fstate 是隐形雷

我们这次跑 satgenpy 生成 Starlink-550 state 时，60 个 fstate 文件里只有
**fstate_0** 和 **fstate_5000000000** 是完整的（169983 行 = 168300 GS-dst +
1683 SAT-dst），其余 58 个都被截断成 1–1800 行，原因不明（不是 OOM 不是
磁盘满）。

→ Phase A 临时绕开的办法：把 `dynamic_state_update_interval_ns` 改成 5e9，
`simulation_end_time_ns` 改成 2.5e9，ns-3 在 t=0 读一次完整的 fstate_0，
计划在 t=5e9 再读但仿真已结束。这样不碰任何坏文件。

→ 检查完整性的 sanity：行数应该等于
`(num_satellites + num_ground_stations - 1) × num_ground_stations`，
Starlink-550 + top-100 GS 的话是 `1683 × 100 = 168300`。

→ Phase B 大规模 sweep 之前**必须**先把 state-gen 稳定下来。建议先在
reduced constellation（17 sat）跑通验证，再上 1584 sat。

### 坑 3：dynamic_state_update_interval_ns 影响 ns-3 真去读哪些文件

ns-3 不是只在 t=0 读 fstate——它会在 `0, interval, 2×interval, …` 每个
时刻都重新打开 `fstate_<t>.txt`。所以缺 fstate 文件 = 跑到那一刻 ns-3
abort。Phase A 通过把 interval 调到大于 simulation_end_time_ns 绕过了
这个问题，代价是仿真期间 forwarding state **冻结**。

→ Phase B 想做长仿真（≥10s）或者要观察路由切换，必须有连续完整的
fstate 序列。

### 坑 4：augment 慢的瓶颈在 SGP-4 propagation

`compute_augment_rows` 单 timestep 在 Starlink-550 上约 23 秒，几乎全
花在 100 GS × 1584 sat 的 SGP-4 距离计算上。增加 dst-sats 个数对
单 timestep 时间影响不大（GS-in-range 是 dst-sats 之外的开销）。

→ Phase B 用 `--dst-sats=all-compute` 给 176 颗 type=C 都加路由，单
timestep 时间不会乘 176（GS-in-range 缓存一次），但**会乘的是 timestep
数**——10s 仿真 100 个 timestep 就是 ~40 分钟。建议：先 reduce 仿真时长
做 smoke test，再上长时段。

### 坑 5：每 phase 加的端点都要走同一个 satellite_roles.txt

C++ 端启动时读一次 `<run_dir>/satellite_roles.txt`，加入 m_endpoints
之后**不重读**。所以 schedule 里所有 `from` / `to` 引用的卫星必须
在角色文件里是 type=C。Phase B 写 workload generator 时，先决定要用
哪些 SAT，更新角色文件，再生成 schedule。

---

## 十、Phase B 接续点

Phase A 把数据契约和路由打通了，Phase B 可以**纯 Python 接续**：

1. **一次性 augment 所有 type=C**（约 40 分钟，manifest 让中途可断续）：
   ```bash
   python augment_fstate.py \
       --state-dir <state> \
       --dynamic-state-dir <dyn> \
       --dst-sats all-compute \
       --roles satellite_roles.txt
   ```

2. **写 LLM workload generator**——输出多行 `schedule.csv`，basic-sim
   的 TCP scheduler 天然支持并发流。每个"LLM 请求"用两行表达：
   - 一条 GS → SAT prompt flow（small bytes，例如 4 KB）
   - 一条 SAT → GS response flow，start_time =
     `t_prompt_arrive + think_time`

3. **保持 C++ 不动**——把"SAT 上真的跑一段 compute 时间"留给 Phase C
   的 `LlmRequestApp`。Phase B 用 schedule 的 start_time 偏移 fake 出
   think_time 的语义，validation 通过 RTT 时序和 throughput 是否符合
   预期就足够了。

这样 Phase B 的范围 = **workload generator → schedule.csv → 跑 → 验证**，
不碰 ns-3 源码、不改 fstate 算法、不动 endpoint 判定。最干净的迭代节奏。

---

## 参考

- 英文版完整文档：[`README.md`](README.md)
- 时间序工作日志（含两次停下来请示的决策点）：[`phase_a_log.md`](phase_a_log.md)
- PASS 判定的实验结果：[`phase_a_result.md`](phase_a_result.md)
- 整个 Hypatia 仓库的中文使用手册：[`../../../使用手册.md`](../../../使用手册.md)