"""
Runtime-independent parts of running algorithms on a node.

The node can start algorithms using different container runtimes (e.g. Docker or
Kubernetes). This package contains the interface that every runtime implements and
the logic that is shared between runtimes, such as the node policies on allowed
algorithms and the preparation of the algorithm input/output files.
"""
