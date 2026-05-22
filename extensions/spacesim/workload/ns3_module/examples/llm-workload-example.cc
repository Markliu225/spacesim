/*
 * llm-workload-example — minimal two-node TCP LLM workload harness.
 *
 * Two ns-3 nodes joined by a 10 Mbps / 5 ms point-to-point link.
 * Node 0 = "GS" running LLMRequestApplication. Node 1 = "compute SAT"
 * running GatherApplication + ComputeApplication.
 *
 * Verifies the module's per-request lifecycle (TCP connect → header
 * → prompt payload → compute → response payload → close) without any
 * satellite topology / fstate / routing infrastructure around it.
 *
 * Logs land in /tmp/llm-example-{gather,compute,stuck,response}.csv.
 *
 * Run:
 *   ./waf --run "llm-workload-example"
 *   ls /tmp/llm-example-*.csv
 */
#include "ns3/core-module.h"
#include "ns3/network-module.h"
#include "ns3/internet-module.h"
#include "ns3/point-to-point-module.h"
#include "ns3/applications-module.h"

#include "ns3/llm-request-application.h"
#include "ns3/llm-request-helper.h"
#include "ns3/gather-application.h"
#include "ns3/gather-helper.h"
#include "ns3/compute-application.h"
#include "ns3/compute-helper.h"

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

    // SAT-side: gather + compute.
    ComputeHelper ch;
    ch.SetAttribute("AlphaNsPerInputToken", UintegerValue(100000));
    ch.SetAttribute("BetaNsPerOutputToken", UintegerValue(50000));
    ch.SetAttribute("GammaNs",              UintegerValue(10000000));
    ch.SetAttribute("BytesPerToken",        UintegerValue(4));
    ch.SetAttribute("LOutMean",             DoubleValue(50.0));
    ch.SetAttribute("LOutStd",              DoubleValue(10.0));
    ch.SetAttribute("LOutMin",              UintegerValue(1));
    ch.SetAttribute("LOutMax",              UintegerValue(200));
    ch.SetAttribute("LogFilename", StringValue("/tmp/llm-example-compute.csv"));
    ApplicationContainer comp = ch.Install(nodes.Get(1));
    Ptr<ComputeApplication> compute_app =
        DynamicCast<ComputeApplication>(comp.Get(0));
    compute_app->SetStartTime(Seconds(0.0));
    compute_app->SetStopTime(Seconds(5.0));

    GatherHelper gh;
    gh.SetAttribute("Port",             UintegerValue(9999));
    gh.SetAttribute("BytesPerToken",    UintegerValue(4));
    gh.SetAttribute("LogFilename",      StringValue("/tmp/llm-example-gather.csv"));
    gh.SetAttribute("StuckLogFilename", StringValue("/tmp/llm-example-stuck.csv"));
    ApplicationContainer gatherc = gh.Install(nodes.Get(1));
    Ptr<GatherApplication> gather_app =
        DynamicCast<GatherApplication>(gatherc.Get(0));
    gather_app->SetGatherCompleteCallback(
        MakeCallback(&ComputeApplication::OnGatherComplete, compute_app));
    gather_app->SetStartTime(Seconds(0.0));
    gather_app->SetStopTime(Seconds(5.0));

    // GS-side: request app.
    Ipv4Address dst_ip = ifs.GetAddress(1);
    LLMRequestHelper rh(dst_ip, 9999);
    rh.SetAttribute("Lambda",   DoubleValue(5.0));
    rh.SetAttribute("LInMean",  DoubleValue(100.0));
    rh.SetAttribute("LInStd",   DoubleValue(20.0));
    rh.SetAttribute("LInMin",   UintegerValue(1));
    rh.SetAttribute("LInMax",   UintegerValue(500));
    rh.SetAttribute("BytesPerToken", UintegerValue(4));
    rh.SetAttribute("ResponseLogFilename",
                    StringValue("/tmp/llm-example-response.csv"));
    ApplicationContainer reqc = rh.Install(nodes.Get(0));
    reqc.Start(Seconds(0.1));
    reqc.Stop(Seconds(3.0));

    Simulator::Stop(Seconds(5.0));
    Simulator::Run();
    Simulator::Destroy();
    return 0;
}
