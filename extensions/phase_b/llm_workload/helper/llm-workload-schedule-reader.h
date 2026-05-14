/*
 * LlmWorkloadScheduleReader — read llm_workload_schedule.csv.
 *
 * CSV row format (11 columns):
 *
 *   src_gs_node_id,dst_compute_sat_node_id,lambda_req_per_sec,
 *   L_in_mean,L_in_std,L_in_min,L_in_max,bytes_per_token,packet_payload,
 *   start_time_ns,stop_time_ns
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
    uint32_t bytes_per_token;
    uint32_t packet_payload;
    int64_t start_time_ns;
    int64_t stop_time_ns;
};

std::vector<LlmWorkloadEntry> read_llm_workload_schedule(
    const std::string &path,
    int64_t simulation_end_time_ns);

} // namespace ns3

#endif // LLM_WORKLOAD_SCHEDULE_READER_H