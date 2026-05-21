/*
 * LLMResponseSinkApplication — Phase C.
 *
 * Installed on ground stations. Listens on UDP port (default 19999) for
 * RESPONSE packets coming back from the compute satellite and logs each
 * arrival to response_log.csv. Stateless: req_id correlation is done
 * offline by joining response_log with gather_log and compute_log.
 */
#ifndef LLM_RESPONSE_SINK_APPLICATION_H
#define LLM_RESPONSE_SINK_APPLICATION_H

#include <fstream>
#include <string>

#include "ns3/application.h"
#include "ns3/socket.h"

namespace ns3 {

class LLMResponseSinkApplication : public Application
{
public:
    static TypeId GetTypeId(void);
    LLMResponseSinkApplication();
    ~LLMResponseSinkApplication() override;

    uint64_t GetRxPacketCount() const { return m_rx_pkt_count; }

protected:
    void DoDispose() override;

private:
    void StartApplication() override;
    void StopApplication() override;
    void HandleRead(Ptr<Socket> socket);

    uint16_t      m_port;
    std::string   m_log_filename;
    Ptr<Socket>   m_socket;
    std::ofstream m_log;
    uint64_t      m_rx_pkt_count;
};

} // namespace ns3

#endif // LLM_RESPONSE_SINK_APPLICATION_H