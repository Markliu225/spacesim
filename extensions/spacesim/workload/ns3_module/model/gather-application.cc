#include "ns3/gather-application.h"

#include <algorithm>
#include <cstring>

#include "ns3/log.h"
#include "ns3/inet-socket-address.h"
#include "ns3/tcp-socket-factory.h"
#include "ns3/uinteger.h"
#include "ns3/string.h"
#include "ns3/simulator.h"

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
                      "TCP port to listen on for incoming requests.",
                      UintegerValue(9999),
                      MakeUintegerAccessor(&GatherApplication::m_port),
                      MakeUintegerChecker<uint16_t>())
        .AddAttribute("BytesPerToken",
                      "Bytes per prompt token (for payload-size computation).",
                      UintegerValue(4),
                      MakeUintegerAccessor(&GatherApplication::m_bytes_per_token),
                      MakeUintegerChecker<uint32_t>(1))
        .AddAttribute("LogFilename",
                      "Path to llm_gather_node<sat>.csv.",
                      StringValue(""),
                      MakeStringAccessor(&GatherApplication::m_log_filename),
                      MakeStringChecker())
        .AddAttribute("StuckLogFilename",
                      "Path to llm_stuck_node<sat>.csv (kept for "
                      "dashboard run-discovery compat; TCP rarely produces "
                      "rows here).",
                      StringValue(""),
                      MakeStringAccessor(&GatherApplication::m_stuck_log_filename),
                      MakeStringChecker());
    return tid;
}

GatherApplication::GatherApplication()
    : m_port(9999), m_bytes_per_token(4),
      m_listen_socket(nullptr), m_gather_count(0)
{}

GatherApplication::~GatherApplication() {}

void
GatherApplication::DoDispose()
{
    if (m_listen_socket) m_listen_socket = nullptr;
    for (auto &kv : m_conns) if (kv.first) kv.first->Close();
    m_conns.clear();
    if (m_log.is_open()) m_log.close();
    if (m_stuck_log.is_open()) m_stuck_log.close();
    Application::DoDispose();
}

void
GatherApplication::StartApplication()
{
    NS_LOG_FUNCTION(this);

    if (m_log_filename.empty()) {
        NS_FATAL_ERROR("GatherApplication node " << GetNode()->GetId()
                       << ": LogFilename empty.");
    }
    m_log.open(m_log_filename, std::ios::out | std::ios::trunc);
    if (!m_log.is_open()) {
        NS_FATAL_ERROR("Cannot open gather log: " << m_log_filename);
    }
    // Same columns as v1 UDP so analysis/lifecycle.py needs no changes.
    // L_out_expected is recorded as 0 (Option B: SAT samples L_out; the
    // value used will be in llm_compute_node<sat>.csv). total_pkts_*
    // become "transfer events": for TCP we emit total=2 (header arrival
    // + payload final) — these fields are mostly cosmetic now.
    m_log << "req_id,compute_sat_id,t_first_arrival_ns,t_last_arrival_ns,"
             "D_gather_ns,total_pkts_expected,total_pkts_received,"
             "src_node_id,L_in,L_out_expected,t_emit_ns\n";

    if (!m_stuck_log_filename.empty()) {
        m_stuck_log.open(m_stuck_log_filename, std::ios::out | std::ios::trunc);
        if (m_stuck_log.is_open()) {
            m_stuck_log << "req_id,compute_sat_id,t_first_arrival_ns,"
                           "t_timeout_ns,total_pkts_expected,"
                           "total_pkts_received,src_node_id\n";
        }
    }

    m_listen_socket = Socket::CreateSocket(GetNode(),
                                           TcpSocketFactory::GetTypeId());
    InetSocketAddress local = InetSocketAddress(Ipv4Address::GetAny(), m_port);
    if (m_listen_socket->Bind(local) != 0) {
        NS_FATAL_ERROR("GatherApplication Bind failed on port " << m_port);
    }
    if (m_listen_socket->Listen() != 0) {
        NS_FATAL_ERROR("GatherApplication Listen failed on port " << m_port);
    }
    m_listen_socket->SetAcceptCallback(
        MakeCallback(&GatherApplication::OnAcceptRequest, this),
        MakeCallback(&GatherApplication::OnAcceptNew, this));
}

void
GatherApplication::StopApplication()
{
    NS_LOG_FUNCTION(this);
    if (m_listen_socket) {
        m_listen_socket->Close();
        m_listen_socket->SetAcceptCallback(
            MakeNullCallback<bool, Ptr<Socket>, const Address &>(),
            MakeNullCallback<void, Ptr<Socket>, const Address &>());
        m_listen_socket = nullptr;
    }
    // Leave open per-conn sockets alone — ComputeApplication still needs
    // them to send responses. Their close callbacks will clean up.
    if (m_log.is_open()) m_log.flush();
    if (m_stuck_log.is_open()) m_stuck_log.flush();
}

bool
GatherApplication::OnAcceptRequest(Ptr<Socket> /*sock*/, const Address &/*from*/)
{
    return true;  // accept every incoming connection
}

