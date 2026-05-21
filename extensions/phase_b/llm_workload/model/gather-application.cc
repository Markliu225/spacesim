#include "ns3/gather-application.h"

#include "ns3/log.h"
#include "ns3/inet-socket-address.h"
#include "ns3/udp-socket-factory.h"
#include "ns3/uinteger.h"
#include "ns3/string.h"
#include "ns3/simulator.h"

#include "ns3/llm-packet-tag.h"

namespace ns3 {

NS_LOG_COMPONENT_DEFINE("GatherApplication");
NS_OBJECT_ENSURE_REGISTERED(GatherApplication);

TypeId
GatherApplication::GetTypeId(void)
{
    static TypeId tid = TypeId("ns3::GatherApplication")
        .SetParent<Application>()
        .SetGroupName("LlmWorkload")
        .AddConstructor<GatherApplication>()
        .AddAttribute("Port",
                      "UDP port to bind for incoming REQUEST packets.",
                      UintegerValue(9999),
                      MakeUintegerAccessor(&GatherApplication::m_port),
                      MakeUintegerChecker<uint16_t>())
        .AddAttribute("TimeoutNs",
                      "Per-request timeout. After this long without "
                      "all packets arriving, the entry is reaped and "
                      "logged to stuck_log.csv. Default 30 s.",
                      UintegerValue(30000000000ULL),
                      MakeUintegerAccessor(&GatherApplication::m_timeout_ns),
                      MakeUintegerChecker<uint64_t>())
        .AddAttribute("LogFilename",
                      "Path to gather_log.csv (per completed gather event).",
                      StringValue(""),
                      MakeStringAccessor(&GatherApplication::m_log_filename),
                      MakeStringChecker())
        .AddAttribute("StuckLogFilename",
                      "Path to stuck_log.csv (per timed-out gather entry).",
                      StringValue(""),
                      MakeStringAccessor(&GatherApplication::m_stuck_log_filename),
                      MakeStringChecker());
    return tid;
}

GatherApplication::GatherApplication()
    : m_port(9999),
      m_timeout_ns(30000000000ULL),
      m_socket(nullptr),
      m_rx_pkt_count(0),
      m_gather_count(0),
      m_timeout_count(0)
{
}

GatherApplication::~GatherApplication() {}

void
GatherApplication::DoDispose()
{
    if (m_socket) { m_socket = nullptr; }
    if (m_log.is_open()) { m_log.close(); }
    if (m_stuck_log.is_open()) { m_stuck_log.close(); }
    Application::DoDispose();
}

void
GatherApplication::StartApplication()
{
    NS_LOG_FUNCTION(this);
    if (m_log_filename.empty()) {
        NS_FATAL_ERROR("GatherApplication on node " << GetNode()->GetId()
                       << ": LogFilename empty.");
    }
    m_log.open(m_log_filename.c_str(), std::ios::out | std::ios::trunc);
    if (!m_log.is_open()) {
        NS_FATAL_ERROR("Cannot open gather_log: " << m_log_filename);
    }
    m_log << "req_id,compute_sat_id,t_first_arrival_ns,t_last_arrival_ns,"
             "D_gather_ns,total_pkts_expected,total_pkts_received,"
             "src_node_id,L_in,L_out_expected,t_emit_ns\n";

    if (!m_stuck_log_filename.empty()) {
        m_stuck_log.open(m_stuck_log_filename.c_str(),
                         std::ios::out | std::ios::trunc);
        if (m_stuck_log.is_open()) {
            m_stuck_log << "req_id,compute_sat_id,t_first_arrival_ns,"
                           "t_timeout_ns,total_pkts_expected,"
                           "total_pkts_received,src_node_id\n";
        }
    }

    m_socket = Socket::CreateSocket(GetNode(),
                                    UdpSocketFactory::GetTypeId());
    InetSocketAddress local = InetSocketAddress(Ipv4Address::GetAny(), m_port);
    if (m_socket->Bind(local) != 0) {
        NS_FATAL_ERROR("GatherApplication bind failed on port " << m_port);
    }
    m_socket->SetRecvCallback(MakeCallback(&GatherApplication::HandleRead, this));
}

void
GatherApplication::StopApplication()
{
    NS_LOG_FUNCTION(this);
    if (m_socket) {
        m_socket->Close();
        m_socket->SetRecvCallback(MakeNullCallback<void, Ptr<Socket>>());
        m_socket = nullptr;
    }
    // Cancel any pending timeouts to avoid spurious post-sim callbacks.
    for (auto &kv : m_pending) {
        if (kv.second.timeout_event.IsRunning()) {
            Simulator::Cancel(kv.second.timeout_event);
        }
    }
    if (m_log.is_open()) { m_log.flush(); m_log.close(); }
    if (m_stuck_log.is_open()) { m_stuck_log.flush(); m_stuck_log.close(); }
}

void
GatherApplication::HandleRead(Ptr<Socket> socket)
{
    Ptr<Packet> packet;
    Address from;
    uint64_t now_ns = (uint64_t) Simulator::Now().GetNanoSeconds();
    while ((packet = socket->RecvFrom(from))) {
        LLMPacketTag tag;
        if (!packet->PeekPacketTag(tag)) {
            continue;
        }
        if (tag.GetDirection() != LLMPacketTag::REQUEST) {
            // Phase C: GatherApplication only gathers REQUEST packets.
            continue;
        }
        ++m_rx_pkt_count;

        uint64_t req_id = tag.GetReqId();
        auto it = m_pending.find(req_id);
        if (it == m_pending.end()) {
            GatherState st;
            st.total_pkts          = tag.GetTotalPkts();
            st.t_first_arrival_ns  = now_ns;
            st.t_last_arrival_ns   = now_ns;
            st.L_in                = tag.GetLIn();
            st.L_out_expected      = tag.GetLOutExpected();
            st.src_node_id         = tag.GetSrcNodeId();
            st.t_emit_ns           = tag.GetTEmitNs();
            st.received_pkt_ids.insert(tag.GetPacketId());
            st.timeout_event = Simulator::Schedule(
                NanoSeconds(m_timeout_ns),
                &GatherApplication::OnTimeout, this, req_id);
            m_pending[req_id] = st;
        } else {
            GatherState &st = it->second;
            st.received_pkt_ids.insert(tag.GetPacketId());
            st.t_last_arrival_ns = now_ns;
        }

        GatherState &st = m_pending[req_id];
        if (st.received_pkt_ids.size() == (size_t) st.total_pkts) {
            // Complete.
            if (st.timeout_event.IsRunning()) {
                Simulator::Cancel(st.timeout_event);
            }
            uint64_t D_gather_ns = st.t_last_arrival_ns - st.t_first_arrival_ns;
            m_log << req_id << ','
                  << GetNode()->GetId() << ','
                  << st.t_first_arrival_ns << ','
                  << st.t_last_arrival_ns  << ','
                  << D_gather_ns           << ','
                  << st.total_pkts         << ','
                  << st.received_pkt_ids.size() << ','
                  << st.src_node_id        << ','
                  << st.L_in               << ','
                  << st.L_out_expected     << ','
                  << st.t_emit_ns          << '\n';
            ++m_gather_count;

            if (!m_on_gather_complete.IsNull()) {
                m_on_gather_complete(req_id, st.L_in, st.L_out_expected,
                                     st.src_node_id, st.t_emit_ns);
            }
            m_pending.erase(req_id);
        }
    }
}

void
GatherApplication::OnTimeout(uint64_t req_id)
{
    auto it = m_pending.find(req_id);
    if (it == m_pending.end()) return;
    const GatherState &st = it->second;
    uint64_t now_ns = (uint64_t) Simulator::Now().GetNanoSeconds();
    ++m_timeout_count;
    if (m_stuck_log.is_open()) {
        m_stuck_log << req_id << ','
                    << GetNode()->GetId() << ','
                    << st.t_first_arrival_ns << ','
                    << now_ns << ','
                    << st.total_pkts << ','
                    << st.received_pkt_ids.size() << ','
                    << st.src_node_id << '\n';
    }
    m_pending.erase(it);
}

} // namespace ns3