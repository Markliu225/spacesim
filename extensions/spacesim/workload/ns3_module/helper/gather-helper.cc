#include "ns3/gather-helper.h"

namespace ns3 {

GatherHelper::GatherHelper()
{
    m_factory.SetTypeId(GatherApplication::GetTypeId());
}

void
GatherHelper::SetAttribute(const std::string &name, const AttributeValue &value)
{
    m_factory.Set(name, value);
}

ApplicationContainer
GatherHelper::Install(Ptr<Node> node) const
{
    Ptr<Application> app = m_factory.Create<Application>();
    node->AddApplication(app);
    return ApplicationContainer(app);
}

} // namespace ns3
