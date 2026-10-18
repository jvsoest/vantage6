#!/bin/sh
# Start the docker node of the end-to-end test, the same way `v6 node start` does
set -e
HERE=$(cd "$(dirname "$0")" && pwd)
NAME=docker-node
IMAGE=${NODE_IMAGE:-vantage6-node:e2e}
docker rm -f vantage6-$NAME-user >/dev/null 2>&1 || true
docker run -d --name vantage6-$NAME-user --network kind \
  -e DATA_VOLUME_NAME=vantage6-$NAME-user-vol \
  -e VPN_VOLUME_NAME=vantage6-$NAME-user-vpn-vol \
  -e SSH_TUNNEL_VOLUME_NAME=vantage6-$NAME-user-ssh-vol \
  -e SSH_SQUID_VOLUME_NAME=vantage6-$NAME-user-squid-vol \
  -e DEFAULT_DATABASE_URI=default.csv \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v "$HERE/docker-node.yaml:/mnt/config/$NAME.yaml:ro" \
  -v "$HERE/docker-data.csv:/mnt/default.csv:ro" \
  -v vantage6-$NAME-user-vol:/mnt/data \
  -v vantage6-$NAME-user-vpn-vol:/mnt/vpn \
  -v vantage6-$NAME-user-ssh-vol:/mnt/ssh \
  -v vantage6-$NAME-user-squid-vol:/mnt/squid \
  --label vantage6-type=node \
  "$IMAGE" \
  vnode-local start -c /mnt/config/$NAME.yaml -n $NAME --dockerized --user
