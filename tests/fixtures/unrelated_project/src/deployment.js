// A Kubernetes-manifest-as-JS helper -- "apiVersion" is a Kubernetes field
// name here, not a Stripe/OpenAI API version pin.
function buildDeployment() {
  return {
    apiVersion: "apps/v1",
    kind: "Deployment",
    metadata: { name: "worker" },
  };
}

module.exports = { buildDeployment };
