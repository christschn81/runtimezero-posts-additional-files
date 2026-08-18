#!/usr/bin/env python3
"""Enable or disable pod/container monitoring on VKS clusters in VCF Operations.

Examples:
  # List clusters and current pod monitoring state
  ./set_pod_monitoring.py --host ops-a.site-a.vcf.lab --username admin --list

  # Enable on specific clusters by name
  ./set_pod_monitoring.py --host ops-a.site-a.vcf.lab --username admin \\
      --cluster bookstore-app --cluster kubernetes-cluster-g546 --enable

  # No --cluster given -> interactively pick from the available list
  ./set_pod_monitoring.py --host ops-a.site-a.vcf.lab --username admin --enable
"""
from __future__ import annotations

import argparse
import getpass
import os
import sys

from vcfops_client import VcfOperationsClient, VksCluster

PASSWORD_ENV_VAR = "VCF_OPS_PASSWORD"


def state_label(enabled: bool) -> str:
    return "enabled" if enabled else "disabled"


def select_by_name(clusters: list[VksCluster], names: list[str]) -> list[VksCluster]:
    by_name = {c.name: c for c in clusters}
    # Report every bad name at once — exiting on the first one hides the rest and
    # makes fixing a long --cluster list a guess-and-retry loop.
    missing = [name for name in names if name not in by_name]
    if missing:
        sys.exit(f"Cluster(s) not found: {', '.join(missing)}")
    return [by_name[name] for name in names]


def prompt_for_clusters(clusters: list[VksCluster]) -> list[VksCluster]:
    print("\nAvailable VKS clusters:")
    for idx, cluster in enumerate(clusters, 1):
        print(f"  {idx}. {cluster.name}  (pod monitoring: {state_label(cluster.pod_monitoring_enabled)})")

    raw = input("\nSelect cluster number(s) to change, comma-separated (or 'all'): ").strip()
    if raw.lower() == "all":
        return clusters

    selected = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            selected.append(clusters[int(part) - 1])
        except (ValueError, IndexError):
            print(f"Ignoring invalid selection: {part}", file=sys.stderr)
    return selected


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", required=True, help="VCF Operations hostname, e.g. ops-a.site-a.vcf.lab")
    parser.add_argument("--username", required=True)
    parser.add_argument("--password",
                        help=f"Prompted for if omitted; ${PASSWORD_ENV_VAR} is used when set. "
                             "Prefer the env var for automation — argv is readable by other "
                             "processes and lands in shell history.")
    parser.add_argument("--cluster", action="append", dest="clusters",
                        help="VKS cluster name to change (repeatable). Omit to choose interactively.")
    # One action per run, enforced by argparse rather than by hand-rolled checks.
    # This also makes contradictions like `--list --enable` an error instead of
    # silently listing.
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--list", action="store_true", help="List clusters and their current state, then exit")
    action.add_argument("--enable", action="store_true", help="Enable pod/container monitoring")
    action.add_argument("--disable", action="store_true", help="Disable pod/container monitoring")
    args = parser.parse_args()

    password = args.password or os.environ.get(PASSWORD_ENV_VAR) or getpass.getpass(f"Password for {args.username}: ")
    client = VcfOperationsClient(args.host, args.username, password)

    clusters = client.list_vks_clusters()
    if not clusters:
        print("No VKS clusters found.")
        return

    if args.list:
        # Size the column to the data so long cluster names aren't truncated.
        width = max(len(c.name) for c in clusters)
        print(f"{'NAME':{width}}  POD MONITORING")
        for cluster in clusters:
            print(f"{cluster.name:{width}}  {state_label(cluster.pod_monitoring_enabled)}")
        return

    # --cluster filters client-side rather than via ResourceQuery.name: the SDK
    # documents that filter as supporting only a single name, and --cluster is
    # repeatable.
    targets = select_by_name(clusters, args.clusters) if args.clusters else prompt_for_clusters(clusters)
    if not targets:
        print("No clusters selected, nothing to do.")
        return

    # --enable/--disable are mutually exclusive and one action is required, so by
    # this point args.enable alone carries the desired state.
    desired = args.enable
    for cluster in targets:
        if cluster.pod_monitoring_enabled == desired:
            print(f"{cluster.name}: already {state_label(desired)}, skipping")
            continue
        client.set_pod_monitoring(cluster, desired)
        print(f"{cluster.name}: pod monitoring {state_label(desired)}")


if __name__ == "__main__":
    main()
