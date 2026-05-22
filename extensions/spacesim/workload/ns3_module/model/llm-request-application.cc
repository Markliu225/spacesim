#include "ns3/llm-request-application.h"

#include <algorithm>
#include <cctype>
#include <cmath>
#include <cstring>
#include <fstream>
#include <iostream>

#include "ns3/log.h"
#include "ns3/inet-socket-address.h"
#include "ns3/tcp-socket-factory.h"
#include "ns3/uinteger.h"
#include "ns3/double.h"
#include "ns3/string.h"
#include "ns3/simulator.h"

#include "ns3/llm-header.h"

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
                      "Destination compute-SAT IPv4 address.",
                      AddressValue(),
                      MakeAddressAccessor(&LLMRequestApplication::m_dst_addr),
                      MakeAddressChecker())
        .AddAttribute("DestPort",
                      "Destination TCP port on the compute SAT.",
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
                      "Std-dev of prompt-token count.",
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
                      "Bytes used to encode one prompt token.",
                      UintegerValue(4),
                      MakeUintegerAccessor(&LLMRequestApplication::m_bytes_per_token),
                      MakeUintegerChecker<uint32_t>(1))
        .AddAttribute("ResponseLogFilename",
                      "Path to llm_response_node<gs>.csv.",
                      StringValue(""),
                      MakeStringAccessor(&LLMRequestApplication::m_response_log_filename),
                      MakeStringChecker())
        .AddAttribute("EventsFilename",
                      "Optional path to a per-GS events CSV "
                      "(columns: req_id,src_gs_idx,t_emit_ns,L_in,L_out). "
                      "When non-empty the app schedules each event at its "
                      "t_emit_ns with the row's L_in, and the distribution "
                      "attributes above are ignored.",
                      StringValue(""),
                      MakeStringAccessor(&LLMRequestApplication::m_events_filename),
                      MakeStringChecker());
    return tid;
}

LLMRequestApplication::LLMRequestApplication()
    : m_dst_port(9999), m_lambda(10.0),
      m_L_in_mean(500.0), m_L_in_std(100.0),
      m_L_in_min(1), m_L_in_max(2000),
      m_bytes_per_token(4),
      m_req_counter(0), m_tx_pkt_count(0)
{}

LLMRequestApplication::~LLMRequestApplication() {}

void
LLMRequestApplication::DoDispose()
{
    if (m_next_event.IsRunning()) Simulator::Cancel(m_next_event);
    for (auto &kv : m_conns) if (kv.first) kv.first->Close();
    m_conns.clear();
    if (m_response_log.is_open()) m_response_log.close();
    Application::DoDispose();
}

void
LLMRequestApplication::StartApplication()
{
    NS_LOG_FUNCTION(this);
    if (m_L_in_max < m_L_in_min) NS_FATAL_ERROR("LInMax < LInMin");

    if (m_response_log_filename.empty()) {
        NS_FATAL_ERROR("LLMRequestApplication node " << GetNode()->GetId()
                       << ": ResponseLogFilename empty.");
    }
    m_response_log.open(m_response_log_filename, std::ios::out | std::ios::trunc);
    if (!m_response_log.is_open()) {
        NS_FATAL_ERROR("Cannot open response log: " << m_response_log_filename);
    }
    m_response_log
        << "req_id,gs_node_id,response_pkt_id,total_response_pkts,"
           "t_response_emit_ns,t_response_recv_ns,network_return_delay_ns,"
           "src_compute_sat_id,L_in,L_out\n";

    if (!m_events_filename.empty()) {
        // Trace-replay: schedule every event up-front from the file.
        ScheduleEventsFromFile();
    } else {
        // Distribution-driven: lazy chain of Poisson inter-arrivals.
        if (m_lambda <= 0.0) NS_FATAL_ERROR("Lambda must be > 0 in distribution mode");
        m_iat_rv = CreateObject<ExponentialRandomVariable>();
        m_iat_rv->SetAttribute("Mean", DoubleValue(1.0 / m_lambda));
        m_L_in_rv = CreateObject<NormalRandomVariable>();
        m_L_in_rv->SetAttribute("Mean",     DoubleValue(m_L_in_mean));
        m_L_in_rv->SetAttribute("Variance", DoubleValue(m_L_in_std * m_L_in_std));
        ScheduleNext();
    }
}

