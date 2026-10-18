"""
Kubernetes runtime of the node.

With this runtime, the node runs as a pod in a Kubernetes cluster and starts every
algorithm run as a Kubernetes Job in the same namespace. The node and the algorithm
pods exchange the input, output and token files through a shared persistent volume
claim (the task volume), so that no host paths or pinned Kubernetes nodes are
required. This makes the runtime suitable for managed Kubernetes clusters and
OpenShift.
"""
