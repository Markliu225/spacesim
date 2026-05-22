#include "ns3/compute-application.h"

#include <algorithm>
#include <cmath>

#include "ns3/log.h"
#include "ns3/uinteger.h"
#include "ns3/double.h"
#include "ns3/string.h"
#include "ns3/simulator.h"

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
                      "T_prefill coefficient per input token (ns).",
                      UintegerValue(100000),
                      MakeUintegerAccessor(&ComputeApplication::m_alpha_ns_per_input_tok),
                      MakeUintegerChecker<uint64_t>())
        .AddAttribute("BetaNsPerOutputToken",
                      "T_decode coefficient per output token (ns).",
                      UintegerValue(50000),
                      MakeUintegerAccessor(&ComputeApplication::m_beta_ns_per_output_tok),
                      MakeUintegerChecker<uint64_t>())
        .AddAttribute("GammaNs",
                      "Fixed compute overhead per request (ns).",
                      UintegerValue(10000000),
                      MakeUintegerAccessor(&ComputeApplication::m_gamma_ns),
                      MakeUintegerChecker<uint64_t>())
        .AddAttribute("BytesPerToken",
                      "Bytes per output token (response payload sizing).",
                      UintegerValue(4),
                      MakeUintegerAccessor(&ComputeApplication::m_bytes_per_token),
                      MakeUintegerChecker<uint32_t>(1))
        .AddAttribute("LOutMean",
                      "Mean of response-token Normal distribution.",
                      DoubleValue(200.0),
                      MakeDoubleAccessor(&ComputeApplication::m_L_out_mean),
                      MakeDoubleChecker<double>(0.0))
        .AddAttribute("LOutStd",
                      "Std-dev of response-token Normal distribution.",
                      DoubleValue(50.0),
                      MakeDoubleAccessor(&ComputeApplication::m_L_out_std),
                      MakeDoubleChecker<double>(0.0))
        .AddAttribute("LOutMin",
                      "Lower clamp on L_out.",
                      UintegerValue(1),
                      MakeUintegerAccessor(&ComputeApplication::m_L_out_min),
                      MakeUintegerChecker<uint32_t>(1))
        .AddAttribute("LOutMax",
                      "Upper clamp on L_out.",
                      UintegerValue(1000),
                      MakeUintegerAccessor(&ComputeApplication::m_L_out_max),
                      MakeUintegerChecker<uint32_t>(1))
        .AddAttribute("LogFilename",
                      "Path to llm_compute_node<sat>.csv.",
                      StringValue(""),
                      MakeStringAccessor(&ComputeApplication::m_log_filename),
                      MakeStringChecker());
    return tid;
}

ComputeApplication::ComputeApplication()
    : m_alpha_ns_per_input_tok(100000),
      m_beta_ns_per_output_tok(50000),
      m_gamma_ns(10000000),
      m_bytes_per_token(4),
      m_L_out_mean(200.0), m_L_out_std(50.0),
      m_L_out_min(1), m_L_out_max(1000),
      m_busy(false), m_in_service_start_ns(0),
      m_complete_count(0)
{}

ComputeApplication::~ComputeApplication() {}

void
ComputeApplication::DoDispose()
{
    if (m_log.is_open()) m_log.close();
    for (auto &kv : m_resp) if (kv.first) kv.first->Close();
    m_resp.clear();
    Application::DoDispose();
}

void
ComputeApplication::StartApplication()
{
    NS_LOG_FUNCTION(this);
    if (m_log_filename.empty()) {
        NS_FATAL_ERROR("ComputeApplication node " << GetNode()->GetId()
                       << ": LogFilename empty.");
    }
    m_log.open(m_log_filename, std::ios::out | std::ios::trunc);
    if (!m_log.is_open()) {
        NS_FATAL_ERROR("Cannot open compute log: " << m_log_filename);
    }
    m_log << "req_id,compute_sat_id,src_node_id,t_queue_enter_ns,t_compute_start_ns,"
             "t_compute_end_ns,T_compute_ns,T_queue_wait_ns,L_in,L_out\n";

    m_L_out_rv = CreateObject<NormalRandomVariable>();
    m_L_out_rv->SetAttribute("Mean",     DoubleValue(m_L_out_mean));
    m_L_out_rv->SetAttribute("Variance", DoubleValue(m_L_out_std * m_L_out_std));
}

