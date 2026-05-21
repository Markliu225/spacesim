/*
 * LlmWorkloadScheduler — Hypatia main_satnet integration class.
 *
 * Two modes, switched by the `enable_llm_response_loop` config flag:
 *
 *   - Phase B mode (flag absent / false):
 *       Installs LLMRequestApplication on each src GS + LLMSinkApplication
 *       on each unique dst compute SAT. Compute SAT logs every packet.
 *
 *   - Phase C mode (flag true):
 *       Installs LLMRequestApplication + LLMResponseSinkApplication on
 *       each src GS, and GatherApplication + ComputeApplication on each
 *       unique dst compute SAT. Gather → Compute is wired via callback;
 *       Compute → GS uses a GsIpLookup callback that walks the topology's
 *       NodeContainer.
 *
 * In either mode `enable_llm_workload` must be true.
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
#include "ns3/llm-sink-application.h"
#include "ns3/gather-application.h"
#include "ns3/compute-application.h"
#include "ns3/llm-response-sink-application.h"

namespace ns3 {

class LlmWorkloadScheduler
{
public:
    LlmWorkloadScheduler(Ptr<BasicSimulation> basicSimulation,
                         const NodeContainer &all_nodes);
    void WriteResults();

private:
    void InstallPhaseB();
    void InstallPhaseC();

    Ptr<BasicSimulation>          m_basicSimulation;
    NodeContainer                 m_all_nodes;
    bool                          m_enabled;
    bool                          m_response_loop;        // Phase C switch
    int64_t                       m_simulation_end_time_ns;
    std::vector<LlmWorkloadEntry> m_schedule;
    std::string                   m_log_filename_template;

    // Phase B artefacts.
    std::vector<Ptr<LLMRequestApplication>>     m_request_apps;
    std::map<int64_t, Ptr<LLMSinkApplication>>  m_sink_apps;

    // Phase C artefacts.
    std::map<int64_t, Ptr<GatherApplication>>   m_gather_apps;
    std::map<int64_t, Ptr<ComputeApplication>>  m_compute_apps;
    std::map<int64_t, Ptr<LLMResponseSinkApplication>> m_response_sink_apps;
};

} // namespace ns3

#endif // LLM_WORKLOAD_SCHEDULER_H