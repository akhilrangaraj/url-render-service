#!/usr/bin/env bash
# Apply the renderer stack to the cluster (substituting the real image ref).
#   TAG=latest ./scripts/deploy.sh
#   REGISTRY=registry.example.com/akhil TAG=v1 ./scripts/deploy.sh
set -euo pipefail
cd "$(dirname "$0")/.."

REGISTRY="${REGISTRY:-registry.rangaraj.net/akhilrangaraj}"
TAG="${TAG:-latest}"
IMG="${REGISTRY}/url-render-service:${TAG}"
NS="${NS:-browsers}"
PULL_SECRET="${PULL_SECRET:-regcred}"

echo "==> namespace"
kubectl apply -f k8s/namespace.yaml

echo "==> image pull secret ${PULL_SECRET}"
if ! kubectl -n "${NS}" get secret "${PULL_SECRET}" >/dev/null 2>&1; then
  echo "error: secret '${PULL_SECRET}' (type kubernetes.io/dockerconfigjson) is missing in namespace '${NS}'." >&2
  echo "       Create it, e.g. by copying from another namespace:" >&2
  echo "       kubectl -n <ns> get secret ${PULL_SECRET} -o json | jq 'del(.metadata | .namespace, .uid, .resourceVersion, .creationTimestamp, .ownerReferences, .managedFields)' | kubectl -n ${NS} apply -f -" >&2
  exit 1
fi

echo "==> configmap"
kubectl -n "${NS}" apply -f k8s/configmap.yaml

echo "==> secret url-render-auth"
if kubectl -n "${NS}" get secret url-render-auth >/dev/null 2>&1; then
  echo "secret url-render-auth already exists"
else
  # Fed via stdin so the key never appears in the process list.
  printf 'default:%s' "$(openssl rand -hex 24)" | kubectl -n "${NS}" create secret generic url-render-auth \
    --from-file=api-keys=/dev/stdin >/dev/null
  echo "created secret url-render-auth with a generated key for user 'default'"
  echo "read the key back with:"
  echo "  kubectl -n ${NS} get secret url-render-auth -o jsonpath='{.data.api-keys}' | base64 -d"
fi

echo "==> deployment (image=${IMG})"
sed "s#REPLACE_WITH_REGISTRY/url-render-service:latest#${IMG}#" k8s/deployment.yaml | kubectl -n "${NS}" apply -f -
echo "==> service"
kubectl -n "${NS}" apply -f k8s/service.yaml
echo "==> networkpolicy"
kubectl -n "${NS}" apply -f k8s/networkpolicy.yaml

echo
echo "==> rollout"
kubectl -n "${NS}" rollout status deploy/url-render --timeout=180s
kubectl -n "${NS}" get pods,svc

echo
echo "Next:"
echo "  ./scripts/port-forward.sh"
echo "  curl -s localhost:8000/health"
