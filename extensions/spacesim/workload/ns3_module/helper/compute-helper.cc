#include "ns3/compute-helper.h"
#include "ns3/compute-application.h"

namespace ns3 {

ComputeHelper::ComputeHelper()
{
    m_factory.SetTypeId(ComputeApplication::GetTypeId());
}

void
ComputeHelper::SetAttribute(const std::string &name, const AttributeValue &v)
{
    m_factory.Set(name, v);
}

ApplicationContainer
ComputeHelper::Install(Ptr<Node> node) const
{
    Ptr<Application> app = m_factory.Create<Application>();
    node->AddApplication(app);
    return ApplicationContainer(app);
}

} // namespace ns3