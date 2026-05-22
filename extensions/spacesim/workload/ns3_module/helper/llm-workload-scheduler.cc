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
#include "ns3/llm-sink-helper.h"
#include "ns3/gather-helper.h"
#include "ns3/compute-helper.h"
#include "ns3/llm-response-sink-helper.h"

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

// Build "<logs_dir>/<basename>_<role>_node<NODEID>.csv"
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

    m_response_loop = parse_boolean(m_basicSimulation->GetConfigParamOrDefault(
        "enable_llm_response_loop", "false"));

    std::cout << "  > Mode: "
              << (m_response_loop ? "Phase C (gather + compute + response)"
                                  : "Phase B (sink-only)")
              << std::endl;

    std::string sched_filename =
        m_basicSimulation->GetRunDir() + "/" +
        m_basicSimulation->GetConfigParamOrFail("llm_workload_schedule_filename");
    m_schedule = read_llm_workload_schedule(sched_filename,
                                            m_simulation_end_time_ns);
    std::cout << "  > Read schedule (" << m_schedule.size() << " entries)"
              << std::endl;

    m_log_filename_template = m_basicSimulation->GetLogsDir() + "/" +
        m_basicSimulation->GetConfigParamOrDefault(
            "llm_workload_log_filename", "llm_workload_sink.csv");

    if (m_response_loop) {
        InstallPhaseC();
    } else {
        InstallPhaseB();
    }

    m_basicSimulation->RegisterTimestamp("Setup LLM workload scheduler");
}

void
LlmWorkloadScheduler::InstallPhaseB()
{
    const uint16_t udp_port = 9999;
    for (const auto &entry : m_schedule) {
        if ((uint32_t) entry.src_gs_node_id >= m_all_nodes.GetN())
            NS_FATAL_ERROR("LLM schedule: bad src node " << entry.src_gs_node_id);
        if ((uint32_t) entry.dst_compute_sat_node_id >= m_all_nodes.GetN())
            NS_FATAL_ERROR("LLM schedule: bad dst node " << entry.dst_compute_sat_node_id);
        Ptr<Node> src_node = m_all_nodes.Get(entry.src_gs_node_id);
        Ptr<Node> dst_node = m_all_nodes.Get(entry.dst_compute_sat_node_id);

        if (m_sink_apps.count(entry.dst_compute_sat_node_id) == 0) {
            std::string log_file = build_log_path(m_log_filename_template,
                                                  "sink", entry.dst_compute_sat_node_id);
            LLMSinkHelper sink_helper(udp_port, log_file);
            ApplicationContainer sink_apps = sink_helper.Install(dst_node);
            Ptr<LLMSinkApplication> sink_app =
                DynamicCast<LLMSinkApplication>(sink_apps.Get(0));
            sink_app->SetStartTime(NanoSeconds(entry.start_time_ns));
            sink_app->SetStopTime(NanoSeconds(entry.stop_time_ns));
            m_sink_apps[entry.dst_compute_sat_node_id] = sink_app;
            std::cout << "  > Installed sink on node "
                      << entry.dst_compute_sat_node_id
                      << " (log: " << log_file << ")" << std::endl;
        }

        Ipv4Address dst_ip = get_first_non_loopback_ipv4(dst_node);
        LLMRequestHelper req_helper(dst_ip, udp_port);
        req_helper.SetAttribute("Lambda",        DoubleValue(entry.lambda_req_per_sec));
        req_helper.SetAttribute("LInMean",       DoubleValue(entry.L_in_mean));
        req_helper.SetAttribute("LInStd",        DoubleValue(entry.L_in_std));
        req_helper.SetAttribute("LInMin",        UintegerValue(entry.L_in_min));
        req_helper.SetAttribute("LInMax",        UintegerValue(entry.L_in_max));
        req_helper.SetAttribute("LOutMean",      DoubleValue(entry.L_out_mean));
        req_helper.SetAttribute("LOutStd",       DoubleValue(entry.L_out_std));
        req_helper.SetAttribute("LOutMin",       UintegerValue(entry.L_out_min));
        req_helper.SetAttribute("LOutMax",       UintegerValue(entry.L_out_max));
        req_helper.SetAttribute("BytesPerToken", UintegerValue(entry.bytes_per_token));
        req_helper.SetAttribute("PacketPayload", UintegerValue(entry.packet_payload));
        ApplicationContainer req_apps = req_helper.Install(src_node);
        Ptr<LLMRequestApplication> req_app =
            DynamicCast<LLMRequestApplication>(req_apps.Get(0));
        req_app->SetStartTime(NanoSeconds(entry.start_time_ns));
        req_app->SetStopTime(NanoSeconds(entry.stop_time_ns));
        m_request_apps.push_back(req_app);
        std::cout << "  > Installed request app on node "
                  << entry.src_gs_node_id << " -> "
                  << dst_ip << ":" << udp_port
                  << "  lambda=" << entry.lambda_req_per_sec
                  << " req/s  L_in=N(" << entry.L_in_mean << ","
                  << entry.L_in_std << ")" << std::endl;
    }
}

