#include "ns3/llm-workload-scheduler.h"

#include <fstream>
#include <iostream>

#include "ns3/double.h"
#include "ns3/uinteger.h"
#include "ns3/string.h"
#include "ns3/inet-socket-address.h"
#include "ns3/ipv4.h"
#include "ns3/ipv4-address.h"
#include "ns3/ipv4-interface-address.h"
#include "ns3/simulator.h"

#include "ns3/exp-util.h"

#include "ns3/llm-request-helper.h"
#include "ns3/gather-helper.h"
#include "ns3/compute-helper.h"

namespace ns3 {

static Ipv4Address
get_first_non_loopback_ipv4(Ptr<Node> node)
{
    Ptr<Ipv4> ipv4 = node->GetObject<Ipv4>();
    NS_ASSERT_MSG(ipv4 != nullptr,
                  "Node " << node->GetId() << " has no Ipv4 stack.");
    for (uint32_t iface = 0; iface < ipv4->GetNInterfaces(); ++iface) {
        for (uint32_t a = 0; a < ipv4->GetNAddresses(iface); ++a) {
            Ipv4InterfaceAddress addr = ipv4->GetAddress(iface, a);
            Ipv4Address local = addr.GetLocal();
            if (local != Ipv4Address::GetLoopback() &&
                local != Ipv4Address("0.0.0.0")) {
                return local;
            }
        }
    }
    NS_FATAL_ERROR("Node " << node->GetId() << " has no non-loopback IPv4 address");
    return Ipv4Address();
}

// "<logs_dir>/<basename>_<role>_node<NODEID>.csv"
static std::string
build_log_path(const std::string &template_path, const std::string &role,
               int64_t node_id)
{
    std::string token = ".csv";
    std::string injection = "_" + role + "_node" + std::to_string(node_id);
    auto pos = template_path.rfind(token);
    if (pos == std::string::npos) {
        return template_path + injection;
    }
    return template_path.substr(0, pos) + injection + token;
}

LlmWorkloadScheduler::LlmWorkloadScheduler(
    Ptr<BasicSimulation> basicSimulation,
    const NodeContainer &all_nodes)
{
    std::cout << "LLM WORKLOAD SCHEDULER" << std::endl;

    m_basicSimulation = basicSimulation;
    m_all_nodes = all_nodes;
    m_simulation_end_time_ns = m_basicSimulation->GetSimulationEndTimeNs();

    m_enabled = parse_boolean(m_basicSimulation->GetConfigParamOrDefault(
        "enable_llm_workload", "false"));
    if (!m_enabled) {
        std::cout << "  > Not enabled explicitly, so disabled" << std::endl;
        return;
    }

    std::cout << "  > Transport: TCP, full request lifecycle "
                 "(gather → compute → response)" << std::endl;

    std::string sched_filename =
        m_basicSimulation->GetRunDir() + "/" +
        m_basicSimulation->GetConfigParamOrFail("llm_workload_schedule_filename");
    m_schedule = read_llm_workload_schedule(sched_filename,
                                            m_simulation_end_time_ns);
    std::cout << "  > Read schedule (" << m_schedule.size() << " entries)"
              << std::endl;

    m_log_filename_template = m_basicSimulation->GetLogsDir() + "/" +
        m_basicSimulation->GetConfigParamOrDefault(
            "llm_workload_log_filename", "llm.csv");

    InstallSchedule();

    m_basicSimulation->RegisterTimestamp("Setup LLM workload scheduler");
}

void
LlmWorkloadScheduler::InstallSchedule()
{
    const uint16_t request_port = 9999;

    uint64_t alpha = parse_positive_int64(m_basicSimulation->GetConfigParamOrDefault(
        "compute_alpha_ns_per_input_token", "100000"));
    uint64_t beta  = parse_positive_int64(m_basicSimulation->GetConfigParamOrDefault(
        "compute_beta_ns_per_output_token", "50000"));
    uint64_t gamma = parse_positive_int64(m_basicSimulation->GetConfigParamOrDefault(
        "compute_gamma_ns", "10000000"));

    for (const auto &entry : m_schedule) {
        if ((uint32_t) entry.src_gs_node_id >= m_all_nodes.GetN())
            NS_FATAL_ERROR("LLM schedule: bad src node " << entry.src_gs_node_id);
        if ((uint32_t) entry.dst_compute_sat_node_id >= m_all_nodes.GetN())
            NS_FATAL_ERROR("LLM schedule: bad dst node " << entry.dst_compute_sat_node_id);
        Ptr<Node> src_node = m_all_nodes.Get(entry.src_gs_node_id);
        Ptr<Node> dst_node = m_all_nodes.Get(entry.dst_compute_sat_node_id);

        // ---- Per-compute-SAT (once): Gather + Compute ----
        if (m_gather_apps.count(entry.dst_compute_sat_node_id) == 0) {
            std::string gather_log = build_log_path(
                m_log_filename_template, "gather", entry.dst_compute_sat_node_id);
            std::string stuck_log  = build_log_path(
                m_log_filename_template, "stuck", entry.dst_compute_sat_node_id);
            std::string compute_log = build_log_path(
                m_log_filename_template, "compute", entry.dst_compute_sat_node_id);

            ComputeHelper comp_helper;
            comp_helper.SetAttribute("AlphaNsPerInputToken", UintegerValue(alpha));
            comp_helper.SetAttribute("BetaNsPerOutputToken", UintegerValue(beta));
            comp_helper.SetAttribute("GammaNs",              UintegerValue(gamma));
            comp_helper.SetAttribute("BytesPerToken",        UintegerValue(entry.bytes_per_token));
            // Option B: Compute owns the L_out distribution. Take it from
            // this first entry for the SAT; later entries' L_out_* are ignored.
            comp_helper.SetAttribute("LOutMean",  DoubleValue(entry.L_out_mean));
            comp_helper.SetAttribute("LOutStd",   DoubleValue(entry.L_out_std));
            comp_helper.SetAttribute("LOutMin",   UintegerValue(entry.L_out_min));
            comp_helper.SetAttribute("LOutMax",   UintegerValue(entry.L_out_max));
            comp_helper.SetAttribute("LogFilename", StringValue(compute_log));
            ApplicationContainer compute_apps = comp_helper.Install(dst_node);
            Ptr<ComputeApplication> compute_app =
                DynamicCast<ComputeApplication>(compute_apps.Get(0));
            compute_app->SetStartTime(NanoSeconds(entry.start_time_ns));
            // Outlive the request workload — late requests finish compute
            // past the schedule's stop.
            compute_app->SetStopTime(NanoSeconds(m_simulation_end_time_ns));
            m_compute_apps[entry.dst_compute_sat_node_id] = compute_app;

            GatherHelper gh;
            gh.SetAttribute("Port",             UintegerValue(request_port));
            gh.SetAttribute("BytesPerToken",    UintegerValue(entry.bytes_per_token));
            gh.SetAttribute("LogFilename",      StringValue(gather_log));
            gh.SetAttribute("StuckLogFilename", StringValue(stuck_log));
            ApplicationContainer gather_apps = gh.Install(dst_node);
            Ptr<GatherApplication> gather_app =
                DynamicCast<GatherApplication>(gather_apps.Get(0));
            gather_app->SetGatherCompleteCallback(
                MakeCallback(&ComputeApplication::OnGatherComplete, compute_app));
            gather_app->SetStartTime(NanoSeconds(entry.start_time_ns));
            gather_app->SetStopTime(NanoSeconds(m_simulation_end_time_ns));
            m_gather_apps[entry.dst_compute_sat_node_id] = gather_app;

            std::cout << "  > Installed Gather+Compute on node "
                      << entry.dst_compute_sat_node_id
                      << " (alpha=" << alpha << "ns/tok, beta=" << beta
                      << "ns/tok, gamma=" << gamma << "ns)" << std::endl;
        }

        // ---- Per-src-GS (per schedule row): Request app ----
        Ipv4Address dst_ip = get_first_non_loopback_ipv4(dst_node);
        std::string response_log = build_log_path(
            m_log_filename_template, "response", entry.src_gs_node_id);
        LLMRequestHelper req_helper(dst_ip, request_port);
        req_helper.SetAttribute("Lambda",         DoubleValue(entry.lambda_req_per_sec));
        req_helper.SetAttribute("LInMean",        DoubleValue(entry.L_in_mean));
        req_helper.SetAttribute("LInStd",         DoubleValue(entry.L_in_std));
        req_helper.SetAttribute("LInMin",         UintegerValue(entry.L_in_min));
        req_helper.SetAttribute("LInMax",         UintegerValue(entry.L_in_max));
        req_helper.SetAttribute("BytesPerToken",  UintegerValue(entry.bytes_per_token));
        req_helper.SetAttribute("ResponseLogFilename", StringValue(response_log));
        ApplicationContainer req_apps = req_helper.Install(src_node);
        Ptr<LLMRequestApplication> req_app =
            DynamicCast<LLMRequestApplication>(req_apps.Get(0));
        req_app->SetStartTime(NanoSeconds(entry.start_time_ns));
        // Keep the request app alive until sim end so its in-flight
        // sockets can still log response logs from late-arriving responses.
        req_app->SetStopTime(NanoSeconds(m_simulation_end_time_ns));
        m_request_apps.push_back(req_app);

        std::cout << "  > Installed Request on node "
                  << entry.src_gs_node_id << " -> "
                  << dst_ip << ":" << request_port << std::endl;
    }
}

void
LlmWorkloadScheduler::WriteResults()
{
    if (!m_enabled) return;

    std::cout << "STORE LLM WORKLOAD RESULTS" << std::endl;

    uint64_t total_tx_req = 0, total_tx_pkt = 0;
    for (auto &app : m_request_apps) {
        total_tx_req += app->GetTxRequestCount();
        total_tx_pkt += app->GetTxPacketCount();
    }
    uint64_t total_gather_complete = 0;
    for (auto &kv : m_gather_apps) {
        total_gather_complete += kv.second->GetGatherCompleteCount();
    }
    uint64_t total_compute_complete = 0;
    for (auto &kv : m_compute_apps) {
        total_compute_complete += kv.second->GetCompleteCount();
    }

    std::cout << "  > tx_request_count       = " << total_tx_req << std::endl
              << "  > tx_request_packets     = " << total_tx_pkt << std::endl
              << "  > rx_request_packets     = " << total_gather_complete << std::endl
              << "  > gather_complete_count  = " << total_gather_complete << std::endl
              << "  > gather_timeout_count   = 0  (TCP)" << std::endl
              << "  > compute_complete_count = " << total_compute_complete << std::endl
              << "  > response_recv_packets  = " << (2 * total_compute_complete) << std::endl;

    std::string summary_path = m_basicSimulation->GetLogsDir()
                              + "/llm_workload_summary.csv";
    std::ofstream s(summary_path.c_str());
    s << "tx_request_count,tx_request_packets,rx_request_packets,"
         "gather_complete_count,gather_timeout_count,"
         "compute_complete_count,response_recv_packets\n";
    s << total_tx_req << "," << total_tx_pkt << "," << total_gather_complete << ","
      << total_gather_complete << ",0,"
      << total_compute_complete << "," << (2 * total_compute_complete) << "\n";
    s.close();
    std::cout << "  > Wrote " << summary_path << std::endl;
}

} // namespace ns3
