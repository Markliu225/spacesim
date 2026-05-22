/*
 * llm-workload test suite (v2 — TCP only).
 *
 * Currently covers the LLMHeader pack/unpack round-trip — the one
 * piece of fixed-format wire encoding that, if it breaks, silently
 * corrupts every gather. The end-to-end socket + Application paths
 * are exercised by examples/llm-workload-example.cc and by the
 * integration scenarios.
 */
#include "ns3/test.h"
#include "ns3/llm-header.h"

using namespace ns3;

class LLMHeaderRoundTrip : public TestCase
{
public:
    LLMHeaderRoundTrip()
        : TestCase("LLMHeader pack/unpack roundtrip")
    {}

private:
    void DoRun() override
    {
        LLMHeader a;
        a.t_emit_ns   = 1234567890ULL;
        a.req_id      = 42;
        a.src_node_id = 1584;
        a.L_in        = 500;
        a.reserved    = 0;

        uint8_t buf[LLMHeader::SIZE_BYTES];
        std::size_t n = a.Pack(buf);
        NS_TEST_ASSERT_MSG_EQ(n, LLMHeader::SIZE_BYTES, "Pack returns SIZE_BYTES");

        LLMHeader b;
        std::size_t m = b.Unpack(buf);
        NS_TEST_ASSERT_MSG_EQ(m, LLMHeader::SIZE_BYTES, "Unpack returns SIZE_BYTES");
        NS_TEST_ASSERT_MSG_EQ(b.t_emit_ns,   a.t_emit_ns,   "t_emit_ns");
        NS_TEST_ASSERT_MSG_EQ(b.req_id,      a.req_id,      "req_id");
        NS_TEST_ASSERT_MSG_EQ(b.src_node_id, a.src_node_id, "src_node_id");
        NS_TEST_ASSERT_MSG_EQ(b.L_in,        a.L_in,        "L_in");
        NS_TEST_ASSERT_MSG_EQ(b.reserved,    a.reserved,    "reserved");
    }
};

class LlmWorkloadTestSuite : public TestSuite
{
public:
    LlmWorkloadTestSuite() : TestSuite("llm-workload", UNIT)
    {
        AddTestCase(new LLMHeaderRoundTrip, TestCase::QUICK);
    }
};

static LlmWorkloadTestSuite g_llmWorkloadTestSuite;