void
LLMRequestApplication::ScheduleEventsFromFile()
{
    // events CSV columns: req_id,src_gs_idx,t_emit_ns,L_in,L_out
    // Path is relative to ns-3's run dir.
    std::ifstream f(m_events_filename);
    if (!f.is_open()) {
        NS_FATAL_ERROR("LLMRequestApplication node " << GetNode()->GetId()
                       << ": cannot open events file: " << m_events_filename);
    }
    std::string line;
    bool first = true;
    uint64_t scheduled = 0;
    uint64_t skipped = 0;
    uint64_t now_ns = (uint64_t) Simulator::Now().GetNanoSeconds();
    while (std::getline(f, line)) {
        if (line.empty()) continue;
        if (first) {
            // skip header (any line that starts with a non-digit char)
            first = false;
            if (!line.empty() && !std::isdigit((unsigned char) line[0])) continue;
        }
        // Parse req_id,src_gs_idx,t_emit_ns,L_in,L_out
        size_t p0 = line.find(',');
        size_t p1 = (p0 == std::string::npos) ? std::string::npos : line.find(',', p0 + 1);
        size_t p2 = (p1 == std::string::npos) ? std::string::npos : line.find(',', p1 + 1);
        size_t p3 = (p2 == std::string::npos) ? std::string::npos : line.find(',', p2 + 1);
        if (p3 == std::string::npos) { ++skipped; continue; }
        uint64_t t_emit_ns;
        uint32_t L_in;
        try {
            t_emit_ns = std::stoull(line.substr(p1 + 1, p2 - p1 - 1));
            L_in = (uint32_t) std::stoul(line.substr(p2 + 1, p3 - p2 - 1));
        } catch (const std::exception &) {
            ++skipped;
            continue;
        }
        if (L_in < m_L_in_min) L_in = m_L_in_min;
        if (L_in > m_L_in_max) L_in = m_L_in_max;
        // Schedule relative to now. Drop events whose absolute time has
        // already passed (only meaningful if StartTime was set after t=0).
        if (t_emit_ns < now_ns) { ++skipped; continue; }
        Simulator::Schedule(NanoSeconds(t_emit_ns - now_ns),
                            &LLMRequestApplication::EmitRequestWithLIn,
                            this, L_in);
        ++scheduled;
    }
    std::cout << "  > LLMRequest node " << GetNode()->GetId()
              << " replay: scheduled=" << scheduled
              << " skipped=" << skipped
              << " from " << m_events_filename << std::endl;
}

void
LLMRequestApplication::StopApplication()
{
    NS_LOG_FUNCTION(this);
    if (m_next_event.IsRunning()) Simulator::Cancel(m_next_event);
    if (m_response_log.is_open()) m_response_log.flush();
    // Do NOT close in-flight sockets; their callbacks still need to log.
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
    double L_in_sample = m_L_in_rv->GetValue();
    if (L_in_sample < (double) m_L_in_min) L_in_sample = (double) m_L_in_min;
    if (L_in_sample > (double) m_L_in_max) L_in_sample = (double) m_L_in_max;
    uint32_t L_in = (uint32_t) std::lround(L_in_sample);
    EmitRequestWithLIn(L_in);
    ScheduleNext();
}

void
LLMRequestApplication::EmitRequestWithLIn(uint32_t L_in)
{
    uint64_t req_id = m_req_counter++;
    uint64_t t_emit_ns = (uint64_t) Simulator::Now().GetNanoSeconds();

    Ptr<Socket> sock = Socket::CreateSocket(GetNode(),
                                            TcpSocketFactory::GetTypeId());
    sock->SetConnectCallback(
        MakeCallback(&LLMRequestApplication::OnConnectSuccess, this),
        MakeCallback(&LLMRequestApplication::OnConnectFail,    this));
    sock->SetCloseCallbacks(
        MakeCallback(&LLMRequestApplication::OnClosedNormal, this),
        MakeCallback(&LLMRequestApplication::OnClosedError,  this));
    sock->SetRecvCallback(MakeCallback(&LLMRequestApplication::OnRecv, this));
    sock->SetSendCallback(MakeCallback(&LLMRequestApplication::OnDataSent, this));

    ReqConn rc;
    rc.req_id = req_id;
    rc.L_in   = L_in;
    rc.t_emit_ns = t_emit_ns;
    rc.total_send_bytes =
        LLMHeader::SIZE_BYTES + (uint64_t) L_in * m_bytes_per_token;
    rc.bytes_sent = 0;
    rc.header_sent = false;
    rc.finished_send = false;
    rc.bytes_received = 0;
    rc.t_first_byte_ns = 0;
    rc.t_last_byte_ns = 0;
    rc.logged = false;
    m_conns[sock] = rc;

    InetSocketAddress dst = InetSocketAddress(
        Ipv4Address::ConvertFrom(m_dst_addr), m_dst_port);
    sock->Bind();
    sock->Connect(dst);
    ++m_tx_pkt_count;

    NS_LOG_INFO("EmitRequest node=" << GetNode()->GetId()
                << " req=" << req_id << " L_in=" << L_in);
}

