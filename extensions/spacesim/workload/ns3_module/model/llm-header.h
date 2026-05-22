/*
 * LLMHeader — in-payload, fixed-size 24-byte struct carried as the very
 * first bytes of every LLM TCP stream (both directions).
 *
 * Replaces the v1 LLMPacketTag (which was an ns-3 Packet Tag attached to
 * every UDP datagram). With TCP we don't have packet boundaries to hang
 * a Tag on — we have a byte stream — so request/response metadata lives
 * in-band as a tiny preamble:
 *
 *   bytes 0..7    t_emit_ns       uint64_t   GS-side wall-clock emit time
 *   bytes 8..11   req_id          uint32_t   GS-local monotonic counter
 *   bytes 12..15  src_node_id     uint32_t   originating GS ns-3 node id
 *   bytes 16..19  L_in            uint32_t   prompt tokens
 *   bytes 20..23  reserved        uint32_t   set to 0; future use
 *
 * Little-endian on the wire (ns-3 hosts are LE, and we don't pretend to
 * be wire-compatible with anything outside this module).
 */
#ifndef SPACESIM_LLM_HEADER_H
#define SPACESIM_LLM_HEADER_H

#include <cstdint>
#include <cstring>

namespace ns3 {

struct LLMHeader
{
    static constexpr std::size_t SIZE_BYTES = 24;

    uint64_t t_emit_ns = 0;
    uint32_t req_id = 0;
    uint32_t src_node_id = 0;
    uint32_t L_in = 0;
    uint32_t reserved = 0;

    // Pack into the given buffer (must have at least SIZE_BYTES). Returns
    // SIZE_BYTES. Little-endian, no padding.
    std::size_t Pack(uint8_t *buf) const
    {
        std::memcpy(buf + 0,  &t_emit_ns,   sizeof(t_emit_ns));
        std::memcpy(buf + 8,  &req_id,      sizeof(req_id));
        std::memcpy(buf + 12, &src_node_id, sizeof(src_node_id));
        std::memcpy(buf + 16, &L_in,        sizeof(L_in));
        std::memcpy(buf + 20, &reserved,    sizeof(reserved));
        return SIZE_BYTES;
    }

    // Unpack from buffer (must have at least SIZE_BYTES). Returns SIZE_BYTES.
    std::size_t Unpack(const uint8_t *buf)
    {
        std::memcpy(&t_emit_ns,   buf + 0,  sizeof(t_emit_ns));
        std::memcpy(&req_id,      buf + 8,  sizeof(req_id));
        std::memcpy(&src_node_id, buf + 12, sizeof(src_node_id));
        std::memcpy(&L_in,        buf + 16, sizeof(L_in));
        std::memcpy(&reserved,    buf + 20, sizeof(reserved));
        return SIZE_BYTES;
    }
};

} // namespace ns3

#endif // SPACESIM_LLM_HEADER_H