void
LlmWorkloadScheduler::InstallPhaseC()
{
    const uint16_t request_port  = 9999;
    const uint16_t response_port = 19999;

    // Read compute model defaults from config; entries can override later.
    uint64_t alpha = parse_positive_int64(m_basicSimulation->GetConfigParamOrDefault(
        "compute_alpha_ns_per_input_token", "100000"));    // 100 us / tok
    uint64_t beta  = parse_positive_int64(m_basicSimulation->GetConfigParamOrDefault(
        "compute_beta_ns_per_output_token", "50000"));     // 50 us / tok
    uint64_t gamma = parse_positive_int64(m_basicSimulation->GetConfigParamOrDefault(
        "compute_gamma_ns", "10000000"));                  // 10 ms
    uint64_t gather_timeout_ns = parse_positive_int64(
        m_basicSimulation->GetConfigParamOrDefault(
            "gather_timeout_ns", "30000000000"));          // 30 s

    for (const auto &entry : m_schedule) {
        if ((uint32_t) entry.src_gs_node_id >= m_all_nodes.GetN())
            NS_FATAL_ERROR("LLM schedule: bad src node " << entry.src_gs_node_id);
        if ((uint32_t) entry.dst_compute_sat_node_id >= m_all_nodes.GetN())
            NS_FATAL_ERROR("LLM schedule: bad dst node " << entry.dst_compute_sat_node_id);
        Ptr<Node> src_node = m_all_nodes.Get(entry.src_gs_node_id);
        Ptr<Node> dst_node = m_all_nodes.Get(entry.dst_compute_sat_node_id);

        // --- Per-compute-SAT: Gather + Compute (once per node) ---
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
            comp_helper.SetAttribute("ResponseDestPort",     UintegerValue(response_port));
            comp_helper.SetAttribute("PacketPayload",        UintegerValue(entry.packet_payload));
            comp_helper.SetAttribute("BytesPerToken",        UintegerValue(entry.bytes_per_token));
            comp_helper.SetAttribute("LogFilename",          StringValue(compute_log));
            ApplicationContainer compute_apps = comp_helper.Install(dst_node);
            Ptr<ComputeApplication> compute_app =
                DynamicCast<ComputeApplication>(compute_apps.Get(0));
            compute_app->SetStartTime(NanoSeconds(entry.start_time_ns));
            // ComputeApplication must outlive the request workload --
            // late-queued requests can finish compute well past
            // schedule.stop, and SendResponse must not run with a closed
            // socket. Run until sim end.
            compute_app->SetStopTime(NanoSeconds(m_simulation_end_time_ns));
            m_compute_apps[entry.dst_compute_sat_node_id] = compute_app;
            // GsIpLookup wired up below, after all compute apps exist.

            GatherHelper gh;
            gh.SetAttribute("Port",             UintegerValue(request_port));
            gh.SetAttribute("TimeoutNs",        UintegerValue(gather_timeout_ns));
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

        // --- Per-src-GS: Request app + Response sink (once per node) ---
        Ipv4Address dst_ip = get_first_non_loopback_ipv4(dst_node);
        LLMRequestHelper req_helper(dst_ip, request_port);
        req_helper.SetAttribute("Lambda",        DoubleValue(entry.lambda_req_per_sec));
        req_helper.SetAttribute("LInMean",       DoubleValue(entry.L_in_mean));
        req_helper.SetAttribute("LInStd",        DoubleValue(entry.L_in_std));
        req_helper.SetAttribute("LInMin",        UintegerValue(entry.L_in_min));
        req_helper.SetAttribute("LInMax",        UintegerValue(entry.L_in_max));
        req_helper.SetAttribute("LOutMean",      DoubleValue(entry.L_out_mean));
        req_helper.SetAttribute("LOutStd",       DoubleValue(entry.L_out_std));
        req_helper.SetAttribute("LOutMin",       UintegerValue(entry.L_out_min));
        req_helper.SetAttribute("LOutMax",       UintegerValue(entry.L_out_max));
        req_helper.SetAttribute("BytesPerToken", UintegerValue(entry.bytes_per_token));
        req_helper.SetAttribute("PacketPayload", UintegerValue(entry.packet_payload));
        ApplicationContainer req_apps = req_helper.Install(src_node);
        Ptr<LLMRequestApplication> req_app =
            DynamicCast<LLMRequestApplication>(req_apps.Get(0));
        req_app->SetStartTime(NanoSeconds(entry.start_time_ns));
        req_app->SetStopTime(NanoSeconds(entry.stop_time_ns));
        m_request_apps.push_back(req_app);

        if (m_response_sink_apps.count(entry.src_gs_node_id) == 0) {
            std::string resp_log = build_log_path(
                m_log_filename_template, "response", entry.src_gs_node_id);
            LLMResponseSinkHelper rs;
            rs.SetAttribute("Port",        UintegerValue(response_port));
            rs.SetAttribute("LogFilename", StringValue(resp_log));
            ApplicationContainer rs_apps = rs.Install(src_node);
            Ptr<LLMResponseSinkApplication> rs_app =
                DynamicCast<LLMResponseSinkApplication>(rs_apps.Get(0));
            rs_app->SetStartTime(NanoSeconds(entry.start_time_ns));
            // Keep the response sink alive all the way to sim end so it
            // captures any late response packet from the compute SAT.
            rs_app->SetStopTime(NanoSeconds(m_simulation_end_time_ns));
            m_response_sink_apps[entry.src_gs_node_id] = rs_app;
        }

        std::cout << "  > Installed Request+ResponseSink on node "
                  << entry.src_gs_node_id << " -> "
                  << dst_ip << ":" << request_port << std::endl;
    }

    // Now wire up the GS-IP-lookup for every ComputeApplication. We use a
    // shared dispatcher: a static map<node_id, scheduler*> that ns-3
    // Callback can reach. Simpler approach: hand each compute app the
    // NodeContainer directly via a helper static function bound through
    // MakeBoundCallback.
    // ns-3's Callback supports binding extra args at front via MakeBoundCallback.
    static NodeContainer s_nodes_for_lookup;
    s_nodes_for_lookup = m_all_nodes;
    auto lookup_fn = +[](uint32_t node_id) -> Ipv4Address {
        if (node_id >= s_nodes_for_lookup.GetN())
            return Ipv4Address("0.0.0.0");
        return get_first_non_loopback_ipv4(s_nodes_for_lookup.Get(node_id));
    };
    for (auto &kv : m_compute_apps) {
        kv.second->SetGsIpLookup(MakeCallback<Ipv4Address, uint32_t>(lookup_fn));
    }
}

