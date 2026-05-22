/*
 * llm-workload-example
 *
 * Two-node point-to-point link, LLMRequestApplication on node 0
 * generating Poisson-arrival LLM requests for 2 simulated seconds,
 * LLMSinkApplication on node 1 receiving them and logging to
 * /tmp/llm-workload-example-sink.csv.
 *
 * Run via:
 *   ./waf --run "llm-workload-example"
 *   tail /tmp/llm-workload-example-sink.csv
 *
 * Purpose: lets us validate the module in complete isolation from the
 * Hypatia satellite topology. If the example doesn't write packets,
 * something is wrong inside the module itself.
 */

#include "ns3/core-module.h"
#include "ns3/network-module.h"
#include "ns3/internet-module.h"
#include "ns3/point-to-point-module.h"
#include "ns3/applications-module.h"

#include "ns3/llm-request-application.h"
#include "ns3/llm-sink-application.h"
#include "ns3/llm-request-helper.h"
#include "ns3/llm-sink-helper.h"

using namespace ns3;

int main(int argc, char **argv)
{
    CommandLine cmd;
    cmd.Parse(argc, argv);

    NodeContainer nodes;
    nodes.Create(2);

    PointToPointHelper p2p;
    p2p.SetDeviceAttribute("DataRate", StringValue("10Mbps"));
    p2p.SetChannelAttribute("Delay",   StringValue("5ms"));
    NetDeviceContainer devs = p2p.Install(nodes);

    InternetStackHelper stack;
    stack.Install(nodes);

    Ipv4AddressHelper ip;
    ip.SetBase("10.1.1.0", "255.255.255.0");
    Ipv4InterfaceContainer ifs = ip.Assign(devs);

    Ipv4Address dst_ip = ifs.GetAddress(1);
    uint16_t port = 9999;

    // Install sink on node 1.
    LLMSinkHelper sink_helper(port, "/tmp/llm-workload-example-sink.csv");
    ApplicationContainer sink_apps = sink_helper.Install(nodes.Get(1));
    sink_apps.Start(Seconds(0.0));
    sink_apps.Stop(Seconds(2.0));

    // Install request on node 0.
    LLMRequestHelper req_helper(dst_ip, port);
    req_helper.SetAttribute("Lambda",        DoubleValue(10.0));
    req_helper.SetAttribute("LInMean",       DoubleValue(500.0));
    req_helper.SetAttribute("LInStd",        DoubleValue(100.0));
    req_helper.SetAttribute("LInMin",        UintegerValue(1));
    req_helper.SetAttribute("LInMax",        UintegerValue(2000));
    req_helper.SetAttribute("BytesPerToken", UintegerValue(4));
    req_helper.SetAttribute("PacketPayload", UintegerValue(1400));
    ApplicationContainer req_apps = req_helper.Install(nodes.Get(0));
    req_apps.Start(Seconds(0.1));
    req_apps.Stop(Seconds(2.0));

    Simulator::Stop(Seconds(2.5));
    Simulator::Run();

    Ptr<LLMRequestApplication> req =
        DynamicCast<LLMRequestApplication>(req_apps.Get(0));
    Ptr<LLMSinkApplication> sink =
        DynamicCast<LLMSinkApplication>(sink_apps.Get(0));
    std::cout << "tx_requests = " << req->GetTxRequestCount() << std::endl;
    std::cout << "tx_packets  = " << req->GetTxPacketCount()  << std::endl;
    std::cout << "rx_packets  = " << sink->GetRxPacketCount() << std::endl;

    Simulator::Destroy();
    return 0;
}