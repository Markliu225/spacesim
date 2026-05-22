/*
 * ComputeApplication — TCP, compute-SAT side.
 *
 * Receives "gather complete" callbacks from a co-located GatherApplication.
 * Per callback we receive:
 *
 *   (req_id, L_in, src_node_id, t_emit_request_ns, socket)
 *
 * The socket is the *same* TCP connection the GS opened for this
 * request; we will eventually write the response payload onto it and
 * ShutdownSend.
 *
 * Option B semantics: this application samples L_out itself from a
 * Normal(LOutMean, LOutStd) clipped to [LOutMin, LOutMax]. The GS does
 * not send a hint. T_compute = α·L_in + β·L_out + γ.
 *
 * Single-server FIFO queue (matches v1). When the in-service request
 * is done computing we write the compute log row and send the response.
 */
#ifndef SPACESIM_COMPUTE_APPLICATION_H
#define SPACESIM_COMPUTE_APPLICATION_H

#include <fstream>
#include <map>
#include <queue>
#include <string>

#include "ns3/application.h"
#include "ns3/callback.h"
#include "ns3/random-variable-stream.h"
#include "ns3/socket.h"

namespace ns3 {

class ComputeApplication : public Application
{
public:
    static TypeId GetTypeId(void);
    ComputeApplication();
    ~ComputeApplication() override;

    void OnGatherComplete(uint64_t req_id,
                          uint32_t L_in,
                          uint32_t src_node_id,
                          uint64_t t_emit_request_ns,
                          Ptr<Socket> socket);

    uint64_t GetQueueDepth() const { return m_queue.size(); }
    uint64_t GetCompleteCount() const { return m_complete_count; }

protected:
    void DoDispose() override;

private:
    void StartApplication() override;
    void StopApplication() override;

    void StartNextCompute();
    void OnComputeComplete();
    void SendResponse();
    bool PumpResponse(Ptr<Socket> sock);

    void OnRespDataSent(Ptr<Socket> sock, uint32_t txAvail);
    void OnRespError(Ptr<Socket> sock);

    struct ComputeRequest {
        uint64_t  req_id;
        uint32_t  L_in;
        uint32_t  L_out;
        uint32_t  src_node_id;
        uint64_t  t_emit_request_ns;
        uint64_t  t_queue_enter_ns;
        Ptr<Socket> sock;
    };

    struct RespState {
        uint64_t total_bytes;
        uint64_t bytes_sent;
        bool     finished_send;
    };

    // ---- attributes ----
    uint64_t m_alpha_ns_per_input_tok;
    uint64_t m_beta_ns_per_output_tok;
    uint64_t m_gamma_ns;
    uint32_t m_bytes_per_token;
    double   m_L_out_mean;
    double   m_L_out_std;
    uint32_t m_L_out_min;
    uint32_t m_L_out_max;
    std::string m_log_filename;

    // ---- runtime state ----
    std::queue<ComputeRequest>   m_queue;
    bool                         m_busy;
    ComputeRequest               m_in_service;
    uint64_t                     m_in_service_start_ns;
    std::ofstream                m_log;
    Ptr<NormalRandomVariable>    m_L_out_rv;
    std::map<Ptr<Socket>, RespState> m_resp;
    uint64_t                     m_complete_count;
};

} // namespace ns3

#endif // SPACESIM_COMPUTE_APPLICATION_H
