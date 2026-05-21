#include "ns3/compute-application.h"

#include "ns3/log.h"
#include "ns3/inet-socket-address.h"
#include "ns3/udp-socket-factory.h"
#include "ns3/uinteger.h"
#include "ns3/string.h"
#include "ns3/simulator.h"

#include "ns3/llm-packet-tag.h"

namespace ns3 {

NS_LOG_COMPONENT_DEFINE("ComputeApplication");
NS_OBJECT_ENSURE_REGISTERED(ComputeApplication);

TypeId
ComputeApplication::GetTypeId(void)
{
    static TypeId tid = TypeId("ns3::ComputeApplication")
        .SetParent<Application>()
        .SetGroupName("LlmWorkload")
        .AddConstructor<ComputeApplication>()
        .AddAttribute("AlphaNsPerInputToken",
                      "T_prefill coefficient per input token in nanoseconds.",
                      UintegerValue(100000),
                      MakeUintegerAccessor(&ComputeApplication::m_alpha_ns_per_input_tok),
                      MakeUintegerChecker<uint64_t>())
        .AddAttribute("BetaNsPerOutputToken",
                      "T_decode coefficient per output token in nanoseconds.",
                      UintegerValue(50000),
                      MakeUintegerAccessor(&ComputeApplication::m_beta_ns_per_output_tok),
                      MakeUintegerChecker<uint64_t>())
        .AddAttribute("GammaNs",
                      "Fixed compute overhead in nanoseconds.",
                      UintegerValue(10000000),
                      MakeUintegerAccessor(&ComputeApplication::m_gamma_ns),
                      MakeUintegerChecker<uint64_t>())
        .AddAttribute("ResponseDestPort",
                      "UDP destination port on the source GS for the response.",
                      UintegerValue(19999),
                      MakeUintegerAccessor(&ComputeApplication::m_response_dest_port),
                      MakeUintegerChecker<uint16_t>())
        .AddAttribute("PacketPayload",
                      "Bytes of payload per response UDP packet.",
                      UintegerValue(1400),
                      MakeUintegerAccessor(&ComputeApplication::m_packet_payload),
                      MakeUintegerChecker<uint32_t>(1))
        .AddAttribute("BytesPerToken",
                      "Encoded bytes per output token.",
                      UintegerValue(4),
                      MakeUintegerAccessor(&ComputeApplication::m_bytes_per_token),
                      MakeUintegerChecker<uint32_t>(1))
        .AddAttribute("LogFilename",
                      "Path to compute_log.csv (per-request prefill/decode timings).",
                      StringValue(""),
                      MakeStringAccessor(&ComputeApplication::m_log_filename),
                      MakeStringChecker());
    return tid;
}

ComputeApplication::ComputeApplication()
    : m_alpha_ns_per_input_tok(100000),
      m_beta_ns_per_output_tok(50000),
      m_gamma_ns(10000000),
      m_response_dest_port(19999),
      m_packet_payload(1400),
      m_bytes_per_token(4),
      m_socket(nullptr),
      m_busy(false),
      m_in_service_start_ns(0),
      m_complete_count(0),
      m_response_tx_pkt_count(0)
{
}

ComputeApplication::~ComputeApplication()
{
}

void
ComputeApplication::DoDispose()
{
    if (m_socket) { m_socket = nullptr; }
    if (m_log.is_open()) { m_log.close(); }
    Application::DoDispose();
}

void
ComputeApplication::StartApplication()
{
    NS_LOG_FUNCTION(this);
    if (m_log_filename.empty()) {
        NS_FATAL_ERROR("ComputeApplication on node " << GetNode()->GetId()
                       << ": LogFilename attribute is empty.");
    }
    m_log.open(m_log_filename.c_str(), std::ios::out | std::ios::trunc);
    if (!m_log.is_open()) {
        NS_FATAL_ERROR("Cannot open compute log: " << m_log_filename);
    }
    m_log << "req_id,compute_sat_id,t_queue_enter_ns,t_compute_start_ns,"
             "t_compute_end_ns,T_compute_ns,T_queue_wait_ns,L_in,L_out\n";

    m_socket = Socket::CreateSocket(GetNode(),
                                    UdpSocketFactory::GetTypeId());
    if (m_socket->Bind() != 0) {
        NS_FATAL_ERROR("ComputeApplication: socket Bind failed");
    }
}

void
ComputeApplication::StopApplication()
{
    NS_LOG_FUNCTION(this);
    if (m_socket) { m_socket->Close(); m_socket = nullptr; }
    if (m_log.is_open()) { m_log.flush(); m_log.close(); }
}

void
ComputeApplication::OnGatherComplete(uint64_t req_id,
                                     uint32_t L_in,
                                     uint32_t L_out_expected,
                                     uint32_t src_node_id,
                                     uint64_t t_emit_request_ns)
{
    ComputeRequest r;
    r.req_id            = req_id;
    r.L_in              = L_in;
    r.L_out             = L_out_expected;
    r.src_node_id       = src_node_id;
    r.t_emit_request_ns = t_emit_request_ns;
    r.t_queue_enter_ns  = (uint64_t) Simulator::Now().GetNanoSeconds();
    m_queue.push(r);
    if (!m_busy) {
        StartNextCompute();
    }
}

void
ComputeApplication::StartNextCompute()
{
    if (m_queue.empty()) {
        m_busy = false;
        return;
    }
    m_busy = true;
    m_in_service = m_queue.front();
    m_queue.pop();
    m_in_service_start_ns = (uint64_t) Simulator::Now().GetNanoSeconds();
    uint64_t t_compute_ns =
          m_alpha_ns_per_input_tok * (uint64_t) m_in_service.L_in
        + m_beta_ns_per_output_tok  * (uint64_t) m_in_service.L_out
        + m_gamma_ns;
    Simulator::Schedule(NanoSeconds(t_compute_ns),
                        &ComputeApplication::OnComputeComplete, this);
}

void
ComputeApplication::OnComputeComplete()
{
    uint64_t now_ns = (uint64_t) Simulator::Now().GetNanoSeconds();
    uint64_t T_compute_ns = now_ns - m_in_service_start_ns;
    uint64_t T_queue_wait_ns = m_in_service_start_ns - m_in_service.t_queue_enter_ns;

    m_log << m_in_service.req_id    << ','
          << GetNode()->GetId()     << ','
          << m_in_service.t_queue_enter_ns << ','
          << m_in_service_start_ns  << ','
          << now_ns                 << ','
          << T_compute_ns           << ','
          << T_queue_wait_ns        << ','
          << m_in_service.L_in      << ','
          << m_in_service.L_out     << '\n';
    ++m_complete_count;

    SendResponse(m_in_service.req_id, m_in_service.L_in, m_in_service.L_out,
                 m_in_service.src_node_id, m_in_service.t_emit_request_ns);
    StartNextCompute();
}

void
ComputeApplication::SendResponse(uint64_t req_id, uint32_t L_in, uint32_t L_out,
                                 uint32_t src_node_id, uint64_t /*t_emit_request_ns*/)
{
    // If a previously-scheduled OnComputeComplete fires after our
    // StopApplication has already closed the socket, m_socket is null —
    // gracefully drop the response instead of crashing the simulator.
    if (!m_socket) {
        NS_LOG_WARN("ComputeApplication::SendResponse: socket closed, "
                    "dropping response for req=" << req_id);
        return;
    }
    if (m_gs_ip_lookup.IsNull()) {
        NS_LOG_WARN("ComputeApplication: no GS IP lookup callback; dropping response");
        return;
    }
    Ipv4Address dst_ip = m_gs_ip_lookup(src_node_id);

    uint64_t total_bytes = (uint64_t) L_out * m_bytes_per_token;
    uint32_t N_pkt_out = (uint32_t)((total_bytes + m_packet_payload - 1)
                                    / m_packet_payload);
    if (N_pkt_out == 0) N_pkt_out = 1;
    if (N_pkt_out > 65535) N_pkt_out = 65535;

    uint64_t t_emit_response_ns = (uint64_t) Simulator::Now().GetNanoSeconds();
    InetSocketAddress dst = InetSocketAddress(dst_ip, m_response_dest_port);
    for (uint32_t i = 0; i < N_pkt_out; ++i) {
        Ptr<Packet> pkt = Create<Packet>(m_packet_payload);
        LLMPacketTag tag(req_id,
                         (uint16_t) i,
                         (uint16_t) N_pkt_out,
                         t_emit_response_ns,
                         GetNode()->GetId(),
                         L_in,
                         L_out,
                         LLMPacketTag::RESPONSE);
        pkt->AddPacketTag(tag);
        m_socket->SendTo(pkt, 0, dst);
        ++m_response_tx_pkt_count;
    }
}

} // namespace ns3