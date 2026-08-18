"""Reusable client for VCF Operations, built on the official `vcf-operations`
Python SDK (part of the `vcf-sdk` meta-package published by Broadcom on PyPI:
https://pypi.org/project/vcf-sdk/).

    pip install vcf-operations==9.1.0.0
"""
from __future__ import annotations

from dataclasses import dataclass

import requests
import urllib3

from vcf.operations.api.auth.token_client import Acquire
from vcf.operations.api.resources_client import Query
from vcf.operations.api_client import Resources
from vcf.operations.model_client import Resource, ResourceIdentifier, ResourceQuery, UsernamePassword
from vmware.vapi.lib.connect import get_requests_connector
from vmware.vapi.security.client.security_context_filter import LegacySecurityContextFilter
from vmware.vapi.security.http_authorization import create_http_authorization_security_context
from vmware.vapi.stdlib.client.factories import StubConfigurationFactory

# The resource identifier backing the "Pod Monitoring" toggle in the VKS cluster UI.
# Not named in any published spec — found by diffing the resource model of clusters
# with the setting on vs. off, so treat it as reverse-engineered rather than contractual.
POD_CONTAINER_MONITORING = "POD_CONTAINER_MONITORING"

# VKS clusters are collected by the vSphere Supervisor adapter and modelled under the
# "GuestCluster" resource kind, which the UI labels "VKS Cluster".
SUPERVISOR_ADAPTER_KIND = "SupervisorAdapter"
GUEST_CLUSTER_RESOURCE_KIND = "GuestCluster"

OPS_AUTHZ_SCHEME = "OpsToken"
API_ENDPOINT = "suite-api"

# Single-request cap when listing clusters. Realistic deployments have tens of VKS
# clusters, so one page is enough; sites past this would need real paging (the SDK
# exposes `page=` and returns `page_info.total_count`).
_MAX_CLUSTERS = 1000


@dataclass(frozen=True)
class VksCluster:
    """A VKS cluster, wrapping the SDK ``Resource`` it came from.

    ``name`` and ``pod_monitoring_enabled`` are read through to ``resource`` on each
    access rather than copied out of it. That matters because writes mutate the
    resource in place: copied fields would go stale the moment a setting changed,
    whereas these cannot disagree with the object that gets sent back to the API.

    ``frozen=True`` pins which resource a cluster refers to; the resource's own
    contents stay mutable, which is what identifier updates rely on.
    """

    resource: Resource

    @property
    def id(self) -> str:
        return self.resource.identifier

    @property
    def name(self) -> str:
        return self.resource.resource_key.name

    @property
    def pod_monitoring_enabled(self) -> bool:
        identifier = self.find_identifier(POD_CONTAINER_MONITORING)
        # Identifier values are strings over the wire, not JSON booleans.
        return identifier is not None and identifier.value == "true"

    def find_identifier(self, name: str) -> ResourceIdentifier | None:
        """Return the named resource identifier, or None if the resource lacks it."""
        return next(
            (i for i in self.resource.resource_key.resource_identifiers if i.identifier_type.name == name),
            None,
        )

    def set_identifier(self, name: str, value: str) -> None:
        """Set an existing resource identifier's value in place.

        Deliberately refuses to add missing identifiers: the set of identifiers is
        defined by the resource kind, and inventing one here would produce a resource
        the server rejects rather than a useful error.
        """
        identifier = self.find_identifier(name)
        if identifier is None:
            raise ValueError(f"{name!r} identifier not found on cluster {self.name!r}")
        identifier.value = value


def _new_stub_config(host: str, username: str, password: str, session: requests.Session):
    """Acquire a token and build an authenticated stub configuration.

    Two configurations are needed, which is why this looks repetitive: the token
    endpoint must be called *before* a token exists, so it gets an unauthenticated
    connector, and the returned token is then baked into a second connector as a
    security-context filter that stamps every later call. Both share one
    ``requests.Session``, so this costs an extra object rather than an extra
    connection pool.

    Mirrors the SDK's own sample helper (vcf-operations-samples/operations/helpers/
    client.py), which ships in the GitHub repo but not in the pip package.
    """
    host_url = f"https://{host}/{API_ENDPOINT}"

    unauth_stub_config = StubConfigurationFactory.new_std_configuration(
        get_requests_connector(session=session, msg_protocol="rest", url=host_url)
    )
    token = Acquire(unauth_stub_config).acquire_token(
        username_password=UsernamePassword(auth_source=None, username=username, password=password)
    ).token

    sec_ctx = create_http_authorization_security_context(authz_credentials=token, authn_scheme=OPS_AUTHZ_SCHEME)
    return StubConfigurationFactory.new_std_configuration(
        get_requests_connector(
            session=session,
            url=host_url,
            msg_protocol="rest",
            provider_filter_chain=[LegacySecurityContextFilter(security_context=sec_ctx)],
        )
    )


class VcfOperationsClient:
    def __init__(self, host: str, username: str, password: str, verify_ssl: bool = False):
        session = requests.Session()
        session.verify = verify_ssl
        if not verify_ssl:
            # Lab appliances ship self-signed certs. Silence only the matching warning
            # class — the SDK sample's bare disable_warnings() drops every urllib3
            # warning process-wide, including ones unrelated to this client.
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

        stub_config = _new_stub_config(host, username, password, session)
        self._resources = Resources(stub_config)
        self._query = Query(stub_config)

    def list_vks_clusters(self) -> list[VksCluster]:
        query = ResourceQuery(
            adapter_kind=[SUPERVISOR_ADAPTER_KIND],
            resource_kind=[GUEST_CLUSTER_RESOURCE_KIND],
        )
        result = self._query.get_matching_resources(query, page_size=_MAX_CLUSTERS)
        return [VksCluster(resource) for resource in result.resource_list]

    def set_pod_monitoring(self, cluster: VksCluster, enabled: bool) -> VksCluster:
        """Enable or disable pod & container monitoring on a VKS cluster.

        Sends the whole resource back, not a patch of the one changed field. Per the
        SDK's own docs on update_resource, omitting identifiers that are unique and
        required fails with a 500, and omitting unique-but-optional ones silently
        blanks them — so mutating the fetched resource in place is the safe shape.

        The resource carries its own id, so no separate identifier is passed here.
        """
        cluster.set_identifier(POD_CONTAINER_MONITORING, "true" if enabled else "false")
        updated = self._resources.update_resource(cluster.resource)
        # The API echoes the stored resource; prefer it so the caller observes what
        # actually landed rather than the copy we sent.
        return VksCluster(updated) if updated is not None else cluster
