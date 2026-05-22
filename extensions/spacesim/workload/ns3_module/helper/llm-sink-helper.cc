#include "ns3/llm-sink-helper.h"

#include "ns3/string.h"
#include "ns3/uinteger.h"

#include "ns3/llm-sink-application.h"

namespace ns3 {

LLMSinkHelper::LLMSinkHelper(uint16_t port, const std::string &log_filename)
{
    m_factory.SetTypeId(LLMSinkApplication::GetTypeId());
    m_factory.Set("Port",        UintegerValue(port));
    m_factory.Set("LogFilename", StringValue(log_filename));
}

void
LLMSinkHelper::SetAttribute(const std::string &name, const AttributeValue &value)
{
    m_factory.Set(name, value);
}

ApplicationContainer
LLMSinkHelper::Install(Ptr<Node> node) const
{
    Ptr<Application> app = m_factory.Create<Application>();
    node->AddApplication(app);
    return ApplicationContainer(app);
}

ApplicationContainer
LLMSinkHelper::Install(const NodeContainer &nodes) const
{
    ApplicationContainer apps;
    for (uint32_t i = 0; i < nodes.GetN(); ++i) {
        apps.Add(Install(nodes.Get(i)));
    }
    return apps;
}

} // namespace ns3