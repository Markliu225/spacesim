/*
 * GatherApplication — Phase C.
 *
 * Installed on a compute satellite. Listens on a UDP port for REQUEST
 * packets carrying LLMPacketTag. Maintains an in-flight map keyed by
 * req_id; when all packets of a request have arrived (received_count
 * == total_pkts), records gather metrics to gather_log.csv and fires
 * an `OnGatherComplete` callback so the co-located ComputeApplication
 * can start prefill/decode.
 *
 * Timeout: each in-flight entry has a `timeout_event` scheduled at
 * `now + TimeoutNs`. If a request never receives all its packets the
 * entry is reaped (no compute fired) and a row is written to
 * stuck_log.csv to make routing/queueing pathologies investigable
 * after the fact.
 *
 * Hot path uses integer streaming to std::ofstream; no formatting.
 */
#ifndef GATHER_APPLICATION_H
#define GATHER_APPLICATION_H

#include <fstream>
#include <map>
#include <set>
#include <string>

#include "ns3/application.h"
#include "ns3/callback.h"
#include "ns3/event-id.h"
#include "ns3/socket.h"

namespace ns3 {

class GatherApplication : public Application
{
public:
    // (req_id, L_in, L_out_expected, src_node_id, t_emit_request_ns)
    typedef Callback<void, uint64_t, uint32_t, uint32_t, uint32_t, uint64_t>
            GatherCompleteCallback;

    static TypeId GetTypeId(void);
    GatherApplication();
    ~GatherApplication() override;

    void SetGatherCompleteCallback(GatherCompleteCallback cb)
    { m_on_gather_complete = cb; }

    uint64_t GetRxPacketCount() const   { return m_rx_pkt_count; }
    uint64_t GetGatherCompleteCount() const { return m_gather_count; }
    uint64_t GetTimeoutCount() const    { return m_timeout_count; }

protected:
    void DoDispose() override;

private:
    void StartApplication() override;
    void StopApplication() override;
    void HandleRead(Ptr<Socket> socket);
    void OnTimeout(uint64_t req_id);

    struct GatherState {
        std::set<uint16_t> received_pkt_ids;
        uint16_t total_pkts;
        uint64_t t_first_arrival_ns;
        uint64_t t_last_arrival_ns;
        uint32_t L_in;
        uint32_t L_out_expected;
        uint32_t src_node_id;
        uint64_t t_emit_ns;
        EventId  timeout_event;
    };

    uint16_t                            m_port;
    uint64_t                            m_timeout_ns;
    std::string                         m_log_filename;
    std::string                         m_stuck_log_filename;

    Ptr<Socket>                         m_socket;
    std::map<uint64_t, GatherState>     m_pending;
    std::ofstream                       m_log;
    std::ofstream                       m_stuck_log;

    GatherCompleteCallback              m_on_gather_complete;

    uint64_t                            m_rx_pkt_count;
    uint64_t                            m_gather_count;
    uint64_t                            m_timeout_count;
};

} // namespace ns3

#endif // GATHER_APPLICATION_H