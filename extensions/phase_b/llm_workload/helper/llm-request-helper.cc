#include "ns3/llm-request-helper.h"

#include "ns3/uinteger.h"

#include "ns3/llm-request-application.h"

namespace ns3 {

LLMRequestHelper::LLMRequestHelper(Address dest_addr, uint16_t dest_port)
{
    m_factory.SetTypeId(LLMRequestApplication::GetTypeId());
    m_factory.Set("DestAddress", AddressValue(dest_addr));
    m_factory.Set("DestPort",    UintegerValue(dest_port));
}

void
LLMRequestHelper::SetAttribute(const std::string &name, const AttributeValue &value)
{
    m_factory.Set(name, value);
}

ApplicationContainer
LLMRequestHelper::Install(Ptr<Node> node) const
{
    Ptr<Application> app = m_factory.Create<Application>();
    node->AddApplication(app);
    return ApplicationContainer(app);
}

} // namespace ns3