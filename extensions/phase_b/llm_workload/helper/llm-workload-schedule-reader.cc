#include "ns3/llm-workload-schedule-reader.h"

#include <fstream>
#include <sstream>
#include <stdexcept>

namespace ns3 {

static std::vector<std::string> split_comma(const std::string &line) {
    std::vector<std::string> out;
    std::string field;
    std::istringstream ss(line);
    while (std::getline(ss, field, ',')) {
        out.push_back(field);
    }
    return out;
}

std::vector<LlmWorkloadEntry>
read_llm_workload_schedule(const std::string &path,
                           int64_t simulation_end_time_ns)
{
    std::vector<LlmWorkloadEntry> rows;
    std::ifstream f(path);
    if (!f.is_open()) {
        throw std::runtime_error("Cannot open LLM workload schedule: " + path);
    }
    std::string line;
    int lineno = 0;
    while (std::getline(f, line)) {
        ++lineno;
        if (line.empty() || line[0] == '#') continue;
        std::vector<std::string> parts = split_comma(line);
        if (parts.size() < 11) {
            throw std::runtime_error("Malformed LLM workload schedule line "
                                     + std::to_string(lineno) + ": " + line);
        }
        LlmWorkloadEntry e;
        e.src_gs_node_id          = std::stoll(parts[0]);
        e.dst_compute_sat_node_id = std::stoll(parts[1]);
        e.lambda_req_per_sec      = std::stod (parts[2]);
        e.L_in_mean               = std::stod (parts[3]);
        e.L_in_std                = std::stod (parts[4]);
        e.L_in_min                = (uint32_t) std::stoul(parts[5]);
        e.L_in_max                = (uint32_t) std::stoul(parts[6]);
        e.bytes_per_token         = (uint32_t) std::stoul(parts[7]);
        e.packet_payload          = (uint32_t) std::stoul(parts[8]);
        e.start_time_ns           = std::stoll(parts[9]);
        e.stop_time_ns            = std::stoll(parts[10]);

        // Sanity checks.
        if (e.lambda_req_per_sec <= 0.0) {
            throw std::runtime_error("LLM schedule line "
                + std::to_string(lineno) + ": lambda must be > 0");
        }
        if (e.start_time_ns < 0 || e.stop_time_ns <= e.start_time_ns) {
            throw std::runtime_error("LLM schedule line "
                + std::to_string(lineno) + ": invalid time window");
        }
        if (e.stop_time_ns > simulation_end_time_ns) {
            e.stop_time_ns = simulation_end_time_ns;  // clamp
        }
        if (e.L_in_max < e.L_in_min) {
            throw std::runtime_error("LLM schedule line "
                + std::to_string(lineno) + ": L_in_max < L_in_min");
        }
        rows.push_back(e);
    }
    return rows;
}

} // namespace ns3