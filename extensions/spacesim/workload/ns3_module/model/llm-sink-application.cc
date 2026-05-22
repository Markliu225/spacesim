#include "ns3/llm-sink-application.h"

#include <iostream>

#include "ns3/log.h"
#include "ns3/inet-socket-address.h"
#include "ns3/udp-socket-factory.h"
#include "ns3/uinteger.h"
#include "ns3/string.h"
#include "ns3/simulator.h"

#include "ns3/llm-packet-tag.h"

namespace ns3 {

NS_LOG_COMPONENT_DEFINE("LLMSinkApplication");

NS_OBJECT_ENSURE_REGISTERED(LLMSinkApplication);

TypeId
LLMSinkApplication::GetTypeId(void)
{
    static TypeId tid = TypeId("ns3::LLMSinkApplication")
        .SetParent<Application>()
        .SetGroupName("LlmWorkload")
        .AddConstructor<LLMSinkApplication>()
        .AddAttribute("Port",
                      "UDP port to bind to.",
                      UintegerValue(9999),
                      MakeUintegerAccessor(&LLMSinkApplication::m_port),
                      MakeUintegerChecker<uint16_t>())
        .AddAttribute("LogFilename",
                      "Path to per-sink CSV log file. Header is written at "
                      "StartApplication; one row per received packet on the "
                      "hot path.",
                      StringValue(""),
                      MakeStringAccessor(&LLMSinkApplication::m_log_filename),
                      MakeStringChecker());
    return tid;
}

LLMSinkApplication::LLMSinkApplication()
    : m_port(9999), m_log_filename(""), m_socket(nullptr), m_rx_pkt_count(0)
{
}

LLMSinkApplication::~LLMSinkApplication()
{
}

void
LLMSinkApplication::DoDispose()
{
    if (m_socket) {
        m_socket = nullptr;
    }
    if (m_log.is_open()) {
        m_log.close();
    }
    Application::DoDispose();
}

void
LLMSinkApplication::StartApplication()
{
    NS_LOG_FUNCTION(this);

    if (m_log_filename.empty()) {
        NS_FATAL_ERROR("LLMSinkApplication on node " << GetNode()->GetId()
                       << " has empty LogFilename attribute.");
    }

    m_log.open(m_log_filename.c_str(), std::ios::out | std::ios::trunc);
    if (!m_log.is_open()) {
        NS_FATAL_ERROR("Cannot open LLM sink log file: " << m_log_filename);
    }
    // CSV header.
    m_log << "recv_time_ns,req_id,packet_id,total_pkts,t_emit_ns,"
             "src_node_id,L_in,L_out_expected,recv_node_id\n";

    m_socket = Socket::CreateSocket(GetNode(),
                                    UdpSocketFactory::GetTypeId());
    InetSocketAddress local = InetSocketAddress(Ipv4Address::GetAny(), m_port);
    if (m_socket->Bind(local) != 0) {
        NS_FATAL_ERROR("LLMSinkApplication failed to bind UDP port "
                       << m_port << " on node " << GetNode()->GetId());
    }
    m_socket->SetRecvCallback(MakeCallback(&LLMSinkApplication::HandleRead, this));
}

void
LLMSinkApplication::StopApplication()
{
    NS_LOG_FUNCTION(this);
    if (m_socket) {
        m_socket->Close();
        m_socket->SetRecvCallback(MakeNullCallback<void, Ptr<Socket>>());
        m_socket = nullptr;
    }
    if (m_log.is_open()) {
        m_log.flush();
        m_log.close();
    }
}

void
LLMSinkApplication::HandleRead(Ptr<Socket> socket)
{
    Ptr<Packet> packet;
    Address from;
    uint32_t my_node_id = GetNode()->GetId();
    int64_t now_ns = Simulator::Now().GetNanoSeconds();
    while ((packet = socket->RecvFrom(from))) {
        LLMPacketTag tag;
        if (!packet->PeekPacketTag(tag)) {
            // Stray packet without our tag — count but do not log.
            continue;
        }
        ++m_rx_pkt_count;
        // Hot path: only integer streaming, no fmt/printf.
        m_log << now_ns << ','
              << tag.GetReqId() << ','
              << tag.GetPacketId() << ','
              << tag.GetTotalPkts() << ','
              << tag.GetTEmitNs() << ','
              << tag.GetSrcNodeId() << ','
              << tag.GetLIn() << ','
              << tag.GetLOutExpected() << ','
              << my_node_id << '\n';
    }
}

} // namespace ns3