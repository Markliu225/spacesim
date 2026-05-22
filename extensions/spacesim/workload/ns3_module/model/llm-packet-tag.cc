#include "ns3/llm-packet-tag.h"

namespace ns3 {

NS_OBJECT_ENSURE_REGISTERED(LLMPacketTag);

LLMPacketTag::LLMPacketTag()
    : m_req_id(0), m_packet_id(0), m_total_pkts(0),
      m_t_emit_ns(0), m_src_node_id(0), m_L_in(0), m_L_out_expected(0),
      m_direction(REQUEST)
{
}

LLMPacketTag::LLMPacketTag(uint64_t req_id,
                           uint16_t packet_id,
                           uint16_t total_pkts,
                           uint64_t t_emit_ns,
                           uint32_t src_node_id,
                           uint32_t L_in,
                           uint32_t L_out_expected,
                           uint8_t  direction)
    : m_req_id(req_id),
      m_packet_id(packet_id),
      m_total_pkts(total_pkts),
      m_t_emit_ns(t_emit_ns),
      m_src_node_id(src_node_id),
      m_L_in(L_in),
      m_L_out_expected(L_out_expected),
      m_direction(direction)
{
}

TypeId
LLMPacketTag::GetTypeId(void)
{
    static TypeId tid = TypeId("ns3::LLMPacketTag")
        .SetParent<Tag>()
        .SetGroupName("LlmWorkload")
        .AddConstructor<LLMPacketTag>();
    return tid;
}

TypeId
LLMPacketTag::GetInstanceTypeId(void) const
{
    return GetTypeId();
}

uint32_t
LLMPacketTag::GetSerializedSize(void) const
{
    return 33;
}

void
LLMPacketTag::Serialize(TagBuffer i) const
{
    i.WriteU64(m_req_id);
    i.WriteU16(m_packet_id);
    i.WriteU16(m_total_pkts);
    i.WriteU64(m_t_emit_ns);
    i.WriteU32(m_src_node_id);
    i.WriteU32(m_L_in);
    i.WriteU32(m_L_out_expected);
    i.WriteU8 (m_direction);
}

void
LLMPacketTag::Deserialize(TagBuffer i)
{
    m_req_id         = i.ReadU64();
    m_packet_id      = i.ReadU16();
    m_total_pkts     = i.ReadU16();
    m_t_emit_ns      = i.ReadU64();
    m_src_node_id    = i.ReadU32();
    m_L_in           = i.ReadU32();
    m_L_out_expected = i.ReadU32();
    m_direction      = i.ReadU8();
}

void
LLMPacketTag::Print(std::ostream &os) const
{
    os << "LLMTag(req=" << m_req_id
       << " pkt=" << m_packet_id << "/" << m_total_pkts
       << " t_emit_ns=" << m_t_emit_ns
       << " src=" << m_src_node_id
       << " L_in=" << m_L_in
       << " L_out_exp=" << m_L_out_expected
       << " dir=" << (m_direction == RESPONSE ? "RESP" : "REQ")
       << ")";
}

} // namespace ns3