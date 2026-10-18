#!/bin/sh
# End-to-end test of a collaboration with a Kubernetes node and a docker node.
#
# Requires docker, kind, kubectl and helm. Creates a kind cluster 'v6-e2e', a v4
# server container and a docker node container, all on the docker network 'kind'.
# Leftovers of a previous run are removed first. Run `./run.sh cleanup` to remove
# everything afterwards.
set -eu

HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$HERE/../.." && pwd)
CLUSTER=v6-e2e
NAMESPACE=v6
SERVER_IMAGE=${SERVER_IMAGE:-ghcr.io/vantage6/infrastructure/server:4.15.2}
TEST_IMAGE=${TEST_IMAGE:-python:3.10-slim}

cleanup() {
  kind delete cluster --name "$CLUSTER" || true
  docker rm -f v6-e2e-server vantage6-docker-node-user >/dev/null 2>&1 || true
  docker ps -aq --filter label=node=docker-node | xargs -r docker rm -f
  docker network rm vantage6-docker-node-user-net >/dev/null 2>&1 || true
  docker volume ls -q --filter name=vantage6-docker-node-user | xargs -r docker volume rm
}

if [ "${1:-}" = "cleanup" ]; then
  cleanup
  exit 0
fi

step() { echo; echo "=== $*"; }

# runs the task script in a container with the vantage6 client of this repository
run_task() {
  docker run --rm --network kind -v "$REPO:/repo" -w /repo/tools/k8s-e2e \
    -e PYTHONPATH=/repo/vantage6-common:/repo/vantage6-client:/repo/vantage6-algorithm-tools:/repo/vantage6 \
    v6-e2e-client python run_task.py http://v6-e2e-server:80 "$@"
}

step "Remove leftovers of a previous run"
cleanup

step "Build images"
docker build -q -f "$HERE/node.Dockerfile" -t vantage6-node:e2e "$REPO"
printf 'FROM %s\nCOPY requirements.txt /tmp/\nRUN pip install --no-cache-dir -r /tmp/requirements.txt\n' \
  "$TEST_IMAGE" | docker build -q -t v6-e2e-client -f - "$REPO"

step "Create kind cluster"
kind create cluster --name "$CLUSTER" --wait 120s
kind load docker-image vantage6-node:e2e --name "$CLUSTER"

step "Start server"
docker rm -f v6-e2e-server >/dev/null 2>&1 || true
docker run -d --name v6-e2e-server --network kind \
  -v "$HERE/server.yaml:/mnt/config.yaml:ro" \
  -v "$HERE/fixtures.yaml:/mnt/fixtures.yaml:ro" \
  "$SERVER_IMAGE" /vantage6/vantage6-server/server.sh >/dev/null
until docker exec v6-e2e-server python -c \
  "import urllib.request; urllib.request.urlopen('http://localhost/api/version')" \
  >/dev/null 2>&1; do
  sleep 2
done
docker exec v6-e2e-server vserver-local import --config /mnt/config.yaml /mnt/fixtures.yaml

step "Install Kubernetes node"
kubectl create namespace "$NAMESPACE"
kubectl -n "$NAMESPACE" apply -f "$HERE/data.yaml"
kubectl -n "$NAMESPACE" wait --for=jsonpath='{.status.phase}'=Succeeded \
  pod/e2e-data-writer --timeout=180s
helm install node "$REPO/charts/vantage6-node" -n "$NAMESPACE" \
  -f "$HERE/values.yaml" --wait --timeout 5m

step "Start docker node"
"$HERE/docker-node.sh"

step "Average over both nodes (central task on the Kubernetes node)"
run_task k8s-org,docker-org

step "Average over both nodes (central task on the docker node)"
run_task docker-org,k8s-org

step "Partial average on the Kubernetes node with a copied database"
run_task k8s-org --partial --database copied

step "Image that is not allowed by the node policies"
run_task k8s-org --partial --image ghcr.io/vantage6/algorithm/demo/other:4 \
  --expect-status "not allowed"

step "Kill a running task"
docker stop vantage6-docker-node-user >/dev/null
run_task k8s-org,docker-org --kill-after 15 --expect-status "killed by user" \
  --timeout 120 || true
test -z "$(kubectl -n "$NAMESPACE" get jobs -o name)"
docker start vantage6-docker-node-user >/dev/null

step "Algorithm pods can only reach the node proxy"
kubectl -n "$NAMESPACE" delete pod e2e-isolation-probe --ignore-not-found
kubectl -n "$NAMESPACE" apply -f "$HERE/isolation-probe.yaml"
kubectl -n "$NAMESPACE" wait --for=jsonpath='{.status.phase}'=Succeeded \
  pod/e2e-isolation-probe --timeout=120s
probe=$(kubectl -n "$NAMESPACE" logs e2e-isolation-probe)
echo "$probe"
echo "$probe" | tr '\n' ' ' | grep -q "proxy reachable server blocked internet blocked"

echo
echo "All end-to-end tests passed"