void
LlmWorkloadScheduler::WriteResults()
{
    if (!m_enabled) return;

    std::cout << "STORE LLM WORKLOAD RESULTS" << std::endl;

    if (!m_response_loop) {
        // Phase B summary.
        uint64_t total_tx_req = 0, total_tx_pkt = 0;
        for (auto &app : m_request_apps) {
            total_tx_req += app->GetTxRequestCount();
            total_tx_pkt += app->GetTxPacketCount();
        }
        uint64_t total_rx_pkt = 0;
        for (auto &kv : m_sink_apps) total_rx_pkt += kv.second->GetRxPacketCount();
        std::cout << "  > tx_request_count = " << total_tx_req << std::endl
                  << "  > tx_packet_count  = " << total_tx_pkt << std::endl
                  << "  > rx_packet_count  = " << total_rx_pkt
                  << (total_tx_pkt > 0
                      ? "  (" + std::to_string(100.0 * total_rx_pkt / total_tx_pkt) + "%)"
                      : "")
                  << std::endl;
        std::string summary_path = m_basicSimulation->GetLogsDir()
                                  + "/llm_workload_summary.csv";
        std::ofstream s(summary_path.c_str());
        s << "tx_request_count,tx_packet_count,rx_packet_count\n";
        s << total_tx_req << "," << total_tx_pkt << "," << total_rx_pkt << "\n";
        s.close();
        std::cout << "  > Wrote " << summary_path << std::endl;
        return;
    }

    // Phase C summary.
    uint64_t total_tx_req = 0, total_tx_pkt = 0;
    for (auto &app : m_request_apps) {
        total_tx_req += app->GetTxRequestCount();
        total_tx_pkt += app->GetTxPacketCount();
    }
    uint64_t total_gather_complete = 0, total_timeout = 0, total_rx_gather_pkt = 0;
    for (auto &kv : m_gather_apps) {
        total_gather_complete += kv.second->GetGatherCompleteCount();
        total_timeout         += kv.second->GetTimeoutCount();
        total_rx_gather_pkt   += kv.second->GetRxPacketCount();
    }
    uint64_t total_compute_complete = 0;
    for (auto &kv : m_compute_apps) {
        total_compute_complete += kv.second->GetCompleteCount();
    }
    uint64_t total_response_rx = 0;
    for (auto &kv : m_response_sink_apps) {
        total_response_rx += kv.second->GetRxPacketCount();
    }
    std::cout << "  > tx_request_count       = " << total_tx_req << std::endl
              << "  > tx_request_packets     = " << total_tx_pkt << std::endl
              << "  > rx_request_packets     = " << total_rx_gather_pkt << std::endl
              << "  > gather_complete_count  = " << total_gather_complete << std::endl
              << "  > gather_timeout_count   = " << total_timeout << std::endl
              << "  > compute_complete_count = " << total_compute_complete << std::endl
              << "  > response_recv_packets  = " << total_response_rx << std::endl;
    std::string summary_path = m_basicSimulation->GetLogsDir()
                              + "/llm_workload_summary.csv";
    std::ofstream s(summary_path.c_str());
    s << "tx_request_count,tx_request_packets,rx_request_packets,"
         "gather_complete_count,gather_timeout_count,"
         "compute_complete_count,response_recv_packets\n";
    s << total_tx_req << "," << total_tx_pkt << "," << total_rx_gather_pkt << ","
      << total_gather_complete << "," << total_timeout << ","
      << total_compute_complete << "," << total_response_rx << "\n";
    s.close();
    std::cout << "  > Wrote " << summary_path << std::endl;
}

} // namespace ns3