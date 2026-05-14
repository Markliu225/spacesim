/*
 * LLMRequestHelper — thin install-on-node helper for LLMRequestApplication.
 *
 * Per-row of the schedule CSV the higher-level LlmWorkloadScheduler builds
 * one helper, sets all the LLM parameters, and Install()s the application
 * on the source GS node.
 */
#ifndef LLM_REQUEST_HELPER_H
#define LLM_REQUEST_HELPER_H

#include "ns3/address.h"
#include "ns3/application-container.h"
#include "ns3/node-container.h"
#include "ns3/object-factory.h"

namespace ns3 {

class LLMRequestHelper
{
public:
    LLMRequestHelper(Address dest_addr, uint16_t dest_port);
    void SetAttribute(const std::string &name, const AttributeValue &value);

    ApplicationContainer Install(Ptr<Node> node) const;

private:
    ObjectFactory m_factory;
};

} // namespace ns3

#endif // LLM_REQUEST_HELPER_H