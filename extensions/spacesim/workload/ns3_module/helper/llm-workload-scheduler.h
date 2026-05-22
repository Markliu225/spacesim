/*
 * LlmWorkloadScheduler — Hypatia main_satnet integration.
 *
 * TCP-only design (v2). For each schedule entry it installs (once per
 * dst compute SAT) a GatherApplication + ComputeApplication pair, and
 * (per src GS) an LLMRequestApplication.
 *
 * Wiring:
 *   - GatherApp accepts incoming TCP connections, reconstructs the
 *     request, then fires GatherCompleteCallback → ComputeApp.OnGatherComplete
 *     (passing the live socket).
 *   - ComputeApp samples L_out (Option B), queues, and after T_compute
 *     sends the response payload on the same socket and ShutdownSend.
 *   - LLMRequestApp on the GS half-receives the response on the same
 *     socket, logs first/last byte times to llm_response_node<gs>.csv.
 *
 * The v1 Phase-B "sink-only" mode is gone. `enable_llm_response_loop`
 * is accepted in config for back-compat but ignored: the response loop
 * is always on.
 */
#ifndef LLM_WORKLOAD_SCHEDULER_H
#define LLM_WORKLOAD_SCHEDULER_H

#include <map>
#include <string>
#include <vector>

#include "ns3/core-module.h"
#include "ns3/node-container.h"

#include "ns3/basic-simulation.h"

#include "ns3/llm-workload-schedule-reader.h"
#include "ns3/llm-request-application.h"
#include "ns3/gather-application.h"
#include "ns3/compute-application.h"

namespace ns3 {

class LlmWorkloadScheduler
{
public:
    LlmWorkloadScheduler(Ptr<BasicSimulation> basicSimulation,
                         const NodeContainer &all_nodes);
    void WriteResults();

private:
    void InstallSchedule();

    Ptr<BasicSimulation>          m_basicSimulation;
    NodeContainer                 m_all_nodes;
    bool                          m_enabled;
    int64_t                       m_simulation_end_time_ns;
    std::vector<LlmWorkloadEntry> m_schedule;
    std::string                   m_log_filename_template;

    std::vector<Ptr<LLMRequestApplication>>     m_request_apps;
    std::map<int64_t, Ptr<GatherApplication>>   m_gather_apps;
    std::map<int64_t, Ptr<ComputeApplication>>  m_compute_apps;
};

} // namespace ns3

#endif // LLM_WORKLOAD_SCHEDULER_H
