"""
llm_events_replay/build_and_run.py — verification driver for trace-replay
mode.

Builds a run dir that points the ns-3 LLM workload module at pre-staged
per-GS event traces (16-column schedule), then invokes waf to run the
simulator.

Reuses the same constellation gen_data as the ``llm_workload`` scenario.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SPACESIM = HERE.parent.parent  # extensions/spacesim
sys.path.insert(0, str(SPACESIM.parent))  # so 'from spacesim...' resolves

from spacesim.workload.events_replay import install_events_replay  # noqa: E402

PHASE_A_MIXED = Path("/home/mark/spacesim/hypatia/extensions/spacesim/scenarios/mixed_topology")
SIMULATOR = Path("/home/mark/spacesim/hypatia/ns3-sat-sim/simulator")
VENV_BIN = Path("/home/mark/spacesim/venv/bin")
NETWORK = "tiny_walker_1500_isls_plus_grid_5cities_algorithm_free_one_only_over_isls"


class _Sim:
    duration_seconds = 5
    update_interval_ms = 100


class _Workload:
    L_in_mean = 500.0
    L_in_std = 100.0
    L_in_min = 1
    L_in_max = 4000
    bytes_per_token = 4
    packet_payload = 1400


class _Compute:
    alpha_ns_per_input_token = 100_000
    beta_ns_per_output_token = 50_000
    gamma_ns = 10_000_000


class _Cfg:
    simulation = _Sim()
    workload = _Workload()
    compute = _Compute()


def _topology_hash(self):
    return "events_replay_verify"
_Cfg.topology_hash = _topology_hash


def main() -> int:
    cfg = _Cfg()

    gen_data_link = HERE / "gen_data" / NETWORK
    gen_data_link.parent.mkdir(parents=True, exist_ok=True)
    if not gen_data_link.exists():
        os.symlink(PHASE_A_MIXED / "gen_data" / NETWORK, gen_data_link)
    dyn_dir = gen_data_link / "dynamic_state_100ms_for_5s"
    roles_path = PHASE_A_MIXED / "satellite_roles.txt"
    for p in (dyn_dir, roles_path):
        if not p.exists():
            print(f"FATAL: prerequisite missing: {p}", file=sys.stderr)
            return 1

    run_dir = HERE / "run"
    if run_dir.exists():
        shutil.rmtree(run_dir)
    (run_dir / "logs_ns3").mkdir(parents=True)

    # Topology has 60 sats and 5 GS at node ids 60..64. The traffic
    # generator gives us gs_idx 0..4 for the first 5 cities — map them
    # 1:1 to node ids 60..64.
    num_sats = 60
    n_rows = install_events_replay(
        cfg,
        per_gs_dir=HERE / "per_gs",
        run_dir=run_dir,
        num_satellites=num_sats,
        gs_idx_to_node_id=lambda i: num_sats + i,
        roles_path=roles_path,
        dst_strategy="per_gs_round_robin",
        stage_mode="copy",
    )
    print(f"  > schedule rows: {n_rows}")

    # Stage roles + config.
    os.symlink(roles_path, run_dir / "satellite_roles.txt")

    config_lines = [
        "simulation_end_time_ns=5000000000",
        "simulation_seed=246810",
        f'satellite_network_dir="../gen_data/{NETWORK}"',
        f'satellite_network_routes_dir="../gen_data/{NETWORK}/dynamic_state_100ms_for_5s"',
        "dynamic_state_update_interval_ns=100000000",
        "isl_data_rate_megabit_per_s=10.0",
        "gsl_data_rate_megabit_per_s=10.0",
        "isl_max_queue_size_pkts=100",
        "gsl_max_queue_size_pkts=100",
        "enable_isl_utilization_tracking=false",
        "tcp_socket_type=TcpNewReno",
        "enable_llm_workload=true",
        'llm_workload_schedule_filename="llm_workload_schedule.csv"',
        'llm_workload_log_filename="llm.csv"',
        f"compute_alpha_ns_per_input_token={cfg.compute.alpha_ns_per_input_token}",
        f"compute_beta_ns_per_output_token={cfg.compute.beta_ns_per_output_token}",
        f"compute_gamma_ns={cfg.compute.gamma_ns}",
        "enable_tcp_flow_scheduler=false",
        "enable_udp_burst_scheduler=false",
        "enable_pingmesh_scheduler=false",
        "",
    ]
    (run_dir / "config_ns3.properties").write_text("\n".join(config_lines))

    print("  > running ns-3...")
    env = os.environ.copy()
    env["PATH"] = f"{VENV_BIN}:{env.get('PATH', '')}"
    console_log = run_dir / "logs_ns3" / "console.txt"
    with open(console_log, "w") as cfh:
        p = subprocess.run(
            ["./waf", "--run", f"main_satnet --run_dir='{run_dir}'"],
            cwd=SIMULATOR, env=env, stdout=cfh, stderr=subprocess.STDOUT,
        )
    print(f"  > exit code: {p.returncode}")
    print(f"  > console: {console_log}")
    return p.returncode


if __name__ == "__main__":
    sys.exit(main())
