#!/usr/bin/env bash
# Build (and optionally push) the renderer image.
#   REGISTRY=registry.example.com/akhil ./scripts/build.sh
#   TAG=v1 REGISTRY=... ./scripts/build.sh --push
set -euo pipefail
cd "$(dirname "$0")/.."

REGISTRY="${REGISTRY:?set REGISTRY, e.g. registry.example.com/akhil}"
TAG="${TAG:-latest}"
IMG="${REGISTRY}/url-render-service:${TAG}"

echo "==> docker build ${IMG}"
docker build -t "${IMG}" -t "url-render-service:local" .

if [[ "${1:-}" == "--push" ]]; then
  echo "==> docker push ${IMG}"
  docker push "${IMG}"
fi

echo
echo "Done. To deploy:  REGISTRY=${REGISTRY} TAG=${TAG} ./scripts/deploy.sh"
