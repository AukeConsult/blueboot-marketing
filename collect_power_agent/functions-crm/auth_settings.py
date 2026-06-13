"""Central CRM API authentication policy.

Every Flask API route must have a matching rule here. When a route is added,
deleted, renamed, or gets different HTTP methods, update this table in the
same change.

Stored role names:
- guest: signed in, no assigned role yet
- user: can read normal campaign views
- campaign-user: can read and change campaign work
- admin: can manage all settings and users

Service access is not mapped to CRM user roles. A service caller must present a
Google service identity that has one of the service roles listed below.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from fnmatch import fnmatchcase
from functools import lru_cache


ADMIN = "admin"
CAMPAIGN_USER = "campaign-user"
USER = "user"
GUEST = "guest"

CRM_SERVICE_INVOKER = "crm.serviceInvoker"


@dataclass(frozen=True)
class ServiceRolePolicy:
    service_role: str
    description: str


SERVICE_ROLE_POLICIES: tuple[ServiceRolePolicy, ...] = (
    ServiceRolePolicy(
        CRM_SERVICE_INVOKER,
        "Allows trusted schedulers/runners to call routes marked service_call(...).",
    ),
)


class AuthKind(str, Enum):
    BLOCKED = "blocked"
    PUBLIC = "public"
    WORKER = "worker"
    SERVICE = "service"
    SIGNED_IN = "signed-in"
    ROLE = "role"


@dataclass(frozen=True)
class ApiRule:
    methods: tuple[str, ...]
    path: str
    auth: AuthKind
    min_role: str | None = None
    service_role: str | None = None
    note: str = ""


def _m(*methods: str) -> tuple[str, ...]:
    return tuple(m.upper() for m in methods)


def public(methods: tuple[str, ...], path: str, note: str = "") -> ApiRule:
    return ApiRule(methods, path, AuthKind.PUBLIC, note=note)


def worker(methods: tuple[str, ...], path: str, note: str = "") -> ApiRule:
    return ApiRule(methods, path, AuthKind.WORKER, note=note)


def service_call(
    methods: tuple[str, ...],
    path: str,
    note: str = "",
    service_role: str = CRM_SERVICE_INVOKER,
) -> ApiRule:
    return ApiRule(methods, path, AuthKind.SERVICE, service_role=service_role, note=note)


def blocked_route(methods: tuple[str, ...], path: str, note: str = "") -> ApiRule:
    return ApiRule(methods, path, AuthKind.BLOCKED, note=note)


def guest_read(path: str, note: str = "") -> ApiRule:
    return ApiRule(_m("GET"), path, AuthKind.ROLE, GUEST, note=note)


def user_read(path: str, note: str = "") -> ApiRule:
    return ApiRule(_m("GET"), path, AuthKind.ROLE, USER, note=note)


def user_personal(methods: tuple[str, ...], path: str, note: str = "") -> ApiRule:
    return ApiRule(methods, path, AuthKind.ROLE, USER, note=note)


def campaign_work(methods: tuple[str, ...], path: str, note: str = "") -> ApiRule:
    return ApiRule(methods, path, AuthKind.ROLE, CAMPAIGN_USER, note=note)


def admin_only(methods: tuple[str, ...], path: str, note: str = "") -> ApiRule:
    return ApiRule(methods, path, AuthKind.ROLE, ADMIN, note=note)


API_RULES: tuple[ApiRule, ...] = (
    # Infrastructure.
    public(_m("OPTIONS"), "*", "CORS preflight"),
    worker(_m("POST"), "/api/crm/worker/*", "Cloud Tasks worker"),

    # Service-account routes. These bypass CRM user roles and are validated
    # against service roles, not hardcoded service account IDs.
    service_call(_m("GET", "POST"), "/outreach-send", "direct smartMail"),
    service_call(_m("POST"), "/inbound-read", "direct smartMail"),
    service_call(_m("GET", "POST"), "/reply-match", "direct smartMail"),

    # blocked from frontend and only to be used by smartMail to prevent concurrency
    blocked_route(_m("GET", "POST"), "/api/crm/outreach-send", "use direct service-authenticated smartMail route"),
    blocked_route(_m("GET", "POST"), "/api/crm/reply-match", "use direct service-authenticated smartMail route"),
    blocked_route(_m("GET", "POST"), "/api/crm/reply_match", "use direct service-authenticated smartMail route"),

    # Guest-readable general routes.
    guest_read("/", "service index"),
    guest_read("/api/crm/whoami", "debug identity"),
    guest_read("/api/crm/statistics", "general dashboard statistics"),
    guest_read("/api/crm/filter-facets", "general filter metadata"),
    guest_read("/api/crm/filter-facets/*", "general filter metadata"),
    guest_read("/api/crm/leads/by-domain/*", "general lead lookup"),

    # User campaign reads. A standard user can see campaigns but cannot change them.
    user_read("/api/crm/campaigns"),
    user_read("/api/crm/campaigns/*"),
    user_read("/api/crm/followup-contacts"),
    user_read("/api/crm/followup-meta"),
    user_read("/api/crm/status/*", "job status read"),
    user_read("/api/crm/jobs", "job history read"),
    user_read("/api/crm/gdisk/check", "Drive integration check"),
    user_read("/api/crm/gdisk/files", "Drive file listing"),
    user_read("/api/crm/gdisk/files/*", "Drive file read"),
    user_read("/api/crm/mailbox-tags/*", "mailbox tag read"),
    user_read("/api/crm/batch/jobs", "Cloud Batch job list"),
    user_read("/api/crm/batch/jobs/*/runs", "Cloud Batch run list"),
    user_read("/api/crm/batch/jobs/*/runs/*", "Cloud Batch run detail"),
    user_read("/api/crm/batch/jobs/*/tasks", "Cloud Batch task list"),
    user_personal(_m("GET", "PUT"), "/api/crm/user-prefs", "own page state"),

    # Campaign work. Campaign-users can create, update, run, and maintain campaigns.
    campaign_work(_m("POST", "PATCH", "DELETE"), "/api/crm/campaigns/*"),
    campaign_work(_m("POST", "PATCH"), "/api/crm/filter-facets/*"),
    campaign_work(_m("POST"), "/api/crm/filter-facets/*/create-campaign"),
    campaign_work(_m("POST"), "/api/crm/leads/by-domain/*/exclude"),
    campaign_work(_m("POST"), "/api/crm/name-enrich"),
    campaign_work(_m("GET"), "/api/crm/discover-campaigns"),

    # Campaign jobs and Smart Mail triggers.
    campaign_work(_m("GET"), "/api/crm/contact-sync"),
    campaign_work(_m("GET"), "/api/crm/push-and-sync"),
    campaign_work(_m("GET"), "/api/crm/template-sync"),
    campaign_work(_m("GET"), "/api/crm/crm-sync"),
    campaign_work(_m("GET"), "/api/crm/campaign-sync"),
    campaign_work(_m("GET"), "/api/crm/campaign-export"),

    campaign_work(_m("POST"), "/api/crm/inbound-read"),
    campaign_work(_m("POST"), "/api/crm/inbound_read"),

    # Campaign-user operational tools.
    campaign_work(_m("POST"), "/api/crm/gdisk/files"),
    campaign_work(_m("DELETE"), "/api/crm/gdisk/files/*"),
    campaign_work(_m("PUT", "DELETE"), "/api/crm/mailbox-tags/*"),
    campaign_work(_m("POST"), "/api/crm/statistics/collect"),

    # Cloud Batch is campaign work, not general read access.
    campaign_work(_m("PATCH"), "/api/crm/batch/jobs/*"),
    campaign_work(_m("POST"), "/api/crm/batch/jobs/*/run"),
    campaign_work(_m("POST"), "/api/crm/batch/jobs/*/tasks"),
    campaign_work(_m("PATCH", "DELETE"), "/api/crm/batch/jobs/*/tasks/*"),
    campaign_work(_m("POST"), "/api/crm/batch/jobs/*/tasks/*/run"),
    campaign_work(_m("POST"), "/api/crm/batch/sync-schedulers"),

    # Admin-only settings and user administration.
    admin_only(_m("GET", "POST", "PUT", "PATCH", "DELETE"), "/api/crm/settings/*", "settings"),
    admin_only(_m("GET", "POST", "PATCH"), "/api/crm/gdisk/settings", "settings"),
    admin_only(_m("GET", "PATCH", "DELETE"), "/api/crm/auth/users*", "user administration"),
)


def _normalize_path(path: str) -> str:
    return (path or "/").rstrip("/") or "/"


def _path_matches(pattern: str, path: str) -> bool:
    if pattern == "*":
        return True
    return fnmatchcase(path, pattern)


@lru_cache(maxsize=512)
def find_api_rule(method: str, path: str) -> ApiRule | None:
    method = method.upper()
    path = _normalize_path(path)

    for rule in API_RULES:
        if method not in rule.methods and "*" not in rule.methods:
            continue
        if _path_matches(rule.path, path):
            return rule
    return None
