#include "ns3/llm-request-application.h"

#include <cmath>

#include "ns3/log.h"
#include "ns3/inet-socket-address.h"
#include "ns3/udp-socket-factory.h"
#include "ns3/uinteger.h"
#include "ns3/double.h"
#include "ns3/address-utils.h"
#include "ns3/simulator.h"

#include "ns3/llm-packet-tag.h"

namespace ns3 {

NS_LOG_COMPONENT_DEFINE("LLMRequestApplication");

NS_OBJECT_ENSURE_REGISTERED(LLMRequestApplication);

TypeId
LLMRequestApplication::GetTypeId(void)
{
    static TypeId tid = TypeId("ns3::LLMRequestApplication")
        .SetParent<Application>()
        .SetGroupName("LlmWorkload")
        .AddConstructor<LLMRequestApplication>()
        .AddAttribute("DestAddress",
                      "Destination compute-sat IPv4 address.",
                      AddressValue(),
                      MakeAddressAccessor(&LLMRequestApplication::m_dst_addr),
                      MakeAddressChecker())
        .AddAttribute("DestPort",
                      "Destination UDP port.",
                      UintegerValue(9999),
                      MakeUintegerAccessor(&LLMRequestApplication::m_dst_port),
                      MakeUintegerChecker<uint16_t>())
        .AddAttribute("Lambda",
                      "Request arrival rate (requests per second).",
                      DoubleValue(10.0),
                      MakeDoubleAccessor(&LLMRequestApplication::m_lambda),
                      MakeDoubleChecker<double>(1e-9))
        .AddAttribute("LInMean",
                      "Mean of prompt-token count Normal distribution.",
                      DoubleValue(500.0),
                      MakeDoubleAccessor(&LLMRequestApplication::m_L_in_mean),
                      MakeDoubleChecker<double>(0.0))
        .AddAttribute("LInStd",
                      "Std dev of prompt-token count Normal distribution.",
                      DoubleValue(100.0),
                      MakeDoubleAccessor(&LLMRequestApplication::m_L_in_std),
                      MakeDoubleChecker<double>(0.0))
        .AddAttribute("LInMin",
                      "Lower clamp on L_in.",
                      UintegerValue(1),
                      MakeUintegerAccessor(&LLMRequestApplication::m_L_in_min),
                      MakeUintegerChecker<uint32_t>(1))
        .AddAttribute("LInMax",
                      "Upper clamp on L_in.",
                      UintegerValue(2000),
                      MakeUintegerAccessor(&LLMRequestApplication::m_L_in_max),
                      MakeUintegerChecker<uint32_t>(1))
        .AddAttribute("BytesPerToken",
                      "Encoded bytes per input token.",
                      UintegerValue(4),
                      MakeUintegerAccessor(&LLMRequestApplication::m_bytes_per_token),
                      MakeUintegerChecker<uint32_t>(1))
        .AddAttribute("PacketPayload",
                      "Bytes of payload per UDP packet (modeled).",
                      UintegerValue(1400),
                      MakeUintegerAccessor(&LLMRequestApplication::m_packet_payload),
                      MakeUintegerChecker<uint32_t>(1));
    return tid;
}

LLMRequestApplication::LLMRequestApplication()
    : m_dst_port(9999),
      m_lambda(10.0),
      m_L_in_mean(500.0),
      m_L_in_std(100.0),
      m_L_in_min(1),
      m_L_in_max(2000),
      m_bytes_per_token(4),
      m_packet_payload(1400),
      m_socket(nullptr),
      m_req_counter(0),
      m_tx_pkt_count(0)
{
}

LLMRequestApplication::~LLMRequestApplication()
{
}

void
LLMRequestApplication::DoDispose()
{
    if (m_socket) {
        m_socket = nullptr;
    }
    if (m_next_event.IsRunning()) {
        Simulator::Cancel(m_next_event);
    }
    Application::DoDispose();
}

void
LLMRequestApplication::StartApplication()
{
    NS_LOG_FUNCTION(this);

    if (m_lambda <= 0.0) {
        NS_FATAL_ERROR("LLMRequestApplication: Lambda must be > 0 (got "
                       << m_lambda << ")");
    }
    if (m_L_in_max < m_L_in_min) {
        NS_FATAL_ERROR("LLMRequestApplication: LInMax < LInMin");
    }

    m_iat_rv = CreateObject<ExponentialRandomVariable>();
    m_iat_rv->SetAttribute("Mean", DoubleValue(1.0 / m_lambda));

    m_L_in_rv = CreateObject<NormalRandomVariable>();
    m_L_in_rv->SetAttribute("Mean",     DoubleValue(m_L_in_mean));
    m_L_in_rv->SetAttribute("Variance", DoubleValue(m_L_in_std * m_L_in_std));

    m_socket = Socket::CreateSocket(GetNode(),
                                    UdpSocketFactory::GetTypeId());
    // Connect-style sends are unnecessary for UDP; we use SendTo.
    if (m_socket->Bind() != 0) {
        NS_FATAL_ERROR("LLMRequestApplication: socket Bind failed");
    }

    ScheduleNext();
}

void
LLMRequestApplication::StopApplication()
{
    NS_LOG_FUNCTION(this);
    if (m_next_event.IsRunning()) {
        Simulator::Cancel(m_next_event);
    }
    if (m_socket) {
        m_socket->Close();
        m_socket = nullptr;
    }
}

void
LLMRequestApplication::ScheduleNext()
{
    double iat_s = m_iat_rv->GetValue();
    m_next_event = Simulator::Schedule(Seconds(iat_s),
                                       &LLMRequestApplication::EmitRequest,
                                       this);
}

void
LLMRequestApplication::EmitRequest()
{
    // 1. Sample L_in, clip to configured bounds.
    double L_in_sample = m_L_in_rv->GetValue();
    if (L_in_sample < (double) m_L_in_min) L_in_sample = (double) m_L_in_min;
    if (L_in_sample > (double) m_L_in_max) L_in_sample = (double) m_L_in_max;
    uint32_t L_in = (uint32_t) std::lround(L_in_sample);

    // 2. Slice into N_pkt UDP packets.
    uint64_t total_bytes = (uint64_t) L_in * m_bytes_per_token;
    uint32_t N_pkt = (uint32_t)((total_bytes + m_packet_payload - 1) / m_packet_payload);
    if (N_pkt == 0) N_pkt = 1;
    if (N_pkt > 65535) N_pkt = 65535;  // tag width

    uint64_t req_id = m_req_counter++;
    uint64_t t_emit_ns = (uint64_t) Simulator::Now().GetNanoSeconds();
    uint32_t my_node_id = GetNode()->GetId();

    InetSocketAddress dst = InetSocketAddress(
        Ipv4Address::ConvertFrom(m_dst_addr), m_dst_port);

    for (uint32_t i = 0; i < N_pkt; ++i) {
        Ptr<Packet> pkt = Create<Packet>(m_packet_payload);
        LLMPacketTag tag(req_id,
                         (uint16_t) i,
                         (uint16_t) N_pkt,
                         t_emit_ns,
                         my_node_id,
                         L_in,
                         0 /* L_out_expected, Phase C will fill */);
        pkt->AddPacketTag(tag);
        m_socket->SendTo(pkt, 0, dst);
        ++m_tx_pkt_count;
    }

    NS_LOG_INFO("EmitRequest node=" << my_node_id
                << " req=" << req_id
                << " L_in=" << L_in
                << " N_pkt=" << N_pkt);

    ScheduleNext();
}

} // namespace ns3