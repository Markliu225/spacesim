/*
 * ComputeApplication — Phase C.
 *
 * Installed on a compute satellite. Subscribes to gather-complete events
 * from a co-located GatherApplication via a callback; on each completion
 * pushes a ComputeRequest into a FIFO queue. A single service worker pops
 * the queue, schedules `T_compute_ns = alpha*L_in + beta*L_out + gamma`
 * into the future, and at expiry sends an L_out-token response burst back
 * to the source GS using the LLMPacketTag with direction=RESPONSE.
 *
 * GS IP resolution: the scheduler installs ComputeApplication and sets
 * a `GsIpLookup` callback that maps src_node_id -> Ipv4Address by walking
 * the topology's NodeContainer once at scheduler-construction time.
 *
 * Logs to `compute_log.csv` on the hot path using integer streaming
 * (no fmt/printf).
 */
#ifndef COMPUTE_APPLICATION_H
#define COMPUTE_APPLICATION_H

#include <fstream>
#include <queue>
#include <string>

#include "ns3/application.h"
#include "ns3/callback.h"
#include "ns3/ipv4-address.h"
#include "ns3/socket.h"

namespace ns3 {

class ComputeApplication : public Application
{
public:
    static TypeId GetTypeId(void);

    ComputeApplication();
    ~ComputeApplication() override;

    // Resolve a GS node id -> IPv4 address. Scheduler captures the
    // topology's NodeContainer once and provides the closure.
    typedef Callback<Ipv4Address, uint32_t> GsIpLookup;
    void SetGsIpLookup(GsIpLookup cb) { m_gs_ip_lookup = cb; }

    // Public entry point used by GatherApplication when a request has
    // been fully gathered.
    void OnGatherComplete(uint64_t req_id,
                          uint32_t L_in,
                          uint32_t L_out_expected,
                          uint32_t src_node_id,
                          uint64_t t_emit_request_ns);

    uint64_t GetQueueDepth() const { return m_queue.size(); }
    uint64_t GetCompleteCount() const { return m_complete_count; }

protected:
    void DoDispose() override;

private:
    void StartApplication() override;
    void StopApplication() override;

    void StartNextCompute();
    void OnComputeComplete();
    void SendResponse(uint64_t req_id, uint32_t L_in, uint32_t L_out,
                      uint32_t src_node_id, uint64_t t_emit_request_ns);

    struct ComputeRequest {
        uint64_t req_id;
        uint32_t L_in;
        uint32_t L_out;
        uint32_t src_node_id;
        uint64_t t_emit_request_ns;
        uint64_t t_queue_enter_ns;
    };

    // ---- attributes ----
    uint64_t m_alpha_ns_per_input_tok;
    uint64_t m_beta_ns_per_output_tok;
    uint64_t m_gamma_ns;
    uint16_t m_response_dest_port;
    uint32_t m_packet_payload;
    uint32_t m_bytes_per_token;
    std::string m_log_filename;

    // ---- runtime state ----
    Ptr<Socket>                 m_socket;
    std::queue<ComputeRequest>  m_queue;
    bool                        m_busy;
    ComputeRequest              m_in_service;
    uint64_t                    m_in_service_start_ns;
    std::ofstream               m_log;
    GsIpLookup                  m_gs_ip_lookup;
    uint64_t                    m_complete_count;
    uint64_t                    m_response_tx_pkt_count;
};

} // namespace ns3

#endif // COMPUTE_APPLICATION_H