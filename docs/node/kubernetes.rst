.. _node-kubernetes:

Running a node on Kubernetes
----------------------------

Besides on a (virtual) machine with Docker, a node can run in a Kubernetes cluster.
The node then runs as a pod and starts each algorithm as a Kubernetes Job in the
same namespace. This works on plain Kubernetes, on managed Kubernetes services and
on OpenShift: the node does not need host paths, privileged containers, access to
a Docker daemon or a specific Kubernetes node.

Algorithms receive the same input, environment variables and files on Kubernetes
nodes as on Docker nodes. A collaboration can therefore consist of both Docker and
Kubernetes nodes, and the same algorithm images can be used on both.

How it works
^^^^^^^^^^^^

.. code:: text

            vantage6 server
                  ^
                  | https (outgoing only)
    +-------------|-------------------------------------------------+
    | namespace   |                                                 |
    |   +---------+---------+  creates Jobs  +--------------------+ |
    |   |     node pod      |--------------->|   algorithm pod    | |
    |   |  proxy on :8080   |<---------------|   (one per run)    | |
    |   +---------+---------+  via Service   +---------+----------+ |
    |             |                                    |            |
    |             +----------- task volume ------------+            |
    |               (input, output and token files)                 |
    +---------------------------------------------------------------+

- The node pod authenticates with the server and listens for tasks, exactly as a
  Docker node does.
- For every run, the node writes the input and token to a shared *task volume*
  (a persistent volume claim) and creates a Job. The algorithm pod only mounts the
  folder of its own run and the temporary folder of its job, at the same paths as
  on a Docker node (``/mnt/data/task-<run id>/`` and ``/mnt/tmp``).
- Algorithms reach the server through the proxy server of the node, via a
  Kubernetes Service. NetworkPolicies prevent algorithms from reaching anything
  else, except DNS and services you explicitly allow (e.g. a database server).
- When the Job has finished, the node reads the output from the task volume,
  sends the results to the server and deletes the Job.
- If the node pod is restarted (e.g. rescheduled to another Kubernetes node), the
  running algorithm Jobs keep running. The new node pod picks them up and reports
  their results when they finish.

Requirements
^^^^^^^^^^^^

- A namespace for the node. Install each node in its own namespace.
- Permission to create a Deployment, Service, ServiceAccounts, a Role and
  RoleBinding, Secrets, NetworkPolicies and a PersistentVolumeClaim in that
  namespace. No cluster-wide permissions are required.
- A storage class that supports ``ReadWriteMany`` for the task volume, e.g. NFS,
  CephFS, Azure Files, Amazon EFS or Google Filestore. If only ``ReadWriteOnce`` is
  available, algorithm pods are scheduled on the same Kubernetes node as the node
  pod (see :ref:`node-kubernetes-rwo`).
- A network plugin that enforces NetworkPolicies (most do, e.g. Calico, Cilium,
  OVN-Kubernetes on OpenShift). Without it, algorithms are **not** isolated from
  the network.
- Outgoing access from the node pod to the vantage6 server and to the registries
  of the algorithm images.

Installation
^^^^^^^^^^^^

The node is installed with the Helm chart in ``charts/vantage6-node`` of the
vantage6 repository. Create a values file, for instance ``my-node.yaml``:

.. code:: yaml

    node:
      serverUrl: https://server.example.org
      port: 443
      apiPath: /api
      apiKey: <API key of the node>
      databases:
        - label: default
          type: csv
          uri: data.csv
          mount_mode: ro
          volume:
            claim_name: research-data
      policies:
        allowed_algorithms:
          - ^ghcr\.io/vantage6/algorithm/.*
    taskVolume:
      storageClass: <a ReadWriteMany storage class>

and install the chart:

.. code:: bash

    helm install my-node charts/vantage6-node -n my-node --create-namespace \
      -f my-node.yaml

Follow the logs of the node with
``kubectl logs -n my-node deploy/my-node-vantage6-node -f``.

All options are documented in the ``values.yaml`` file of the chart. Instead of
having the chart generate the node configuration, you can also provide a complete
node configuration file in an existing secret (``node.existingConfigSecret``,
key ``config.yaml``). See :ref:`node-kubernetes-config` for the configuration
options that are specific to Kubernetes.

Databases
^^^^^^^^^

Three kinds of databases are supported:

**Service databases**
    Databases that algorithms connect to over the network, such as a PostgreSQL or
    OMOP database or a SPARQL endpoint. The ``uri`` is passed to the algorithm as
    is. Allow algorithms to reach the database with a NetworkPolicy egress rule in
    ``algorithms.egress``, for example:

    .. code:: yaml

        algorithms:
          egress:
            - to:
                - ipBlock:
                    cidr: 10.0.12.0/24
              ports:
                - protocol: TCP
                  port: 5432

**Files and folders on a volume (read-only)**
    Set ``mount_mode: ro`` and the persistent volume claim that holds the data in
    ``volume.claim_name`` (and optionally a folder on the claim in
    ``volume.sub_path``). The ``uri`` is the path of the file or folder relative to
    the root of the claim (or ``sub_path``). The file or folder is mounted
    read-only in the algorithm pods, at ``/mnt/<label>.<extension>`` for files and
    ``/mnt/<label>`` for folders, just like on a Docker node. Nothing is copied.

