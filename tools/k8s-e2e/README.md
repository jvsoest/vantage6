# End-to-end test for Kubernetes nodes

Tests a collaboration with one node on Kubernetes (installed with the Helm chart in
`charts/vantage6-node`) and one docker node, using the sources of this repository.

Requirements: docker, [kind](https://kind.sigs.k8s.io/), kubectl and helm.

```sh
./run.sh          # build images, set up everything and run the tests
./run.sh cleanup  # remove the kind cluster and containers
```

The script:

1. builds a node image with the sources of this repository (`node.Dockerfile`);
2. creates a kind cluster `v6-e2e` and starts a v4 server on the docker network
   `kind`, with the organizations and collaboration in `fixtures.yaml`;
3. installs a Kubernetes node with `values.yaml`, with a read-only and a copied
   csv database on a volume (`data.yaml`), and starts a docker node
   (`docker-node.sh`);
4. runs the demo average algorithm over both nodes, with the central task on either
   node, checks node policies and killing tasks, and checks that algorithm pods can
   only reach the node proxy (`isolation-probe.yaml`).

kind only provides `ReadWriteOnce` volumes, so the test also covers co-locating
algorithm pods with the node pod.
