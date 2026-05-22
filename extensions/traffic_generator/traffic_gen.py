"""
traffic_gen.py — main CLI for the LLM traffic generator.

Wires the Azure trace fitter and the NHPP thinning sampler to a set of
ground stations and writes one events.csv covering the requested
simulation duration.

Usage
-----

    python traffic_gen.py \
        --azure-trace path/to/AzureLLMInferenceTrace_conv_1week.csv \
        --gs-config ground_stations.json \
        --duration-sec 86400 \
        --output events.csv \
        --seed 42

If ``--azure-trace`` is omitted (or the path is missing), the script
generates a small synthetic Azure-format CSV in the output dir and
fits on that — useful for sanity checks and CI runs that don't have
the real ~1 GB trace.

If ``--report`` is passed, also writes a ``generator_report.md`` plus
two PNG plots next to ``--output``: one of ``d(τ)`` and one of per-GS
hourly event counts.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional

import numpy as np

# Support both `python traffic_gen.py …` (script form) and
# `python -m traffic_generator.traffic_gen …` (module form).
try:
    from .trace_fitter import AzureTraceFitter, make_synthetic_azure_trace
    from .nhpp_generator import generate_nhpp_events
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from trace_fitter import AzureTraceFitter, make_synthetic_azure_trace
    from nhpp_generator import generate_nhpp_events


# ---- GS config ----------------------------------------------------------


@dataclass
class GroundStation:
    gs_idx: int
    name: str
    lat: float
    lon: float
    peak_lambda: float


def load_gs_config(path: Path) -> List[GroundStation]:
    """Accept JSON (list of dicts) or CSV with the required columns."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"GS config not found: {p}")
    if p.suffix.lower() == ".json":
        with open(p) as f:
            data = json.load(f)
    elif p.suffix.lower() == ".csv":
        with open(p) as f:
            data = list(csv.DictReader(f))
    else:
        raise ValueError(f"Unsupported GS config suffix: {p.suffix}")

    gs_list: List[GroundStation] = []
    for i, row in enumerate(data):
        try:
            gs_list.append(GroundStation(
                gs_idx=int(row.get("gs_idx", i)),
                name=str(row.get("name", f"gs-{i}")),
                lat=float(row["lat"]),
                lon=float(row["lon"]),
                peak_lambda=float(row["peak_lambda"]),
            ))
        except (KeyError, ValueError) as exc:
            raise ValueError(f"GS config row {i} malformed: {row!r} ({exc})")
    if not gs_list:
        raise ValueError(f"GS config {p} has zero entries")
    return gs_list


# ---- time helpers -------------------------------------------------------


def local_time(utc_sec: float, lon_deg: float) -> float:
    """Local solar time at longitude ``lon_deg`` (east positive), in hours
    in [0, 24). Treats simulation t=0 as UTC midnight.
    """
    return (float(utc_sec) / 3600.0 + float(lon_deg) / 15.0) % 24.0


# ---- per-GS generation --------------------------------------------------


def generate_for_ground_station(
    gs: GroundStation,
    duration_sec: float,
    fitter: AzureTraceFitter,
    rng: np.random.Generator,
) -> List[dict]:
    """Generate this GS's events for ``[0, duration_sec)`` (UTC seconds
    relative to sim t=0).

    Returns a list of dicts with keys:
        req_id (None — filled in by main once across all GSes),
        src_gs_idx, t_emit_ns, L_in, L_out
    """
    def rate_func(t: float) -> float:
        return gs.peak_lambda * fitter.rate_shape(local_time(t, gs.lon))

    times = generate_nhpp_events(duration_sec, rate_func, rng)
    events: List[dict] = []
    for t in times:
        tau = local_time(t, gs.lon)
        L_in, L_out = fitter.sample_length(tau, rng)
        events.append({
            "req_id": None,
            "src_gs_idx": gs.gs_idx,
            "t_emit_ns": int(round(t * 1e9)),
            "L_in": int(L_in),
            "L_out": int(L_out),
        })
    return events


# ---- main driver --------------------------------------------------------


def write_events_csv(events: Iterable[dict], output_csv: Path,
                     chunksize: int = 100_000) -> None:
    """Stream-write the events list to CSV in chunks (memory-friendly)."""
    output_csv = Path(output_csv)
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    with open(output_csv, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["req_id", "src_gs_idx", "t_emit_ns", "L_in", "L_out"])
        buf: List[List] = []
        for e in events:
            buf.append([e["req_id"], e["src_gs_idx"], e["t_emit_ns"],
                        e["L_in"], e["L_out"]])
            if len(buf) >= chunksize:
                w.writerows(buf)
                buf.clear()
        if buf:
            w.writerows(buf)


