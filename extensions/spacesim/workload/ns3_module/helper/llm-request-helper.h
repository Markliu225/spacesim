#ifndef SPACESIM_LLM_REQUEST_HELPER_H
#define SPACESIM_LLM_REQUEST_HELPER_H

#include "ns3/object-factory.h"
#include "ns3/application-container.h"
#include "ns3/node.h"
#include "ns3/ipv4-address.h"

#include "ns3/llm-request-application.h"

namespace ns3 {

class LLMRequestHelper
{
public:
    LLMRequestHelper(Ipv4Address dst_ip, uint16_t dst_port);
    void SetAttribute(const std::string &name, const AttributeValue &value);
    ApplicationContainer Install(Ptr<Node> node) const;

private:
    ObjectFactory m_factory;
};

} // namespace ns3

#endif