void
ComputeApplication::StopApplication()
{
    NS_LOG_FUNCTION(this);
    if (m_log.is_open()) m_log.flush();
    // Don't close response sockets here; they may still be draining.
}

void
ComputeApplication::OnGatherComplete(uint64_t req_id,
                                     uint32_t L_in,
                                     uint32_t src_node_id,
                                     uint64_t t_emit_request_ns,
                                     Ptr<Socket> sock)
{
    // Sample L_out (Option B: SAT decides response size).
    double s = m_L_out_rv->GetValue();
    if (s < (double) m_L_out_min) s = (double) m_L_out_min;
    if (s > (double) m_L_out_max) s = (double) m_L_out_max;
    uint32_t L_out = (uint32_t) std::lround(s);

    ComputeRequest r;
    r.req_id            = req_id;
    r.L_in              = L_in;
    r.L_out             = L_out;
    r.src_node_id       = src_node_id;
    r.t_emit_request_ns = t_emit_request_ns;
    r.t_queue_enter_ns  = (uint64_t) Simulator::Now().GetNanoSeconds();
    r.sock              = sock;
    m_queue.push(r);
    if (!m_busy) StartNextCompute();
}

void
ComputeApplication::StartNextCompute()
{
    if (m_queue.empty()) { m_busy = false; return; }
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
          << m_in_service.src_node_id << ','
          << m_in_service.t_queue_enter_ns << ','
          << m_in_service_start_ns  << ','
          << now_ns                 << ','
          << T_compute_ns           << ','
          << T_queue_wait_ns        << ','
          << m_in_service.L_in      << ','
          << m_in_service.L_out     << '\n';
    ++m_complete_count;

    SendResponse();
    StartNextCompute();
}

void
ComputeApplication::SendResponse()
{
    Ptr<Socket> sock = m_in_service.sock;
    if (!sock) {
        NS_LOG_WARN("ComputeApplication: in-service has null socket; dropping response");
        return;
    }
    uint64_t total = (uint64_t) m_in_service.L_out * m_bytes_per_token;
    RespState st;
    st.total_bytes = total;
    st.bytes_sent  = 0;
    st.finished_send = false;
    m_resp[sock] = st;

    sock->SetSendCallback(MakeCallback(&ComputeApplication::OnRespDataSent, this));
    // Override the recv side; the GS won't be sending more. We don't
    // need to react to its FIN here — the GS's own Close-callback will
    // log the response when it sees ours.
    sock->SetCloseCallbacks(
        MakeNullCallback<void, Ptr<Socket>>(),
        MakeCallback(&ComputeApplication::OnRespError, this));

    PumpResponse(sock);
}

bool
ComputeApplication::PumpResponse(Ptr<Socket> sock)
{
    auto it = m_resp.find(sock);
    if (it == m_resp.end()) return false;
    RespState &st = it->second;
    if (st.finished_send) return true;

    while (st.bytes_sent < st.total_bytes) {
        uint64_t remaining = st.total_bytes - st.bytes_sent;
        uint32_t chunk = (uint32_t) std::min<uint64_t>(remaining, 65000ULL);
        Ptr<Packet> pkt = Create<Packet>(chunk);
        int n = sock->Send(pkt);
        if (n < 0) return false;  // tx buffer full
        st.bytes_sent += (uint64_t) n;
        if ((uint32_t) n < chunk) return false;
    }

    if (!st.finished_send) {
        sock->ShutdownSend();
        st.finished_send = true;
    }
    return true;
}

void
ComputeApplication::OnRespDataSent(Ptr<Socket> sock, uint32_t /*txAvail*/)
{
    PumpResponse(sock);
}

void
ComputeApplication::OnRespError(Ptr<Socket> sock)
{
    NS_LOG_WARN("ComputeApplication: response socket error on node "
                << GetNode()->GetId());
    m_resp.erase(sock);
}

} // namespace ns3