def _slugify_name(name: str) -> str:
    """Filesystem-safe slug for the GS name part of per-GS filenames."""
    return "".join(c if c.isalnum() else "_" for c in name).strip("_").lower()


def generate(
    azure_trace: Optional[Path],
    gs_config: Path,
    duration_sec: int,
    output_dir: Path,
    *,
    seed: int = 42,
    max_trace_rows: Optional[int] = None,
    report: bool = False,
) -> dict:
    """End-to-end pipeline. Returns a summary dict.

    Emits **one CSV per ground station** under ``output_dir/per_gs/``:
    ``events_gs<idx>_<slug>.csv``. Each file contains only that GS's
    events, sorted by ``t_emit_ns``, with a GS-local ``req_id`` running
    0..N-1. The simulator consumes these per-GS files directly — there
    is no merged file (events for two different GSes interleave at
    Hypatia's runtime via Simulator::Schedule, not at CSV-load time).
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    per_gs_dir = output_dir / "per_gs"
    per_gs_dir.mkdir(parents=True, exist_ok=True)

    # 1. Resolve trace input.
    if azure_trace is None or not Path(azure_trace).exists():
        synthetic_path = output_dir / "synthetic_azure_trace.csv"
        print(f"[traffic_gen] no real Azure trace at {azure_trace!r}; "
              f"generating synthetic at {synthetic_path}")
        make_synthetic_azure_trace(
            synthetic_path, n_rows=100_000, days=2,
            rng=np.random.default_rng(seed + 10),
        )
        azure_trace = synthetic_path
        synthetic_used = True
    else:
        synthetic_used = False

    # 2. Fit.
    print(f"[traffic_gen] fitting trace: {azure_trace}")
    fitter = AzureTraceFitter(azure_trace, max_rows=max_trace_rows)
    stats = fitter.fit()
    print(f"  rows used = {stats.n_used}/{stats.n_rows}, "
          f"span {stats.span_hours:.1f}h, "
          f"L_in mean={stats.L_in_overall_mean:.0f} "
          f"L_out mean={stats.L_out_overall_mean:.0f}")

    # 3. Per-GS NHPP generation. As soon as a GS finishes generating, we
    #    sort its events locally, assign GS-local req_ids 0..N-1, and
    #    write its dedicated CSV.
    rng = np.random.default_rng(seed)
    gs_list = load_gs_config(gs_config)
    print(f"[traffic_gen] {len(gs_list)} ground stations, duration {duration_sec}s")
    print(f"[traffic_gen] per-GS CSV dir: {per_gs_dir}")
    all_events: List[dict] = []  # only used for the report's hourly bins
    per_gs_counts: dict = {}
    per_gs_paths: dict = {}
    for gs in gs_list:
        events = generate_for_ground_station(gs, float(duration_sec),
                                             fitter, rng)
        events.sort(key=lambda e: e["t_emit_ns"])
        # GS-local req_id; downstream tools read one GS's stream at a
        # time and want consecutive ids per stream.
        for i, e in enumerate(events):
            e["req_id"] = i
        per_gs_counts[gs.gs_idx] = (gs.name, len(events))

        slug = _slugify_name(gs.name)
        per_gs_csv = per_gs_dir / f"events_gs{gs.gs_idx}_{slug}.csv"
        write_events_csv(events, per_gs_csv)
        per_gs_paths[gs.gs_idx] = per_gs_csv

        print(f"  GS-{gs.gs_idx} {gs.name:<12s}  λ_peak={gs.peak_lambda:5.1f}  "
              f"-> {len(events):7d} events  -> {per_gs_csv.name}")
        if report:
            all_events.extend(events)

    if report:
        all_events.sort(key=lambda e: e["t_emit_ns"])

    summary = {
        "azure_trace": str(azure_trace),
        "synthetic_used": synthetic_used,
        "duration_sec": duration_sec,
        "seed": seed,
        "n_events": sum(c for _, c in per_gs_counts.values()),
        "per_gs_counts": per_gs_counts,
        "per_gs_paths": per_gs_paths,
        "per_gs_dir": per_gs_dir,
        "fit_stats": stats,
        "gs_list": gs_list,
        "events": all_events if report else None,
        "output_dir": output_dir,
    }

    if report:
        report_path = output_dir / "generator_report.md"
        write_report(report_path, summary)
        print(f"[traffic_gen] wrote {report_path}")

    return summary


# ---- report -------------------------------------------------------------


def write_report(report_path: Path, summary: dict) -> None:
    """Render generator_report.md plus the two diagnostic PNG plots."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    output_dir = report_path.parent
    fit = summary["fit_stats"]
    gs_list = summary["gs_list"]
    events = summary["events"]
    duration_sec = summary["duration_sec"]

    # ---- plot 1: d(τ) ----
    fig, ax = plt.subplots(figsize=(7.5, 3.2))
    hours = np.arange(24)
    ax.plot(hours, fit.d, marker="o", color="#2177b0", lw=1.8)
    ax.set_xticks(np.arange(0, 25, 3))
    ax.set_xlim(-0.5, 23.5)
    ax.set_ylim(0, 1.05)
    ax.set_xlabel("hour of day (UTC, from trace)")
    ax.set_ylabel("d(τ) — normalised rate")
    ax.set_title(f"Trace-derived diurnal shape (peak hour ≈ {int(np.argmax(fit.d)):d}:00 UTC)")
    ax.grid(True, linestyle="--", alpha=0.4)
    plot_d_path = output_dir / "diurnal_shape.png"
    fig.tight_layout()
    fig.savefig(plot_d_path, dpi=140)
    plt.close(fig)

    # ---- plot 2: per-GS hourly count over the sim window ----
    # Bin events by sim-UTC hour.
    n_hours = max(1, int(np.ceil(duration_sec / 3600)))
    per_gs_hourly = {}
    for gs in gs_list:
        per_gs_hourly[gs.gs_idx] = np.zeros(n_hours, dtype=np.int64)
    for e in events:
        bin_idx = min(n_hours - 1, e["t_emit_ns"] // (3600 * 10**9))
        per_gs_hourly[e["src_gs_idx"]][bin_idx] += 1

    # Plot width grows with GS count so the legend stays legible.
    fig_w = 9.5 + min(4.0, max(0.0, (len(gs_list) - 5) * 0.4))
    fig, ax = plt.subplots(figsize=(fig_w, 4.8))
    colors = ["#2177b0", "#fc7f2b", "#2f9e37", "#d42a2d", "#80007F",
              "#8a554c", "#e079be", "#7d7d7d", "#bcbd22", "#17becf",
              "#1f77b4", "#ff7f0e", "#9467bd", "#e377c2", "#7f7f7f"]
    for i, gs in enumerate(gs_list):
        x = np.arange(n_hours)
        y = per_gs_hourly[gs.gs_idx]
        ax.plot(x, y, marker="o", markersize=4, lw=1.7,
                color=colors[i % len(colors)],
                label=f"GS-{gs.gs_idx} {gs.name} (lon {gs.lon:+.1f})")
    ax.set_xticks(np.arange(0, n_hours + 1, max(1, n_hours // 12)))
    ax.set_xlim(-0.5, n_hours - 0.5)
    ax.set_xlabel("hour of simulation (UTC, t=0 = UTC midnight)")
    ax.set_ylabel("events / hour")
    ax.set_title("Per-GS event rate vs UTC hour\n"
                 "(expect peaks aligned to each GS's local noon-afternoon)")
    ax.grid(True, linestyle="--", alpha=0.4)
    # Legend outside on the right so 10+ lines don't sit on top of the data.
    ax.legend(fontsize=8, loc="center left",
              bbox_to_anchor=(1.005, 0.5), framealpha=0.95)
    plot_gs_path = output_dir / "per_gs_hourly.png"
    fig.tight_layout()
    fig.savefig(plot_gs_path, dpi=140, bbox_inches="tight")
    plt.close(fig)

    # ---- markdown ----
    md = []
    md.append("# Traffic Generator Report\n")
    md.append("## Input\n")
    md.append(f"- Azure trace: `{summary['azure_trace']}`"
              + (" **(synthetic — no real trace supplied)**"
                 if summary['synthetic_used'] else ""))
    md.append(f"- Duration:   {duration_sec} s ({duration_sec/3600:.2f} h)")
    md.append(f"- Seed:       {summary['seed']}")
    md.append(f"- GS count:   {len(gs_list)}")
    md.append("")

    md.append("## Fit\n")
    md.append("| metric | value |")
    md.append("|---|---:|")
    md.append(f"| rows in trace | {fit.n_rows:,} |")
    md.append(f"| rows used | {fit.n_used:,} |")
    md.append(f"| rows dropped (invalid) | {fit.n_dropped_invalid:,} |")
    md.append(f"| time span (h) | {fit.span_hours:.1f} |")
    md.append(f"| trace TIMESTAMP min | `{fit.timestamp_min}` |")
    md.append(f"| trace TIMESTAMP max | `{fit.timestamp_max}` |")
    md.append(f"| L_in mean | {fit.L_in_overall_mean:.0f} |")
    md.append(f"| L_out mean | {fit.L_out_overall_mean:.0f} |")
    md.append(f"| peak hour (UTC) | {int(np.argmax(fit.d)):d}:00 |")
    md.append("")

    md.append("### Per-bucket sample sizes\n")
    md.append("| hour | samples | d(τ) |")
    md.append("|---:|---:|---:|")
    for h in range(24):
        md.append(f"| {h:02d} | {int(fit.per_bucket_size[h]):,} | "
                  f"{fit.d[h]:.3f} |")
    md.append("")

    md.append(f"![diurnal shape]({plot_d_path.name})\n")

    md.append("## Per-GS expected vs actual\n")
    mean_d = float(fit.d.mean())
    md.append("| GS | name | lon (deg) | λ_peak (req/s) | "
              "expected | actual | actual / expected |")
    md.append("|---:|---|---:|---:|---:|---:|---:|")
    total_actual = 0
    total_expected = 0.0
    for gs in gs_list:
        actual = summary["per_gs_counts"][gs.gs_idx][1]
        expected = gs.peak_lambda * duration_sec * mean_d
        total_actual += actual
        total_expected += expected
        ratio = (actual / expected) if expected > 0 else float("nan")
        md.append(f"| {gs.gs_idx} | {gs.name} | {gs.lon:+.2f} | {gs.peak_lambda:.2f} "
                  f"| {expected:,.1f} | {actual:,} | {ratio:.3f} |")
    md.append(f"| **all** | — | — | — | **{total_expected:,.1f}** | "
              f"**{total_actual:,}** | "
              f"**{total_actual / max(total_expected, 1):.3f}** |")
    md.append("")
    md.append(f"Mean of d(τ) over 24h: **{mean_d:.3f}**. The 'expected' "
              f"column uses `λ_peak · duration · mean(d)`, which assumes "
              f"the sim window covers a full day. For shorter / longer "
              f"windows the expectation drifts by integrating d(τ) only "
              f"over the covered hours.\n")

    md.append(f"![per-GS hourly]({plot_gs_path.name})\n")

    md.append("## Sanity check\n")
    if events:
        ts_ns = np.array([e["t_emit_ns"] for e in events])
        md.append(f"- Events: **{len(events):,}**")
        md.append(f"- First t_emit_ns: {int(ts_ns.min()):,}  "
                  f"({ts_ns.min() / 1e9:.2f} s)")
        md.append(f"- Last  t_emit_ns: {int(ts_ns.max()):,}  "
                  f"({ts_ns.max() / 1e9:.2f} s)")
        md.append(f"- req_id range: 0..{len(events)-1}")
        md.append(f"- monotone in t_emit_ns: "
                  f"**{bool(np.all(np.diff(ts_ns) >= 0))}**")

    report_path.write_text("\n".join(md))


# ---- CLI ----------------------------------------------------------------


def _cli() -> int:
    p = argparse.ArgumentParser(
        description="Generate per-ground-station LLM request events "
                    "from an Azure trace shape.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--azure-trace", type=Path, default=None,
                   help="Path to the Azure LLM Inference trace CSV. "
                        "If missing, a synthetic trace is generated.")
    p.add_argument("--gs-config", type=Path, required=True,
                   help="GS config (JSON list of dicts or CSV with "
                        "gs_idx/lat/lon/peak_lambda columns).")
    p.add_argument("--duration-sec", type=int, required=True,
                   help="Simulation duration in seconds.")
    p.add_argument("--output-dir", type=Path, required=True,
                   help="Directory to write per-GS CSVs (under per_gs/) "
                        "and the optional report.")
    p.add_argument("--seed", type=int, default=42,
                   help="RNG seed.")
    p.add_argument("--max-trace-rows", type=int, default=None,
                   help="Cap on rows read from the Azure trace "
                        "(use for fast smoke runs on large traces).")
    p.add_argument("--report", action="store_true",
                   help="Also write generator_report.md plus diagnostic PNGs.")
    args = p.parse_args()

    generate(
        azure_trace=args.azure_trace,
        gs_config=args.gs_config,
        duration_sec=args.duration_sec,
        output_dir=args.output_dir,
        seed=args.seed,
        max_trace_rows=args.max_trace_rows,
        report=args.report,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
