#ifndef LLM_RESPONSE_SINK_HELPER_H
#define LLM_RESPONSE_SINK_HELPER_H

#include "ns3/application-container.h"
#include "ns3/node.h"
#include "ns3/object-factory.h"

namespace ns3 {

class LLMResponseSinkHelper
{
public:
    LLMResponseSinkHelper();
    void SetAttribute(const std::string &name, const AttributeValue &value);
    ApplicationContainer Install(Ptr<Node> node) const;
private:
    ObjectFactory m_factory;
};

} // namespace ns3
#endif