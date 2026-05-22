/*
 * LLMSinkApplication — Phase B.
 *
 * UDP server installed on a compute satellite. For every packet received,
 * it peeks the LLMPacketTag and appends one CSV row to its log file:
 *
 *   recv_time_ns,req_id,packet_id,total_pkts,t_emit_ns,src_node_id,L_in,L_out_expected,recv_node_id
 *
 * The sink does no formatting on the hot path: each field is a fixed-size
 * integer written via `<< ',' <<` to a std::ofstream opened once at
 * StartApplication and flushed at StopApplication.
 *
 * Each LLMSinkApplication writes to its own log file; if multiple sinks
 * are installed on different nodes they should be configured with
 * distinct LogFilename attributes (the scheduler does this).
 */
#ifndef LLM_SINK_APPLICATION_H
#define LLM_SINK_APPLICATION_H

#include <fstream>
#include <string>

#include "ns3/application.h"
#include "ns3/socket.h"

namespace ns3 {

class LLMSinkApplication : public Application
{
public:
    static TypeId GetTypeId(void);

    LLMSinkApplication();
    ~LLMSinkApplication() override;

    uint64_t GetRxPacketCount() const { return m_rx_pkt_count; }

protected:
    void DoDispose() override;

private:
    void StartApplication() override;
    void StopApplication() override;

    // ns-3 socket recv callback.
    void HandleRead(Ptr<Socket> socket);

    uint16_t       m_port;
    std::string    m_log_filename;
    Ptr<Socket>    m_socket;
    std::ofstream  m_log;
    uint64_t       m_rx_pkt_count;
};

} // namespace ns3

#endif // LLM_SINK_APPLICATION_H