void
LLMRequestApplication::OnConnectSuccess(Ptr<Socket> sock)
{
    PumpSend(sock);
}

void
LLMRequestApplication::OnConnectFail(Ptr<Socket> sock)
{
    NS_LOG_WARN("LLMRequest: Connect failed on node " << GetNode()->GetId());
    m_conns.erase(sock);
}

void
LLMRequestApplication::OnDataSent(Ptr<Socket> sock, uint32_t /*txAvail*/)
{
    PumpSend(sock);
}

bool
LLMRequestApplication::PumpSend(Ptr<Socket> sock)
{
    auto it = m_conns.find(sock);
    if (it == m_conns.end()) return false;
    ReqConn &rc = it->second;
    if (rc.finished_send) return true;

    if (!rc.header_sent) {
        LLMHeader hdr;
        hdr.t_emit_ns   = rc.t_emit_ns;
        hdr.req_id      = (uint32_t) rc.req_id;
        hdr.src_node_id = GetNode()->GetId();
        hdr.L_in        = rc.L_in;
        uint8_t buf[LLMHeader::SIZE_BYTES];
        hdr.Pack(buf);
        Ptr<Packet> pkt = Create<Packet>(buf, LLMHeader::SIZE_BYTES);
        int n = sock->Send(pkt);
        if (n < 0) return false;
        rc.header_sent = true;
        rc.bytes_sent += (uint64_t) n;
    }

    while (rc.bytes_sent < rc.total_send_bytes) {
        uint64_t remaining = rc.total_send_bytes - rc.bytes_sent;
        uint32_t chunk = (uint32_t) std::min<uint64_t>(remaining, 65000ULL);
        Ptr<Packet> pkt = Create<Packet>(chunk);
        int n = sock->Send(pkt);
        if (n < 0) return false;
        rc.bytes_sent += (uint64_t) n;
        if ((uint32_t) n < chunk) return false;
    }

    if (!rc.finished_send) {
        sock->ShutdownSend();
        rc.finished_send = true;
    }
    return true;
}

void
LLMRequestApplication::OnRecv(Ptr<Socket> sock)
{
    auto it = m_conns.find(sock);
    if (it == m_conns.end()) return;
    ReqConn &rc = it->second;
    Ptr<Packet> packet;
    Address from;
    uint64_t now_ns = (uint64_t) Simulator::Now().GetNanoSeconds();
    while ((packet = sock->RecvFrom(from))) {
        uint32_t sz = packet->GetSize();
        if (sz == 0) break;
        if (rc.bytes_received == 0) rc.t_first_byte_ns = now_ns;
        rc.bytes_received += sz;
        rc.t_last_byte_ns = now_ns;
    }
}

void
LLMRequestApplication::OnClosedNormal(Ptr<Socket> sock)
{
    auto it = m_conns.find(sock);
    if (it == m_conns.end()) return;
    ReqConn &rc = it->second;
    if (rc.logged) return;
    rc.logged = true;

    if (m_response_log.is_open() && rc.bytes_received > 0) {
        const uint64_t total_response_bytes = rc.bytes_received;
        const uint32_t L_out_recovered = (uint32_t)(total_response_bytes /
                                                    std::max<uint32_t>(m_bytes_per_token, 1));
        const uint32_t my_gs = GetNode()->GetId();
        // row 0: first-byte event
        m_response_log
            << rc.req_id << ',' << my_gs << ",0,2,"
            << rc.t_first_byte_ns << ',' << rc.t_first_byte_ns << ",0,-1,"
            << rc.L_in << ',' << L_out_recovered << '\n';
        // row 1: last-byte event
        m_response_log
            << rc.req_id << ',' << my_gs << ",1,2,"
            << rc.t_first_byte_ns << ',' << rc.t_last_byte_ns << ','
            << (rc.t_last_byte_ns - rc.t_first_byte_ns) << ",-1,"
            << rc.L_in << ',' << L_out_recovered << '\n';
        m_response_log.flush();
    }
    sock->Close();
    m_conns.erase(it);
}

void
LLMRequestApplication::OnClosedError(Ptr<Socket> sock)
{
    NS_LOG_WARN("LLMRequest: socket closed with error on node "
                << GetNode()->GetId());
    m_conns.erase(sock);
}

} // namespace ns3
