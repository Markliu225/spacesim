/*
 * LLMPacketTag — Phase B of the LLM-on-satellite project.
 *
 * Attached to every UDP packet emitted by LLMRequestApplication. Carries
 * the (request, packet) identifiers and a snapshot of the LLM workload
 * metadata that the sink (and later phases' gather / compute / response
 * logic) need.
 *
 * Why a Tag and not a Header: tags are not serialized into the on-wire
 * bytes -- they ride alongside the packet inside ns-3 only. We do not
 * want to inflate the modeled payload with metadata that wouldn't exist
 * in a real deployment (the real protocol would use, e.g., an HTTP body
 * or a binary RPC header).
 *
 * Serialized size: 8 + 2 + 2 + 8 + 4 + 4 + 4 = 32 bytes.
 */
#ifndef LLM_PACKET_TAG_H
#define LLM_PACKET_TAG_H

#include "ns3/tag.h"

namespace ns3 {

class LLMPacketTag : public Tag
{
public:
    LLMPacketTag();
    LLMPacketTag(uint64_t req_id,
                 uint16_t packet_id,
                 uint16_t total_pkts,
                 uint64_t t_emit_ns,
                 uint32_t src_node_id,
                 uint32_t L_in,
                 uint32_t L_out_expected);

    static TypeId GetTypeId(void);
    virtual TypeId GetInstanceTypeId(void) const override;

    virtual uint32_t GetSerializedSize(void) const override;
    virtual void Serialize(TagBuffer i) const override;
    virtual void Deserialize(TagBuffer i) override;
    virtual void Print(std::ostream &os) const override;

    uint64_t GetReqId() const          { return m_req_id; }
    uint16_t GetPacketId() const       { return m_packet_id; }
    uint16_t GetTotalPkts() const      { return m_total_pkts; }
    uint64_t GetTEmitNs() const        { return m_t_emit_ns; }
    uint32_t GetSrcNodeId() const      { return m_src_node_id; }
    uint32_t GetLIn() const            { return m_L_in; }
    uint32_t GetLOutExpected() const   { return m_L_out_expected; }

private:
    uint64_t m_req_id;          // 8 bytes -- unique per (src node, request)
    uint16_t m_packet_id;       // 2       -- 0-based index within this request
    uint16_t m_total_pkts;      // 2       -- ceil(L_in * bpt / payload)
    uint64_t m_t_emit_ns;       // 8       -- Simulator::Now() at packet emission
    uint32_t m_src_node_id;     // 4       -- ns-3 node id that emitted this packet
    uint32_t m_L_in;            // 4       -- prompt token count, drawn from N(mu, sd)
    uint32_t m_L_out_expected;  // 4       -- placeholder for Phase C decode model
};

} // namespace ns3

#endif // LLM_PACKET_TAG_H