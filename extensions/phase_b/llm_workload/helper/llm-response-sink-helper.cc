#include "ns3/llm-response-sink-helper.h"
#include "ns3/llm-response-sink-application.h"

namespace ns3 {

LLMResponseSinkHelper::LLMResponseSinkHelper()
{
    m_factory.SetTypeId(LLMResponseSinkApplication::GetTypeId());
}

void
LLMResponseSinkHelper::SetAttribute(const std::string &name, const AttributeValue &v)
{
    m_factory.Set(name, v);
}

ApplicationContainer
LLMResponseSinkHelper::Install(Ptr<Node> node) const
{
    Ptr<Application> app = m_factory.Create<Application>();
    node->AddApplication(app);
    return ApplicationContainer(app);
}

} // namespace ns3