#include "ns3/llm-workload-scheduler.h"

#include <iostream>

#include "ns3/double.h"
#include "ns3/uinteger.h"
#include "ns3/string.h"
#include "ns3/inet-socket-address.h"
#include "ns3/ipv4.h"
#include "ns3/ipv4-address.h"
#include "ns3/ipv4-interface-address.h"
#include "ns3/simulator.h"

#include "ns3/exp-util.h"  // parse_boolean

#include "ns3/llm-request-helper.h"
#include "ns3/llm-sink-helper.h"

namespace ns3 {

static Ipv4Address
get_first_non_loopback_ipv4(Ptr<Node> node)
{
    Ptr<Ipv4> ipv4 = node->GetObject<Ipv4>();
    NS_ASSERT_MSG(ipv4 != nullptr,
                  "Node " << node->GetId() << " has no Ipv4 stack -- "
                  "LlmWorkloadScheduler cannot resolve its address.");
    for (uint32_t iface = 0; iface < ipv4->GetNInterfaces(); ++iface) {
        for (uint32_t a = 0; a < ipv4->GetNAddresses(iface); ++a) {
            Ipv4InterfaceAddress addr = ipv4->GetAddress(iface, a);
            Ipv4Address local = addr.GetLocal();
            if (local != Ipv4Address::GetLoopback() && local != Ipv4Address("0.0.0.0")) {
                return local;
            }
        }
    }
    NS_FATAL_ERROR("Node " << node->GetId() << " has no non-loopback IPv4 address");
    return Ipv4Address();  // unreachable
}

LlmWorkloadScheduler::LlmWorkloadScheduler(Ptr<BasicSimulation> basicSimulation,
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
    std::cout << "  > LLM workload scheduler is enabled" << std::endl;

    // Read schedule
    std::string sched_filename =
        m_basicSimulation->GetRunDir() + "/" +
        m_basicSimulation->GetConfigParamOrFail("llm_workload_schedule_filename");
    m_schedule = read_llm_workload_schedule(sched_filename,
                                            m_simulation_end_time_ns);
    std::cout << "  > Read schedule (" << m_schedule.size() << " entries)"
              << std::endl;

    // Log filename. Each sink gets its own file:
    //   <basename>_sink_node<NODEID>.csv
    m_log_filename_template = m_basicSimulation->GetLogsDir() + "/" +
        m_basicSimulation->GetConfigParamOrDefault(
            "llm_workload_log_filename", "llm_workload_sink.csv");

    // Install applications.
    uint16_t udp_port = 9999;
    for (const auto &entry : m_schedule) {
        if ((uint32_t) entry.src_gs_node_id >= m_all_nodes.GetN()) {
            NS_FATAL_ERROR("LLM schedule references unknown src node "
                           << entry.src_gs_node_id);
        }
        if ((uint32_t) entry.dst_compute_sat_node_id >= m_all_nodes.GetN()) {
            NS_FATAL_ERROR("LLM schedule references unknown dst node "
                           << entry.dst_compute_sat_node_id);
        }
        Ptr<Node> src_node = m_all_nodes.Get(entry.src_gs_node_id);
        Ptr<Node> dst_node = m_all_nodes.Get(entry.dst_compute_sat_node_id);

        // --- Install sink (one per dst node) ---
        if (m_sink_apps.count(entry.dst_compute_sat_node_id) == 0) {
            // Insert NODEID into the configured template filename.
            std::string log_file = m_log_filename_template;
            std::string token = ".csv";
            std::string injection = "_sink_node" + std::to_string(entry.dst_compute_sat_node_id);
            auto pos = log_file.rfind(token);
            if (pos == std::string::npos) {
                log_file += injection;
            } else {
                log_file = log_file.substr(0, pos) + injection + token;
            }
            LLMSinkHelper sink_helper(udp_port, log_file);
            ApplicationContainer sink_apps = sink_helper.Install(dst_node);
            Ptr<LLMSinkApplication> sink_app =
                DynamicCast<LLMSinkApplication>(sink_apps.Get(0));
            sink_app->SetStartTime(NanoSeconds(entry.start_time_ns));
            sink_app->SetStopTime(NanoSeconds(entry.stop_time_ns));
            m_sink_apps[entry.dst_compute_sat_node_id] = sink_app;
            std::cout << "  > Installed sink on node "
                      << entry.dst_compute_sat_node_id
                      << " (log: " << log_file << ")"
                      << std::endl;
        }

        // --- Install request app ---
        Ipv4Address dst_ip = get_first_non_loopback_ipv4(dst_node);
        LLMRequestHelper req_helper(dst_ip, udp_port);
        req_helper.SetAttribute("Lambda",        DoubleValue(entry.lambda_req_per_sec));
        req_helper.SetAttribute("LInMean",       DoubleValue(entry.L_in_mean));
        req_helper.SetAttribute("LInStd",        DoubleValue(entry.L_in_std));
        req_helper.SetAttribute("LInMin",        UintegerValue(entry.L_in_min));
        req_helper.SetAttribute("LInMax",        UintegerValue(entry.L_in_max));
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
                  << entry.L_in_std << ")"
                  << std::endl;
    }

    m_basicSimulation->RegisterTimestamp("Setup LLM workload scheduler");
}

void
LlmWorkloadScheduler::WriteResults()
{
    if (!m_enabled) return;

    std::cout << "STORE LLM WORKLOAD RESULTS" << std::endl;
    uint64_t total_tx_req = 0;
    uint64_t total_tx_pkt = 0;
    for (auto &app : m_request_apps) {
        total_tx_req += app->GetTxRequestCount();
        total_tx_pkt += app->GetTxPacketCount();
    }
    uint64_t total_rx_pkt = 0;
    for (auto &kv : m_sink_apps) {
        total_rx_pkt += kv.second->GetRxPacketCount();
    }
    std::cout << "  > tx_request_count = " << total_tx_req << std::endl;
    std::cout << "  > tx_packet_count  = " << total_tx_pkt << std::endl;
    std::cout << "  > rx_packet_count  = " << total_rx_pkt
              << (total_tx_pkt > 0
                  ? "  (" + std::to_string(100.0 * total_rx_pkt / total_tx_pkt) + "%)"
                  : "")
              << std::endl;

    // Also write a single-row summary CSV alongside the per-sink logs.
    std::string summary_path = m_basicSimulation->GetLogsDir() + "/llm_workload_summary.csv";
    std::ofstream s(summary_path.c_str());
    s << "tx_request_count,tx_packet_count,rx_packet_count\n";
    s << total_tx_req << "," << total_tx_pkt << "," << total_rx_pkt << "\n";
    s.close();
    std::cout << "  > Wrote " << summary_path << std::endl;
}

} // namespace ns3