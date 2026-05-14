/*
 * Tiny C++ test suite for llm-workload.
 *
 * Currently covers the LLMPacketTag round-trip (Serialize -> Deserialize
 * recovers the original fields). The end-to-end socket + Application
 * paths are exercised by the example and by Phase B's full run.
 */

#include "ns3/test.h"
#include "ns3/llm-packet-tag.h"

using namespace ns3;

class LLMPacketTagRoundTrip : public TestCase {
public:
    LLMPacketTagRoundTrip() : TestCase("LLMPacketTag Serialize/Deserialize roundtrip") {}
private:
    void DoRun() override {
        LLMPacketTag a(123456789ULL, 7, 42, 1234567890ULL,
                       1584, 500, 0);
        NS_TEST_ASSERT_MSG_EQ(a.GetSerializedSize(), 32u,
                              "Serialized size must be 32 bytes");

        // Round-trip via a 32-byte buffer.
        uint8_t buf[32] = {};
        TagBuffer tb_w(buf, buf + 32);
        a.Serialize(tb_w);
        TagBuffer tb_r(buf, buf + 32);
        LLMPacketTag b;
        b.Deserialize(tb_r);

        NS_TEST_ASSERT_MSG_EQ(b.GetReqId(),         a.GetReqId(),         "req_id");
        NS_TEST_ASSERT_MSG_EQ(b.GetPacketId(),      a.GetPacketId(),      "packet_id");
        NS_TEST_ASSERT_MSG_EQ(b.GetTotalPkts(),     a.GetTotalPkts(),     "total_pkts");
        NS_TEST_ASSERT_MSG_EQ(b.GetTEmitNs(),       a.GetTEmitNs(),       "t_emit_ns");
        NS_TEST_ASSERT_MSG_EQ(b.GetSrcNodeId(),     a.GetSrcNodeId(),     "src_node_id");
        NS_TEST_ASSERT_MSG_EQ(b.GetLIn(),           a.GetLIn(),           "L_in");
        NS_TEST_ASSERT_MSG_EQ(b.GetLOutExpected(),  a.GetLOutExpected(),  "L_out_expected");
    }
};

class LlmWorkloadTestSuite : public TestSuite {
public:
    LlmWorkloadTestSuite() : TestSuite("llm-workload", UNIT) {
        AddTestCase(new LLMPacketTagRoundTrip(), TestCase::QUICK);
    }
};

static LlmWorkloadTestSuite g_llm_workload_test_suite;