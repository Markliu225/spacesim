/*
 * LLMRequestApplication — TCP, GS side.
 *
 * Generates LLM requests on a Poisson process. Each request opens a
 * fresh TCP connection to the destination compute SAT and:
 *
 *   1. sends a 24-byte LLMHeader (t_emit_ns / req_id / src_node_id / L_in)
 *   2. sends L_in * BytesPerToken bytes of "prompt payload" (zeros)
 *   3. ShutdownSend  → SAT side reads until EOF
 *   4. continues to drain *response* bytes on the same socket
 *   5. when the SAT half-closes (FIN observed), writes the response log
 *
 * Response-log compatibility
 * --------------------------
 *
 * We emit *two* rows per request — response_pkt_id=0 (first byte) and
 * response_pkt_id=1 (last byte) — so analysis/lifecycle.py's existing
 * groupby(min/max of t_response_recv_ns) recovers TTFT / total-recv
 * without parser changes.
 */
#ifndef SPACESIM_LLM_REQUEST_APPLICATION_H
#define SPACESIM_LLM_REQUEST_APPLICATION_H

#include <fstream>
#include <map>
#include <string>

#include "ns3/application.h"
#include "ns3/address.h"
#include "ns3/event-id.h"
#include "ns3/socket.h"
#include "ns3/random-variable-stream.h"

namespace ns3 {

class LLMRequestApplication : public Application
{
public:
    static TypeId GetTypeId(void);
    LLMRequestApplication();
    ~LLMRequestApplication() override;

    uint64_t GetTxRequestCount() const { return m_req_counter; }
    uint64_t GetTxPacketCount() const  { return m_tx_pkt_count; }

protected:
    void DoDispose() override;

private:
    void StartApplication() override;
    void StopApplication() override;

    // Distribution-driven path:
    void ScheduleNext();
    void EmitRequest();

    // Events-replay path:
    void ScheduleEventsFromFile();
    void EmitRequestWithLIn(uint32_t L_in);

    void OnConnectSuccess(Ptr<Socket> sock);
    void OnConnectFail   (Ptr<Socket> sock);
    void OnDataSent      (Ptr<Socket> sock, uint32_t txAvail);
    void OnRecv          (Ptr<Socket> sock);
    void OnClosedNormal  (Ptr<Socket> sock);
    void OnClosedError   (Ptr<Socket> sock);

    bool PumpSend(Ptr<Socket> sock);

    // attributes
    Address     m_dst_addr;
    uint16_t    m_dst_port;
    double      m_lambda;
    double      m_L_in_mean;
    double      m_L_in_std;
    uint32_t    m_L_in_min;
    uint32_t    m_L_in_max;
    uint32_t    m_bytes_per_token;
    std::string m_response_log_filename;
    // When non-empty, replay events from this file (path relative to
    // ns-3's run dir). Distribution attributes above are ignored.
    std::string m_events_filename;

    // state
    struct ReqConn {
        uint64_t req_id;
        uint32_t L_in;
        uint64_t t_emit_ns;
        uint64_t total_send_bytes;
        uint64_t bytes_sent;
        bool     header_sent;
        bool     finished_send;
        uint64_t bytes_received;
        uint64_t t_first_byte_ns;
        uint64_t t_last_byte_ns;
        bool     logged;
    };
    std::map<Ptr<Socket>, ReqConn>   m_conns;
    Ptr<ExponentialRandomVariable>   m_iat_rv;
    Ptr<NormalRandomVariable>        m_L_in_rv;
    EventId                          m_next_event;
    std::ofstream                    m_response_log;
    uint64_t                         m_req_counter;
    uint64_t                         m_tx_pkt_count;
};

} // namespace ns3

#endif // SPACESIM_LLM_REQUEST_APPLICATION_H
