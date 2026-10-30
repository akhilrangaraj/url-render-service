#!/usr/bin/env bash
# Apply the renderer stack to the cluster (substituting the real image ref).
#   REGISTRY=registry.example.com/akhil TAG=latest ./scripts/deploy.sh
set -euo pipefail
cd "$(dirname "$0")/.."

REGISTRY="${REGISTRY:?set REGISTRY, e.g. registry.example.com/akhil}"
TAG="${TAG:-latest}"
IMG="${REGISTRY}/url-render-service:${TAG}"
NS="url-render"

echo "==> namespace"
kubectl apply -f k8s/namespace.yaml
echo "==> configmap"
kubectl -n "${NS}" apply -f k8s/configmap.yaml
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
