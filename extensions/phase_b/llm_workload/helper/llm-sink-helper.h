/*
 * LLMSinkHelper — thin install-on-node helper for LLMSinkApplication.
 */
#ifndef LLM_SINK_HELPER_H
#define LLM_SINK_HELPER_H

#include <string>

#include "ns3/application-container.h"
#include "ns3/node-container.h"
#include "ns3/object-factory.h"

namespace ns3 {

class LLMSinkHelper
{
public:
    LLMSinkHelper(uint16_t port, const std::string &log_filename);
    void SetAttribute(const std::string &name, const AttributeValue &value);

    ApplicationContainer Install(Ptr<Node> node) const;
    ApplicationContainer Install(const NodeContainer &nodes) const;

private:
    ObjectFactory m_factory;
};

} // namespace ns3

#endif // LLM_SINK_HELPER_H