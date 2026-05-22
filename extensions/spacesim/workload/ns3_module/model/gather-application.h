/*
 * GatherApplication — TCP, compute-SAT side.
 *
 * TCP server bound to a port. For every accepted connection it runs a
 * tiny state machine:
 *
 *   reading_header (24 B)  →  reading_payload (L_in × BytesPerToken)  →  done
 *
 * On `done` it (a) writes one row to llm_gather_node<sat>.csv, (b) fires
 * the `GatherCompleteCallback` so the co-located ComputeApplication can
 * enqueue this request. The *socket itself* is passed in the callback —
 * the compute application will eventually send the response back on
 * the same socket.
 *
 * No more "wait for all N packets and timeout if missing" semantics —
 * TCP gives us in-order delivery and an EOF signal. We still emit
 * `llm_stuck_node<sat>.csv` (empty header) for back-compat with the
 * dashboard's run-discovery heuristics.
 */
#ifndef SPACESIM_GATHER_APPLICATION_H
#define SPACESIM_GATHER_APPLICATION_H

#include <fstream>
#include <map>
#include <string>
#include <vector>

#include "ns3/application.h"
#include "ns3/callback.h"
#include "ns3/socket.h"

#include "ns3/llm-header.h"

namespace ns3 {

class GatherApplication : public Application
{
public:
    // (req_id, L_in, src_node_id, t_emit_ns, socket)
    typedef Callback<void, uint64_t, uint32_t, uint32_t, uint64_t, Ptr<Socket>>
            GatherCompleteCallback;

    static TypeId GetTypeId(void);
    GatherApplication();
    ~GatherApplication() override;

    void SetGatherCompleteCallback(GatherCompleteCallback cb)
    { m_on_gather_complete = cb; }

    uint64_t GetGatherCompleteCount() const { return m_gather_count; }

protected:
    void DoDispose() override;

private:
    void StartApplication() override;
    void StopApplication() override;

    bool OnAcceptRequest(Ptr<Socket> sock, const Address &from);
    void OnAcceptNew    (Ptr<Socket> sock, const Address &from);
    void HandleRead     (Ptr<Socket> sock);
    void OnPeerClose    (Ptr<Socket> sock);
    void OnPeerError    (Ptr<Socket> sock);

    struct ConnState {
        // header progress
        std::vector<uint8_t> hbuf;     // accumulates first 24 bytes
        bool                 header_done = false;
        LLMHeader            header;
        // payload progress
        uint64_t             payload_target_bytes = 0;
        uint64_t             payload_received = 0;
        uint64_t             t_first_byte_ns = 0;
        uint64_t             t_last_byte_ns = 0;
        bool                 handed_off = false;
    };

    uint16_t                            m_port;
    uint32_t                            m_bytes_per_token;
    std::string                         m_log_filename;
    std::string                         m_stuck_log_filename;

    Ptr<Socket>                         m_listen_socket;
    std::map<Ptr<Socket>, ConnState>    m_conns;
    std::ofstream                       m_log;
    std::ofstream                       m_stuck_log;

    GatherCompleteCallback              m_on_gather_complete;
    uint64_t                            m_gather_count;
};

} // namespace ns3

#endif // SPACESIM_GATHER_APPLICATION_H
