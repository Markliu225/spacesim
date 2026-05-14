/*
 * LLMRequestApplication — Phase B.
 *
 * Installed on a ground-station node. While alive, it generates LLM
 * requests according to a Poisson arrival process (ExponentialRandomVariable
 * with mean = 1 / lambda). For each request, the prompt-token count L_in
 * is sampled from a clipped Normal(mu, sigma) ∩ [L_in_min, L_in_max].
 * The request is then sliced into N_pkt = ceil(L_in * bytes_per_token /
 * packet_payload) UDP packets, each carrying an LLMPacketTag with the
 * request and packet identifiers.
 *
 * Designed to be standalone-testable: see
 * examples/llm-workload-example.cc for a two-node P2P minimal harness.
 */
#ifndef LLM_REQUEST_APPLICATION_H
#define LLM_REQUEST_APPLICATION_H

#include "ns3/application.h"
#include "ns3/address.h"
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

    void ScheduleNext();
    void EmitRequest();

    // ---- configuration attributes ----
    Address   m_dst_addr;       // includes port
    uint16_t  m_dst_port;
    double    m_lambda;         // requests per second
    double    m_L_in_mean;
    double    m_L_in_std;
    uint32_t  m_L_in_min;
    uint32_t  m_L_in_max;
    uint32_t  m_bytes_per_token;
    uint32_t  m_packet_payload;

    // ---- runtime state ----
    Ptr<Socket>                       m_socket;
    Ptr<ExponentialRandomVariable>    m_iat_rv;     // inter-arrival time
    Ptr<NormalRandomVariable>         m_L_in_rv;
    EventId                           m_next_event;
    uint64_t                          m_req_counter;
    uint64_t                          m_tx_pkt_count;
};

} // namespace ns3

#endif // LLM_REQUEST_APPLICATION_H