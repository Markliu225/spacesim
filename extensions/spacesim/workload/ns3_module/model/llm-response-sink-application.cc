#include "ns3/llm-response-sink-application.h"

#include "ns3/log.h"
#include "ns3/inet-socket-address.h"
#include "ns3/udp-socket-factory.h"
#include "ns3/uinteger.h"
#include "ns3/string.h"
#include "ns3/simulator.h"

#include "ns3/llm-packet-tag.h"

namespace ns3 {

NS_LOG_COMPONENT_DEFINE("LLMResponseSinkApplication");
NS_OBJECT_ENSURE_REGISTERED(LLMResponseSinkApplication);

TypeId
LLMResponseSinkApplication::GetTypeId(void)
{
    static TypeId tid = TypeId("ns3::LLMResponseSinkApplication")
        .SetParent<Application>()
        .SetGroupName("LlmWorkload")
        .AddConstructor<LLMResponseSinkApplication>()
        .AddAttribute("Port",
                      "UDP port on the GS to receive response burst.",
                      UintegerValue(19999),
                      MakeUintegerAccessor(&LLMResponseSinkApplication::m_port),
                      MakeUintegerChecker<uint16_t>())
        .AddAttribute("LogFilename",
                      "Path to response_log.csv.",
                      StringValue(""),
                      MakeStringAccessor(&LLMResponseSinkApplication::m_log_filename),
                      MakeStringChecker());
    return tid;
}

LLMResponseSinkApplication::LLMResponseSinkApplication()
    : m_port(19999), m_socket(nullptr), m_rx_pkt_count(0) {}

LLMResponseSinkApplication::~LLMResponseSinkApplication() {}

void
LLMResponseSinkApplication::DoDispose()
{
    if (m_socket) m_socket = nullptr;
    if (m_log.is_open()) m_log.close();
    Application::DoDispose();
}

void
LLMResponseSinkApplication::StartApplication()
{
    NS_LOG_FUNCTION(this);
    if (m_log_filename.empty()) {
        NS_FATAL_ERROR("LLMResponseSinkApplication on node "
                       << GetNode()->GetId() << ": LogFilename empty.");
    }
    m_log.open(m_log_filename.c_str(), std::ios::out | std::ios::trunc);
    if (!m_log.is_open()) {
        NS_FATAL_ERROR("Cannot open response_log: " << m_log_filename);
    }
    m_log << "req_id,gs_node_id,response_pkt_id,total_response_pkts,"
             "t_response_emit_ns,t_response_recv_ns,network_return_delay_ns,"
             "src_compute_sat_id,L_in,L_out\n";

    m_socket = Socket::CreateSocket(GetNode(),
                                    UdpSocketFactory::GetTypeId());
    InetSocketAddress local = InetSocketAddress(Ipv4Address::GetAny(), m_port);
    if (m_socket->Bind(local) != 0) {
        NS_FATAL_ERROR("LLMResponseSinkApplication bind failed on port " << m_port);
    }
    m_socket->SetRecvCallback(MakeCallback(&LLMResponseSinkApplication::HandleRead, this));
}

void
LLMResponseSinkApplication::StopApplication()
{
    NS_LOG_FUNCTION(this);
    if (m_socket) {
        m_socket->Close();
        m_socket->SetRecvCallback(MakeNullCallback<void, Ptr<Socket>>());
        m_socket = nullptr;
    }
    if (m_log.is_open()) { m_log.flush(); m_log.close(); }
}

void
LLMResponseSinkApplication::HandleRead(Ptr<Socket> socket)
{
    Ptr<Packet> packet;
    Address from;
    uint32_t my_node_id = GetNode()->GetId();
    uint64_t now_ns = (uint64_t) Simulator::Now().GetNanoSeconds();
    while ((packet = socket->RecvFrom(from))) {
        LLMPacketTag tag;
        if (!packet->PeekPacketTag(tag)) continue;
        if (tag.GetDirection() != LLMPacketTag::RESPONSE) continue;
        ++m_rx_pkt_count;
        uint64_t t_emit_ns = tag.GetTEmitNs();
        m_log << tag.GetReqId() << ','
              << my_node_id << ','
              << tag.GetPacketId() << ','
              << tag.GetTotalPkts() << ','
              << t_emit_ns << ','
              << now_ns << ','
              << (now_ns - t_emit_ns) << ','
              << tag.GetSrcNodeId() << ','
              << tag.GetLIn() << ','
              << tag.GetLOutExpected() << '\n';
    }
}

} // namespace ns3