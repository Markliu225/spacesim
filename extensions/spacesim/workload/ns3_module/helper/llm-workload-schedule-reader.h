/*
 * LlmWorkloadScheduleReader — read llm_workload_schedule.csv.
 *
 * Three column counts accepted:
 *
 *   11 cols — legacy Phase B (no L_out distribution; defaults applied)
 *   15 cols — Phase C distribution-driven workload
 *   16 cols — same as 15 + trailing ``events_filename`` (relative to
 *             run dir). When non-empty, the request app replays events
 *             from this file instead of Poisson-sampling from the
 *             distribution columns. The distribution fields are then
 *             ignored except where noted.
 *
 * Lines beginning with '#' or empty lines are ignored.
 */
#ifndef LLM_WORKLOAD_SCHEDULE_READER_H
#define LLM_WORKLOAD_SCHEDULE_READER_H

#include <cstdint>
#include <string>
#include <vector>

namespace ns3 {

struct LlmWorkloadEntry {
    int64_t src_gs_node_id;
    int64_t dst_compute_sat_node_id;
    double  lambda_req_per_sec;
    double  L_in_mean;
    double  L_in_std;
    uint32_t L_in_min;
    uint32_t L_in_max;
    // Phase C: L_out distribution. Phase B-only callers (11-column CSV)
    // get defaults (mean=200, std=50, [1,1000]).
    double  L_out_mean;
    double  L_out_std;
    uint32_t L_out_min;
    uint32_t L_out_max;
    uint32_t bytes_per_token;
    uint32_t packet_payload;
    int64_t start_time_ns;
    int64_t stop_time_ns;
    // Optional: name of a per-GS events CSV (relative to the run dir).
    // When non-empty the request app replays each row's t_emit_ns and
    // L_in directly; the distribution fields above are ignored.
    std::string events_filename;
};

std::vector<LlmWorkloadEntry> read_llm_workload_schedule(
    const std::string &path,
    int64_t simulation_end_time_ns);

} // namespace ns3

#endif // LLM_WORKLOAD_SCHEDULE_READER_H