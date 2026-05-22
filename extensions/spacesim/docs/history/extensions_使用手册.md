# Hypatia LLM-on-satellite 扩展使用手册（Phase A + Phase B）

把 Hypatia LEO 卫星网络仿真器扩展成"地面站向计算卫星打 LLM 推理流量"
的端到端实验平台。本手册讲清楚两个 phase 各自做了什么、怎么跑、怎么按
你的需求调配置。

> 工作根目录：`/home/mark/spacesim/hypatia/`
> 扩展目录：`extensions/`
> 仅依赖 `/home/mark/spacesim/venv/`（Python 3.8.10 + cartopy 0.18 + pytest 等）

---

## 目录

1. [总览](#一总览)
2. [前置环境](#二前置环境)
3. [Phase A：让 SAT 成为合法流量端点](#三phase-a让-sat-成为合法流量端点)
   - 3.1 概念与组件
   - 3.2 文件清单
   - 3.3 如何跑（默认 + 混合拓扑场景）
   - 3.4 怎么改：12 种典型修改
4. [Phase B：LLM 推理流量](#四phase-bllm-推理流量)
   - 4.1 概念与三层时延
   - 4.2 文件清单（含 ns-3 module）
   - 4.3 如何跑（单流 + 多流场景）
   - 4.4 怎么改：12 种典型修改
5. [改 ns-3 C++ 代码的标准流程](#五改-ns-3-c-代码的标准流程)
6. [常见错误与排查](#六常见错误与排查)
7. [Phase C 接续点](#七phase-c-接续点)

---

## 一、总览

```
┌──────────────────────────────────────────────────────────────────────┐
│ Phase A  ── 让 compute satellite 成为合法的网络流量端点                │
│   修了两处:                                                            │
│     1. 数据层 (satgenpy 写出的 fstate 只编码 GS-dst)                  │
│        → Python 工具 augment_fstate.py 在外部追加 SAT-dst 路由        │
│     2. ns-3 层 (TopologySatelliteNetwork::m_endpoints 写死成 GS-only) │
│        → ~25 行 C++ patch, 读 satellite_roles.txt 把 type=C 加进去   │
│                                                                        │
│   端到端验证: 一条 1 MB TCP 流 GS-Tokyo → SAT-894 跑通                │
│   多流验证 (scenarios/mixed_topology): GS→SAT / SAT→GS / GS→GS       │
└──────────────────────────────────────────────────────────────────────┘
                                  ↓
┌──────────────────────────────────────────────────────────────────────┐
│ Phase B  ── 把流量本身换成 LLM 推理语义                                │
│   ns-3 source tree 里加了一个新 module: src/llm-workload/             │
│     - LLMPacketTag (32 B, 每包带 req_id / packet_id / total_pkts /    │
│                     t_emit_ns / L_in / L_out_expected ...)             │
│     - LLMRequestApplication (Poisson 到达, 截断 Normal L_in,         │
│                              切成 N_pkt UDP 包, 每包带 Tag)            │
│     - LLMSinkApplication (UDP 9999 监听, CSV 整数流式写盘)             │
│     - LlmWorkloadScheduler (main_satnet 集成入口)                     │
│                                                                        │
│   main_satnet.cc 加了 3 行集成代码                                    │
│                                                                        │
│   单流验证: 1 GS @ λ=10 req/s 跑 5 s, 97.78% 送达                     │
│   多流验证 (scenarios/llm_workload): 5 GS → 5 compute SAT,            │
│                                       99.63% 送达, 三层时延报告       │
└──────────────────────────────────────────────────────────────────────┘
                                  ↓
                  Phase C (尚未实现): gather barrier + 计算
```

---

## 二、前置环境

### 2.1 一句话核对

```bash
ls /home/mark/spacesim/venv/bin/python                       # venv 在
ls /home/mark/spacesim/hypatia/ns3-sat-sim/simulator/build/ # ns-3 已构建
ls /home/mark/spacesim/hypatia/extensions/phase_a/satellite_roles.txt  # Phase A 角色文件存在
```

三条都 OK 才继续。

### 2.2 如果是新机器从零开始

参考根目录的 `/home/mark/spacesim/使用手册.md` —— 那份手册讲整个 Hypatia
（不只我们的扩展）怎么从零搭起：装系统包、建 venv、装 Python 依赖（含
cartopy 0.18 的关键 PROJ 6.3 pin）、`./waf` 构建 ns-3。

### 2.3 我们的扩展额外需要的步骤

| 项 | 命令 |
|---|---|
| C++ 改动已编进 ns-3 | `./waf` 在 `ns3-sat-sim/simulator/` 应输出 modules 列表含 `llm-workload` |
| Python 依赖 | venv 已装 `pytest matplotlib cartopy networkx astropy ephem sgp4`（与 Phase A 同样的 lock） |
| 状态生成（Starlink-550 或 tiny_walker_1500） | Phase A 已经跑过；如果状态目录在但 fstate 截断，重新跑见 §3.4-(7) |

---

## 三、Phase A：让 SAT 成为合法流量端点

### 3.1 概念

Hypatia 上游把"流量端点"硬编码成 ground station only：

```
数据层:  fstate_<t>.txt 文件中 dst 列只迭代 GS 节点 ID
         → 任何 dst = sat_id 的包查不到路由, 走到第一个 GSL 节点就被丢

C++ 层:  TopologySatelliteNetwork 构造函数把所有 GS 加进 m_endpoints
         → schedule_reader 校验 from/to 时, SAT id 被拒
```

Phase A 同时解决两层：

- **数据层（不动 Hypatia 代码）**：写 `augment_fstate.py`，读 satgenpy
  生成的 state、用 networkx Floyd-Warshall 算最短路、把 SAT-dst 路由
  **追加**到每个 `fstate_<t>.txt`。Hypatia 自己的算法 / 代码零修改，
  只是它写出来的文件多了几行。
- **C++ 层（修一处核心代码）**：在 `topology-satellite-network.cc`
  构造函数末尾加 ~25 行，读 `<run_dir>/satellite_roles.txt`（格式
  `<sat_id>,<C|T>`），把所有 `C` 角色的 sat 也插进 `m_endpoints`。
  缺这个文件时行为退化为上游 Hypatia，向后完全兼容。

数据契约：`satellite_roles.txt` 是**单点真相**，所有工具（Python /
C++ patch / 实验 / 分析）都读同一份。

### 3.2 文件清单

```
extensions/phase_a/
├── README.md                           ← 英文完整文档
├── 功能说明.md                          ← 中文功能文档
├── phase_a_log.md                      ← 时间序工作日志
├── phase_a_result.md                   ← 实验 PASS 摘要
│
├── satellite_roles.py                  ← 角色生成器 (by_plane | random)
├── satellite_roles.txt                 ← 1584 行 (Starlink-550, 176 C / 1408 T)
│
├── augment_fstate.py                   ← 追加 SAT-dst 路由
├── pick_dst_sat.py                     ← 选远端 type=C sat
│
├── schedule_gs_to_compute.csv          ← 默认 TCP flow: GS-Tokyo → SAT-894
├── config_ns3_phase_a.properties       ← ns-3 配置
├── run_phase_a_experiment.sh           ← 一键 orchestrator
├── analyze_phase_a.py                  ← 离线分析: 路径还原 + RTT 分解
│
├── tests/                              ← pytest 52 + 6 case
├── pytest.ini, run_tests.sh
│
├── runs/gs0_to_compute_sat/            ← 缓存的实验产物
└── scenarios/mixed_topology/           ← 多 GS / 多 compute SAT E2E 场景
    ├── README.md, 功能说明 见内部
    ├── build_state.py                  ← 6 plane × 10 sat / 1500 km 拓扑生成器
    ├── input_data/ground_stations.basic.txt  ← 5 城市 GS
    ├── satellite_roles.txt             ← 60 行 (6 C / 54 T)
    ├── schedule.csv                    ← 5 行多模式流
    ├── config_ns3.properties, run.sh, verify.py
    ├── plot_topology_paths.py          ← 单帧地理地图
    ├── plot_topology_grid.py           ← 6 面板时序快照
    ├── plot_topology_anim.py           ← 50 帧动画
    ├── plot_flow_dynamics.py           ← RTT/cwnd/progress 4 面板
    ├── make_plots.sh
    └── plots/, run/, gen_data/
```

C++ patch 位置（**不在 extensions 下**）：

```
ns3-sat-sim/simulator/contrib/satellite-network/model/topology-satellite-network.cc
                ↑ 构造函数末尾追加 ~25 行
```

### 3.3 如何跑

#### 3.3.1 跑默认的 Phase A 主实验（GS-Tokyo → SAT-894）

```bash
source /home/mark/spacesim/venv/bin/activate
cd /home/mark/spacesim/hypatia/extensions/phase_a
bash run_phase_a_experiment.sh
# 产出: runs/gs0_to_compute_sat/logs_ns3/tcp_flows.csv (completed=YES)
python analyze_phase_a.py \
    --run-dir runs/gs0_to_compute_sat \
    --state-dir ../../paper/satellite_networks_state/gen_data/starlink_550_isls_plus_grid_ground_stations_top_100_algorithm_free_one_only_over_isls \
    --dynamic-state-dir ../../paper/satellite_networks_state/gen_data/starlink_550_isls_plus_grid_ground_stations_top_100_algorithm_free_one_only_over_isls/dynamic_state_100ms_for_10s \
    --out phase_a_result.md
```

#### 3.3.2 跑混合拓扑 E2E 场景（5 GS / 5 流 / 三种模式）

```bash
cd /home/mark/spacesim/hypatia/extensions/phase_a/scenarios/mixed_topology

# 首次 / 拓扑变了时重新生成 state (~1 秒)
python build_state.py -d 5 -i 100 -j 2

# 写 roles 文件 (每平面 in-plane idx 2 = compute)
python -c "
compute = {2, 12, 22, 32, 42, 52}
with open('satellite_roles.txt', 'w') as f:
    for sid in range(60):
        f.write(f'{sid},{\"C\" if sid in compute else \"T\"}\n')
"

# augment fstate 给 6 个 compute SAT 都加路由 (~1 秒, manifest 自动跳过已做的)
python ../../augment_fstate.py \
    --state-dir gen_data/tiny_walker_1500_isls_plus_grid_5cities_algorithm_free_one_only_over_isls \
    --dynamic-state-dir gen_data/tiny_walker_1500_isls_plus_grid_5cities_algorithm_free_one_only_over_isls/dynamic_state_100ms_for_5s \
    --dst-sats all-compute --roles satellite_roles.txt

# 跑 ns-3 (~30 秒)
bash run.sh

# 验证 + 4 张图
python verify.py
bash make_plots.sh
```

#### 3.3.3 跑测试

```bash
cd /home/mark/spacesim/hypatia/extensions/phase_a
./run_tests.sh              # 全部 58 case (含 6 个 mixed_topology E2E 回归)
./run_tests.sh -m slow      # 只跑 E2E
./run_tests.sh -m "not slow" # 只跑单元
```

### 3.4 怎么改：12 种典型修改

| # | 想做什么 | 改哪 | 改完是否要重编 C++ |
|---|---|---|---|
| 1 | 换星座（Kuiper / Telesat） | 重跑 `paper/satellite_networks_state/main_<X>.py` 生成 state；改 config_ns3 里 `satellite_network_dir/...routes_dir` 路径；重跑 augment | 否 |
| 2 | 换 compute SAT 比例 / 位置 | 改 `satellite_roles.py` 的 `--planes` 参数或 `--strategy random --ratio` 重生成 `satellite_roles.txt` | **是** —— C++ patch 在启动时读 satellite_roles，**但**它只读不编，run.sh 重启就生效 |
| 3 | 换 dst compute SAT | 改 `pick_dst_sat.py` 调用、改 `schedule_gs_to_compute.csv` 第 3 列 | 否 |
| 4 | 增加 GS→compute 流（多流） | 在 `schedule_gs_to_compute.csv` 加行；如新 dst sat 没 augment 过，跑 `augment_fstate.py --dst-sats <id>` | 否 |
| 5 | 换 src GS（不再是 Tokyo） | 改 schedule 里 `from_node_id` 字段（GS-i 的 node id = `num_sats + i`） | 否 |
| 6 | 改 TCP 协议（NewReno → Cubic/BBR） | 改 config 里 `tcp_socket_type=TcpCubic` | 否 |
| 7 | 状态生成失败 / fstate 截断 | 重跑生成：`python main_<X>.py <dur_s> <interval_ms> ... <num_threads>`。Phase A 早期 Starlink-550 半失败用了 `interval=5e9` 绕过；mixed_topology 60-sat 是干净的 | 否 |
| 8 | 改仿真时长 | config 里 `simulation_end_time_ns=<新值>`；如果 > state-gen 的 duration，要重跑 state-gen 加长 | 否 |
| 9 | 改链路带宽 / 队列 | config 里 `isl_data_rate_megabit_per_s` / `gsl_max_queue_size_pkts` | 否 |
| 10 | 改 GSL 仰角阈值（影响 max_gsl_length） | 改 `build_state.py` 里 `SATELLITE_CONE_RADIUS_M = ALTITUDE / tan(elevation)`；重跑 state-gen + augment | 否 |
| 11 | 增加测试用例 | 在 `tests/` 加 `test_*.py`；E2E 测试用 `@pytest.mark.slow` 标记 | 否 |
| 12 | 改可视化（颜色 / 时间点 / 标记） | 改对应 `plot_*.py` 里参数；`make_plots.sh` 重跑 | 否 |

---

## 四、Phase B：LLM 推理流量

### 4.1 概念与三层时延

**请求 → token → packet** 两次切分：

```
1 request   →   L_in 个 token  →   ⌈L_in × 4 / 1400⌉ 个 UDP packet
            (clipped Normal       (每包 350 个 token, 末包剩余)
             at the GS app)
```

每个 packet 带 32 字节 `LLMPacketTag`，里面有 `req_id`、`packet_id`、
`total_pkts`、`t_emit_ns`、`src_node_id`、`L_in`、`L_out_expected`。
compute SAT 上的 `LLMSinkApplication` 在 UDP 9999 监听，每收到一个包
就 PeekPacketTag、整数流式写一行到 CSV。

**三层时延都从同一份 CSV 算**：

| 层级 | 公式 | 含义 |
|---|---|---|
| **per-packet** | `recv_time_ns − t_emit_ns` | 单包端到端时延 |
| **per-token** | per-packet 值按 `tokens_in_packet` 复制成多样本 | 按 token 加权的时延 CDF |
| **per-request** | `max(recv) − t_emit` per req_id | request 的最末 token 到齐的时延 ← LLM 应用真正感受到的 |

### 4.2 文件清单

#### 4.2.1 phase_b 工作目录

```
extensions/phase_b/
├── README.md, phase_b_log.md, phase_b_result.md
│
├── install_module.sh                   ← rsync llm_workload/ → src/llm-workload/ + waf
├── llm_workload/                       ← C++ 模块源（master copy）
│   ├── wscript                         ← 依赖: core network internet applications basic-sim
│   ├── model/
│   │   ├── llm-packet-tag.{h,cc}       ← 32 B Tag
│   │   ├── llm-request-application.{h,cc}  ← Poisson + Normal + UDP send
│   │   └── llm-sink-application.{h,cc}     ← UDP recv + CSV 写盘
│   ├── helper/
│   │   ├── llm-request-helper.{h,cc}
│   │   ├── llm-sink-helper.{h,cc}
│   │   ├── llm-workload-schedule-reader.{h,cc}
│   │   └── llm-workload-scheduler.{h,cc}  ← main_satnet 集成入口
│   ├── examples/
│   │   ├── wscript
│   │   └── llm-workload-example.cc      ← 2 节点 P2P 隔离测试
│   └── test/llm-workload-test-suite.cc  ← Tag round-trip 单测
│
├── config_ns3_phase_b.properties       ← 单流默认配置
├── llm_workload_schedule.csv           ← 1 行 schedule (GS-Tokyo → SAT-894)
├── run_phase_b_experiment.sh
├── analyze_phase_b.py
│
├── runs/llm_run/logs_ns3/              ← 缓存的单流实验产物
│
└── scenarios/llm_workload/             ← 多 GS / 多 compute SAT 多流 E2E 场景
    ├── README.md                       ← 中文场景手册
    ├── llm_workload_schedule.csv       ← 5 行 schedule
    ├── config_ns3.properties, run.sh
    ├── analyze.py                      ← 三层时延分析
    ├── plot_latency_cdf.py             ← 三面板 CDF
    ├── plot_topology_llm.py            ← 单帧地图 + 流路径
    ├── plot_topology_anim.py           ← 50 帧实时动画 (含 compute SAT 累积请求计数器)
    ├── plot_request_timeline.py        ← 5 行 Gantt
    ├── make_plots.sh
    ├── result.md, flows.csv
    ├── gen_data/<network> → mixed_topology/gen_data/<network>  (symlink)
    └── plots/{latency_cdf, topology_llm, topology_anim, request_timeline}
```

#### 4.2.2 ns-3 source tree 里的副本

`install_module.sh` 会把 `llm_workload/` 整个 rsync 到：

```
ns3-sat-sim/simulator/src/llm-workload/   ← waf 实际编译这里
```

这两份代码**理论上完全同步**。要修改 module 源，请改 `extensions/phase_b/
llm_workload/` 下的 master copy，再跑 `install_module.sh` 重装重编。

#### 4.2.3 main_satnet.cc 集成点

```
scratch/main_satnet/main_satnet.cc 里追加 3 行:
  #include "ns3/llm-workload-scheduler.h"
  LlmWorkloadScheduler llmWorkloadScheduler(basicSimulation, topology->GetNodes());
  llmWorkloadScheduler.WriteResults();
```

`enable_llm_workload != true` 时 scheduler 构造函数静默 no-op，与上游
完全兼容。

### 4.3 如何跑

#### 4.3.1 装 / 重装 module + 重编 ns-3

```bash
cd /home/mark/spacesim/hypatia/extensions/phase_b
bash install_module.sh
# 输出:
#   == rsync source → src/llm-workload/
#   == waf configure
#   == waf build
#   -> example binary built: .../ns3.31-llm-workload-example-debug
```

完整编译头一次约 15 分钟；之后只改 llm_workload module 内部时增量编译
约 10 秒。

#### 4.3.2 隔离测试 module 本身（不经 Hypatia 拓扑）

```bash
cd /home/mark/spacesim/hypatia/ns3-sat-sim/simulator
PATH=/home/mark/spacesim/venv/bin:$PATH ./waf --run "llm-workload-example"
# 期望输出: tx_requests / tx_packets / rx_packets, 100% 送达
head /tmp/llm-workload-example-sink.csv
```

#### 4.3.3 跑 Phase B 单流主实验

```bash
cd /home/mark/spacesim/hypatia/extensions/phase_b
bash run_phase_b_experiment.sh
python analyze_phase_b.py --run-dir runs/llm_run --out phase_b_result.md
cat phase_b_result.md   # 期望 97-99% 送达, PASS
```

#### 4.3.4 跑多流完整场景（含三层时延分析 + 4 张图）

```bash
cd /home/mark/spacesim/hypatia/extensions/phase_b/scenarios/llm_workload
bash run.sh           # ~10 秒
bash make_plots.sh    # ~30 秒 (含 50 帧动画)
cat result.md
```

### 4.4 怎么改：12 种典型修改

| # | 想做什么 | 改哪 | 改完操作 |
|---|---|---|---|
| 1 | 改 λ（请求到达率） | schedule.csv 第 3 列 | `bash run.sh` |
| 2 | 改 L_in 分布（prompt 长度） | schedule.csv 第 4-7 列 (mean/std/min/max) | `bash run.sh` |
| 3 | 加新 (src,dst) flow | schedule.csv 加新行；如新 dst 没 augment 过，先 `augment_fstate.py --dst-sats <id>` | `bash run.sh` |
| 4 | 换 dst 到别的 compute SAT | schedule.csv 第 2 列 + 确认该 SAT 在 satellite_roles.txt 是 C + augment 过 | `bash run.sh` |
| 5 | 改 packet payload | schedule.csv 第 9 列。注意 UDP MTU 上限 1472 字节，超了会分片 | `bash run.sh` |
| 6 | 改 bytes_per_token（不再是 4 B） | schedule.csv 第 8 列。会影响 N_pkt 公式 | `bash run.sh` |
| 7 | 改 simulation_end_time | config_ns3.properties 里 `simulation_end_time_ns`；stop_time_ns 在 schedule 第 11 列也要相应改 | `bash run.sh` |
| 8 | 改 LLMPacketTag 字段（加新字段如 priority） | 改 `model/llm-packet-tag.{h,cc}` 里 `m_*` 成员 + Serialize/Deserialize/GetSerializedSize | **重编**：`install_module.sh` |
| 9 | 改 sink 输出 CSV 列 | 改 `model/llm-sink-application.cc::HandleRead` 里 `m_log << ... << '\n'` 那段 | **重编**：`install_module.sh` |
| 10 | 改 request 切包逻辑 | 改 `model/llm-request-application.cc::EmitRequest` | **重编**：`install_module.sh` |
| 11 | 改 lambda 分布（不再 Poisson） | `model/llm-request-application.cc::StartApplication` 把 ExponentialRandomVariable 换成别的 RNG | **重编**：`install_module.sh` |
| 12 | 加测试 case | 在 `test/llm-workload-test-suite.cc` 加 `TestCase` 子类 + `AddTestCase` | **重编**：`install_module.sh`；跑 `./waf --run "test-runner --suite=llm-workload"` |

> **重要约定**：永远改 `extensions/phase_b/llm_workload/` 下的 master
> copy（这是 git 友好的位置），不要直接改 `ns3-sat-sim/simulator/src/
> llm-workload/`——它会在下次 `install_module.sh` 时被覆盖。

---

## 五、改 ns-3 C++ 代码的标准流程

只有两个地方允许动 C++：

### 5.1 改 Phase A 的 endpoint patch

```
ns3-sat-sim/simulator/contrib/satellite-network/model/topology-satellite-network.cc
                ↑ 已有 ~25 行 Phase A 追加，在构造函数末尾
```

如果要改这一段（例如改 satellite_roles 文件的格式、加 type=G 第三种角色），
直接 edit，然后：

```bash
cd /home/mark/spacesim/hypatia/ns3-sat-sim/simulator
PATH=/home/mark/spacesim/venv/bin:$PATH ./waf
```

增量编译 ~10 秒（仅重编 topology-satellite-network 与依赖它的 lib）。

### 5.2 改 Phase B 的 llm-workload module

**标准流程**：

```bash
# 1. 在 master copy 改源
$EDITOR /home/mark/spacesim/hypatia/extensions/phase_b/llm_workload/model/llm-packet-tag.cc

# 2. 装 + 编
bash /home/mark/spacesim/hypatia/extensions/phase_b/install_module.sh

# 3. 跑实验
cd /home/mark/spacesim/hypatia/extensions/phase_b/scenarios/llm_workload
bash run.sh
```

### 5.3 加全新的 ns-3 module（Phase C 会做）

参考 `extensions/phase_b/llm_workload/wscript`，关键三件事：

1. `bld.create_ns3_module('<name>', ['core', 'network', ...])` —— 列依赖
2. `module.source = [...]` —— 所有 .cc 文件
3. `headers = bld(features='ns3header'); headers.source = [...]` —— 所有
   暴露的 .h 文件（外部代码用 `#include "ns3/<name>.h"` 才能找到）

模板在 basic-sim 的 wscript 里：`ns3-sat-sim/simulator/contrib/basic-sim/wscript`。

### 5.4 在 main_satnet.cc 集成

```
scratch/main_satnet/main_satnet.cc 改 3 处:
  - 顶部 include 你的 Scheduler header
  - 在原 schedulers 创建那段后面 new 你的 scheduler
  - 在原 WriteResults() 那段后面调你 scheduler 的 WriteResults()
```

每次改完跑 `./waf` 即可。

---

## 六、常见错误与排查

### 6.1 `Invalid to-endpoint for a schedule entry based on topology: <ID>`

ns-3 启动时 schedule-reader 拒了 SAT ID。原因之一：

- **`satellite_roles.txt` 没在 run dir 里** —— Phase A patch 读不到，没把
  compute SAT 加进 m_endpoints。**修法**：确认 `run.sh` / `run_phase_b_*`
  里有 `ln -sf $PHASE_A/satellite_roles.txt $RUN_DIR/satellite_roles.txt`。
- **roles 文件里目标 SAT 不是 C** —— 改 roles 文件或换 dst。

### 6.2 `String # PHASE_A_AUGMENT begin ... has a ,-split of 1 != 5`

ns-3 fstate parser SIGIOT 在注释行上。这是 Phase B 之前 augment_fstate 早期版本
留下的注释行。**修法**：跑 `augment_fstate.py --rewrite --dst-sats <id>`，
它会把所有 `^#` 行扫掉。或者手工 `sed -i '/^#/d' fstate_*.txt`。

### 6.3 `Necessary parameter 'enable_isl_utilization_tracking' is not set.`

config 缺一个 topology 要求的属性。**修法**：在 config 里加
`enable_isl_utilization_tracking=false`。

### 6.4 `ISL ((0,10)) length 10608m > max_isl 5016m at t=0ns`

satgenpy state-gen 检查 ISL 几何超界。在小拓扑（如 4 plane × 5 sat @
550km）会触发。**修法**有三种：
1. 把 N_sats_per_plane 加到 ≥ 9（in-plane 间距 ≤ 42°）；
2. 增加 N_orbs（Walker-Star plane 间距 180°/N）；
3. **legacy 风格**：在 `build_state.py` 里把 `MAX_ISL_LENGTH_M = 1_000_000_000`
   禁掉长度检查（仿真传播延迟仍用真实 distance/c，物理一致）。
   mixed_topology 用的就是这个办法。

### 6.5 `Min. satellites in range = 0` （某 GS 永远看不到星）

挑的 GS 经度上没轨道经过。**修法**：换 GS 或加高度。1500 km 高度比
550 km 单星覆盖面积大 7.4 倍，60 颗星就能 100% 覆盖 ±53° 纬度带。

### 6.6 fstate_*.txt 几乎全是截断（每个只有几行）

satgenpy state-gen worker thread 异常退出。这在 Starlink-550 的某次跑
确实发生过。**修法**：
1. 在小拓扑（17-60 sat）先跑通验证 satgenpy 自身没问题；
2. 重跑大拓扑 state-gen；
3. **临时绕过**：把 `dynamic_state_update_interval_ns` 改成 ≥
   simulation_end_time_ns，让 ns-3 只在 t=0 读一次完整的 fstate_0。

### 6.7 waf build 报 `undefined reference to ns3::BasicSimulation::*`

新加的 module 的 wscript 没列 `basic-sim` 依赖。**修法**：在
`module = bld.create_ns3_module('<name>', [...])` 列表里加
`'basic-sim'`。

### 6.8 编译时 `error: ‘convert_path’ ... in C++14 digit separator`

不是 ns-3 自己的问题——是测试代码用了 `1'234'567ULL` 数字分隔符（C++14+）。
ns-3 用 `-std=c++11`。**修法**：去掉撇号写连续数字。

---

## 七、Phase C 接续点

数据契约已经稳：

1. **`satellite_roles.txt`** 是 compute / transit 角色的单点真相，C++
   端 / Python augment / pick / analyze 全读它。Phase C 加 type=G
   "gather coordinator" 等新角色直接扩 enum 即可。
2. **`LLMPacketTag`** 已带 `L_out_expected` 字段（Phase B 写 0 占位）。
   Phase C 让 GS 端的 LLMRequestApplication 填实际值，让 compute SAT
   端的 LLMComputeApplication 根据它生成响应流。
3. **gather barrier 触发条件**：本场景实测 per-request completion
   latency = per-packet + (N_pkt - 1) × 1.14 ms（GSL 序列化），p95 ≤
   3.5 ms。Phase C 在 `LLMSinkApplication::HandleRead` 里维护
   `unordered_map<req_id, GatherState>`，`received == total_pkts`
   时 fire `m_on_gather_complete` 回调即可。
4. **新增 `LLMComputeApplication`** 订阅 (3) 的回调 → schedule
   `T_prefill(L_in)` 计时器 → 到期后用 LLMRequestApplication 反向
   (compute_sat_node → gs_node)，复用现有 schedule 格式。

最干净的 Phase C 范围 = workload 改成请求/响应双向，**不再动 C++ 任何
地方**——本场景把所有需要的钩子都已经备好了。

---

## 附录：根目录的两份文档

- `/home/mark/spacesim/使用手册.md` —— 整个 Hypatia 仓库的中文使用手册
  （从零搭环境、跑 IMC paper 实验、跑测试）
- `/home/mark/spacesim/hypatia/extensions/phase_a/功能说明.md` —— Phase A
  专项中文文档
- 本文 `extensions/使用手册.md` —— Phase A + B 综合手册（你正在读）

英文版：每个 phase / scenario 目录下的 `README.md`。