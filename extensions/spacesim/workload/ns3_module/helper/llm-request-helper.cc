#include "ns3/llm-request-helper.h"
#include "ns3/address.h"
#include "ns3/uinteger.h"

namespace ns3 {

LLMRequestHelper::LLMRequestHelper(Ipv4Address dst_ip, uint16_t dst_port)
{
    m_factory.SetTypeId(LLMRequestApplication::GetTypeId());
    m_factory.Set("DestAddress", AddressValue(Address(dst_ip)));
    m_factory.Set("DestPort",    UintegerValue(dst_port));
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
