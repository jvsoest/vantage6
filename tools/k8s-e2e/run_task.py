"""
Run the demo average algorithm on the nodes of the end-to-end test collaboration.

Usage: python run_task.py <server url> <organization name>[,<organization name>]
       [--database LABEL] [--image IMAGE] [--expect-status STATUS] [--partial]
"""

import argparse
import json
import sys
import time

from vantage6.client import Client

parser = argparse.ArgumentParser()
parser.add_argument("server")
parser.add_argument("organizations")
parser.add_argument("--database", default="default")
parser.add_argument("--image", default="ghcr.io/vantage6/algorithm/demo/average:4")
parser.add_argument("--expect-status", default="completed")
parser.add_argument("--partial", action="store_true", help="run partial_average only")
parser.add_argument("--kill-after", type=int, default=None)
parser.add_argument("--timeout", type=int, default=600)
args = parser.parse_args()

host, port = args.server.rsplit(":", 1)
client = Client(host, int(port), "/api", log_level="warn")
client.authenticate("admin", "Password123!")
client.setup_encryption(None)

orgs = {o["name"]: o["id"] for o in client.organization.list(per_page=100)["data"]}
org_ids = [orgs[name] for name in args.organizations.split(",")]
collaboration = client.collaboration.list()["data"][0]["id"]

method = "partial_average" if args.partial else "central_average"
task = client.task.create(
    collaboration=collaboration,
    organizations=org_ids if args.partial else org_ids[:1],
    name=f"e2e {method}",
    image=args.image,
    description="end-to-end test",
    input_={"method": method, "kwargs": {"column_name": "age"}},
    databases=[{"label": args.database}],
)
print(f"Created task {task['id']}", flush=True)

if args.kill_after is not None:
    time.sleep(args.kill_after)
    client.task.kill(task["id"])
    print(f"Killed task {task['id']}", flush=True)

start = time.time()
while True:
    runs = client.run.from_task(task["id"])["data"]
    statuses = [r["status"] for r in runs]
    if runs and all(s not in ("pending", "initializing", "active") for s in statuses):
        break
    if time.time() - start > args.timeout:
        print(f"Timeout, statuses: {statuses}")
        sys.exit(1)
    time.sleep(3)

ok = True
for run in runs:
    print(f"run {run['id']}: {run['status']}")
    print(f"  log: {(run.get('log') or '')[-600:]}")
    if run["status"] != args.expect_status:
        ok = False
if args.expect_status == "completed":
    results = client.result.from_task(task["id"])
    print("results:", json.dumps(results))
sys.exit(0 if ok else 1)