void
GatherApplication::OnAcceptNew(Ptr<Socket> sock, const Address &/*from*/)
{
    ConnState st;
    st.hbuf.reserve(LLMHeader::SIZE_BYTES);
    m_conns[sock] = st;
    sock->SetRecvCallback(MakeCallback(&GatherApplication::HandleRead, this));
    sock->SetCloseCallbacks(
        MakeCallback(&GatherApplication::OnPeerClose, this),
        MakeCallback(&GatherApplication::OnPeerError, this));
}

void
GatherApplication::HandleRead(Ptr<Socket> sock)
{
    auto it = m_conns.find(sock);
    if (it == m_conns.end()) return;
    ConnState &st = it->second;

    Ptr<Packet> packet;
    Address from;
    uint64_t now_ns = (uint64_t) Simulator::Now().GetNanoSeconds();
    while ((packet = sock->RecvFrom(from))) {
        uint32_t sz = packet->GetSize();
        if (sz == 0) break;

        if (st.t_first_byte_ns == 0) st.t_first_byte_ns = now_ns;
        st.t_last_byte_ns = now_ns;

        // Copy out bytes once and consume sequentially.
        std::vector<uint8_t> buf(sz);
        packet->CopyData(buf.data(), sz);
        size_t pos = 0;

        // Phase 1: accumulate header.
        if (!st.header_done) {
            size_t need = LLMHeader::SIZE_BYTES - st.hbuf.size();
            size_t take = std::min<size_t>(need, sz - pos);
            st.hbuf.insert(st.hbuf.end(), buf.begin() + pos, buf.begin() + pos + take);
            pos += take;
            if (st.hbuf.size() == LLMHeader::SIZE_BYTES) {
                st.header.Unpack(st.hbuf.data());
                st.header_done = true;
                st.payload_target_bytes =
                    (uint64_t) st.header.L_in * m_bytes_per_token;
                if (st.payload_target_bytes == 0) {
                    // Edge case: zero-token prompt. Hand off immediately.
                    // (Falls through into the "done" check below.)
                }
            }
        }

        // Phase 2: payload bytes (anything left in this packet).
        if (st.header_done && pos < sz) {
            uint64_t remaining = st.payload_target_bytes - st.payload_received;
            uint64_t take = std::min<uint64_t>(remaining, sz - pos);
            st.payload_received += take;
            pos += (size_t) take;
            // Drop any bytes beyond the declared payload length (shouldn't
            // happen with our own LLMRequest, but be defensive).
        }

        // Done?
        if (st.header_done && !st.handed_off
            && st.payload_received >= st.payload_target_bytes)
        {
            st.handed_off = true;
            uint64_t D_gather_ns = st.t_last_byte_ns - st.t_first_byte_ns;
            m_log << st.header.req_id          << ','
                  << GetNode()->GetId()        << ','
                  << st.t_first_byte_ns        << ','
                  << st.t_last_byte_ns         << ','
                  << D_gather_ns               << ','
                  << 2                         << ','  // total_pkts_expected (cosmetic)
                  << 2                         << ','  // total_pkts_received
                  << st.header.src_node_id     << ','
                  << st.header.L_in            << ','
                  << 0                         << ','  // L_out_expected (Option B → unknown)
                  << st.header.t_emit_ns       << '\n';
            ++m_gather_count;
            if (!m_on_gather_complete.IsNull()) {
                m_on_gather_complete(st.header.req_id, st.header.L_in,
                                     st.header.src_node_id, st.header.t_emit_ns,
                                     sock);
            }
            // Note: socket is now in ComputeApplication's care.
        }
    }
}

void
GatherApplication::OnPeerClose(Ptr<Socket> sock)
{
    // GS half-closed (FIN). If our state machine isn't complete yet,
    // the request never delivered a full payload — log to stuck.
    auto it = m_conns.find(sock);
    if (it == m_conns.end()) return;
    ConnState &st = it->second;
    if (!st.handed_off && m_stuck_log.is_open()) {
        uint64_t now_ns = (uint64_t) Simulator::Now().GetNanoSeconds();
        m_stuck_log << (st.header_done ? st.header.req_id : 0) << ','
                    << GetNode()->GetId() << ','
                    << st.t_first_byte_ns << ','
                    << now_ns << ',' << 2 << ',' << (st.handed_off ? 2 : 1) << ','
                    << (st.header_done ? st.header.src_node_id : 0) << '\n';
    }
    // Don't erase the socket if it was handed off: compute owns it.
    if (!st.handed_off) {
        sock->Close();
        m_conns.erase(it);
    } else {
        m_conns.erase(it);
    }
}

void
GatherApplication::OnPeerError(Ptr<Socket> sock)
{
    NS_LOG_WARN("Gather: peer error on node " << GetNode()->GetId());
    auto it = m_conns.find(sock);
    if (it != m_conns.end()) m_conns.erase(it);
}

} // namespace ns3