**Files copied to the task volume**
    Without ``mount_mode: ro`` (i.e. the default ``copy`` mode), the node copies the
    file to the task volume when it starts, and mounts the copy read-only in the
    algorithm pods. The file is read from ``volume.claim_name`` if given, or
    otherwise from the path ``uri`` in the node pod.

The node needs access to file databases to share the column names with the server.
The chart therefore also mounts the claims of databases in the node pod.

OpenShift
^^^^^^^^^

The chart detects OpenShift automatically (or set ``openshift.enabled: true``). On
OpenShift:

- the node and algorithm pods run under the ``restricted-v2`` security context
  constraint, with the user that OpenShift assigns to the namespace. The chart does
  not set ``runAsUser``, ``runAsGroup`` or ``fsGroup``;
- the NetworkPolicies allow DNS through the OpenShift DNS service;
- no Route is needed, because the node only makes outgoing connections.

.. _node-kubernetes-rwo:

Task volumes that are ReadWriteOnce
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

A ``ReadWriteOnce`` volume can only be mounted on one Kubernetes node at a time. If
``taskVolume.accessMode`` is ``ReadWriteOnce``, the algorithm pods get a pod
affinity rule that schedules them on the same Kubernetes node as the node pod. You
don't need to pin the node to a specific Kubernetes node for this.

Note that this limits algorithms to the resources of a single Kubernetes node, and
that a node pod that is rescheduled to another Kubernetes node can only start when
the algorithm pods that still use the volume have finished.

Security
^^^^^^^^

- Algorithm pods run as a non-root user, without privileges, with all Linux
  capabilities dropped, with the ``RuntimeDefault`` seccomp profile and without a
  service account token. Each algorithm pod can only see the files of its own run.
- Algorithm images are pinned to the digest that was checked against the node
  policies, so that the image that runs is the image that was allowed.
- The node has a Role that only allows it to manage Jobs and read pods and their
  logs in its own namespace.
- Because algorithms do not run as root, algorithm images should not need to
  write outside the temporary folder (``/mnt/tmp``), the output file and ``/tmp``.
  On OpenShift, images also have to work with an arbitrary user ID.

.. _node-kubernetes-config:

Configuration options
^^^^^^^^^^^^^^^^^^^^^

The Helm chart generates the node configuration file. If you write the node
configuration yourself, set ``runtime: kubernetes`` and configure the runtime in
the ``kubernetes`` section:

.. code:: yaml

    runtime: kubernetes
    kubernetes:
      # Namespace of the node, defaults to the namespace of the pod
      namespace: my-node
      # Persistent volume claim of the task volume, mounted in the node pod at
      # task_volume_mount_path
      task_pvc: my-node-tasks
      task_volume_mount_path: /mnt/data
      # Service account for algorithm pods (without permissions)
      algorithm_service_account: my-node-algorithm
      # Pull secrets for algorithm images
      image_pull_secrets: [my-node-registries]
      # Labels of the node pod, used to co-locate algorithm pods
      node_pod_labels: {app.kubernetes.io/instance: my-node}
      job:
        # Remove finished jobs after this many seconds (the node removes them
        # itself after reporting their results)
        ttl_seconds_after_finished: 86400
        # Maximum run time of an algorithm in seconds (default: no limit)
        active_deadline_seconds: null
        # Fail a run if its image cannot be pulled within this many seconds
        image_pull_timeout_seconds: 300
        # Fail a run if it cannot be scheduled within this many seconds
        # (default: wait until it can be scheduled)
        pending_timeout_seconds: null
        # Leave running jobs alone when the node stops, so they can be picked up
        # when it starts again
        keep_on_shutdown: true
      algorithm_pod:
        # These are passed to Kubernetes as-is
        resources: {limits: {memory: 4Gi, cpu: "2"}}
        node_selector: {}
        tolerations: []
        affinity: {}
        priority_class_name: null
        # Pod security context, e.g. runAsUser, runAsGroup, fsGroup. Leave these
        # out on OpenShift.
        security_context: {runAsUser: 1000, runAsGroup: 1000, fsGroup: 1000}
        read_only_root_filesystem: false
        # Resource to request when algorithm_device_requests.gpu is true
        gpu_resource_name: nvidia.com/gpu
        # Schedule algorithm pods on the Kubernetes node of the node pod
        co_locate_with_node: false

The node is then started in the pod with
``vnode-local start --config <config file> --runtime kubernetes``. The environment
variable ``PROXY_SERVER_HOST`` should contain the name of the Service through which
algorithms reach the proxy server of the node.

Limitations
^^^^^^^^^^^

The following features of Docker nodes are not available on Kubernetes nodes, and
the node refuses to start if they are configured:

- VPN, and therefore communication between algorithm containers
  (``vpn_subnet``);
- SSH tunnels (``ssh-tunnels``);
- the Squid proxy whitelist (``whitelist``); use ``algorithms.egress``
  NetworkPolicy rules instead;
- linking Docker services (``docker_services``).

The options ``node_extra_env``, ``node_extra_mounts``, ``node_extra_hosts`` and
``images`` only apply to Docker nodes and are ignored. Use the Helm chart to
customize the node pod instead.
