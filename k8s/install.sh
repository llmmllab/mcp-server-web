#!/bin/bash
# Web MCP Server Kubernetes Installation Script
# Usage: ./k8s/install.sh [--create-namespace]

set -e

NAMESPACE="${NAMESPACE:-llmmllab}"
REGISTRY=${REGISTRY:-192.168.0.71:31500}
TAG=${TAG:-latest}
CREATE_NAMESPACE="$1"

docker buildx build --platform linux/amd64,linux/arm64 -t ${REGISTRY}/mcp-server-web:${TAG} --push .

echo "=== Web MCP Server Installation ==="
echo "Namespace: $NAMESPACE"

# Create namespace if requested
if [ "$CREATE_NAMESPACE" = "--create-namespace" ]; then
    echo "Creating namespace $NAMESPACE..."
    kubectl create namespace $NAMESPACE --dry-run=client -o yaml | kubectl apply -f -
fi

# Apply deployment
echo "Applying Web MCP Server deployment..."
kubectl apply -f k8s/deployment.yaml

echo ""
echo "=== Installation Complete ==="
echo ""
echo "Check pod status:"
echo "  kubectl get pods -n $NAMESPACE -l app=mcp-server-web"
echo ""
echo "View logs:"
echo "  kubectl logs -n $NAMESPACE -l app=mcp-server-web -f"
