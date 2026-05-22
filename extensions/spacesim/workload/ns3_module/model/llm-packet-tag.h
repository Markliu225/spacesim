/*
 * LLMPacketTag — extended in Phase C with a direction field.
 *
 * Carried on every UDP packet emitted by LLMRequestApplication (REQUEST
 * direction, GS -> compute SAT) or by ComputeApplication's response burst
 * (RESPONSE direction, compute SAT -> GS).
 *
 * Serialized size: 8 + 2 + 2 + 8 + 4 + 4 + 4 + 1 = 33 bytes
 * (Phase B was 32; Phase C adds one byte for `direction`. The task spec
 * said "29" which is a pre-Phase-B miscount — the actual Phase B size
 * has always been 32.)
 */
#ifndef LLM_PACKET_TAG_H
#define LLM_PACKET_TAG_H

#include "ns3/tag.h"

namespace ns3 {

class LLMPacketTag : public Tag
{
public:
    enum Direction : uint8_t {
        REQUEST  = 0,
        RESPONSE = 1,
    };

    LLMPacketTag();
    LLMPacketTag(uint64_t req_id,
                 uint16_t packet_id,
                 uint16_t total_pkts,
                 uint64_t t_emit_ns,
                 uint32_t src_node_id,
                 uint32_t L_in,
                 uint32_t L_out_expected,
                 uint8_t  direction = REQUEST);

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
    uint8_t  GetDirection() const      { return m_direction; }

    void SetReqId(uint64_t v)           { m_req_id = v; }
    void SetPacketId(uint16_t v)        { m_packet_id = v; }
    void SetTotalPkts(uint16_t v)       { m_total_pkts = v; }
    void SetTEmitNs(uint64_t v)         { m_t_emit_ns = v; }
    void SetSrcNodeId(uint32_t v)       { m_src_node_id = v; }
    void SetLIn(uint32_t v)             { m_L_in = v; }
    void SetLOutExpected(uint32_t v)    { m_L_out_expected = v; }
    void SetDirection(uint8_t v)        { m_direction = v; }

private:
    uint64_t m_req_id;
    uint16_t m_packet_id;
    uint16_t m_total_pkts;
    uint64_t m_t_emit_ns;
    uint32_t m_src_node_id;
    uint32_t m_L_in;
    uint32_t m_L_out_expected;
    uint8_t  m_direction;
};

} // namespace ns3

#endif // LLM_PACKET_TAG_H