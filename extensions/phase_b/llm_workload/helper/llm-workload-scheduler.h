/*
 * LlmWorkloadScheduler — Hypatia main_satnet integration class.
 *
 * Constructed once from main_satnet.cc with (basicSimulation, allNodes).
 * Reads the `enable_llm_workload` property. If enabled, reads the
 * schedule CSV, installs LLMRequestApplication on each src GS node, and
 * installs a single LLMSinkApplication on each unique dst compute SAT
 * node. All applications StartApplication() at the row's start_time_ns
 * and StopApplication() at stop_time_ns.
 *
 * IP resolution: for each dst compute SAT node, the first non-loopback
 * IPv4 address on that node is used as the destination of the UDP
 * stream. (Hypatia assigns a /24 to every GSL interface during topology
 * construction, so this is deterministic.)
 *
 * WriteResults() is called after Simulator::Run(); it currently just
 * prints a summary of how many requests / packets each Tx-side
 * application emitted and how many each sink received.
 */
#ifndef LLM_WORKLOAD_SCHEDULER_H
#define LLM_WORKLOAD_SCHEDULER_H

#include <map>
#include <set>
#include <string>
#include <vector>

#include "ns3/core-module.h"
#include "ns3/node-container.h"

#include "ns3/basic-simulation.h"

#include "ns3/llm-workload-schedule-reader.h"
#include "ns3/llm-request-application.h"
#include "ns3/llm-sink-application.h"

namespace ns3 {

class LlmWorkloadScheduler
{
public:
    LlmWorkloadScheduler(Ptr<BasicSimulation> basicSimulation,
                         const NodeContainer &all_nodes);
    void WriteResults();

private:
    Ptr<BasicSimulation>            m_basicSimulation;
    NodeContainer                   m_all_nodes;
    bool                            m_enabled;
    int64_t                         m_simulation_end_time_ns;
    std::vector<LlmWorkloadEntry>   m_schedule;
    std::vector<Ptr<LLMRequestApplication>> m_request_apps;
    std::map<int64_t, Ptr<LLMSinkApplication>> m_sink_apps;  // node id -> sink
    std::string                     m_log_filename_template;
};

} // namespace ns3

#endif // LLM_WORKLOAD_SCHEDULER_H