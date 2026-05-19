from dataclasses import dataclass

from django.core.cache import cache
from django.db import connections
from django.db.utils import OperationalError


_shutting_down = False


def set_shutting_down():
    global _shutting_down
    _shutting_down = True


def is_shutting_down():
    return _shutting_down


@dataclass
class DependencyStatus:
    ok: bool
    detail: str


def database_ready():
    try:
        with connections["default"].cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
        return DependencyStatus(True, "database reachable")
    except OperationalError as exc:
        return DependencyStatus(False, str(exc))


def cache_ready():
    try:
        cache.set("healthcheck", "ok", timeout=5)
        if cache.get("healthcheck") != "ok":
            return DependencyStatus(False, "cache round-trip failed")
        return DependencyStatus(True, "cache reachable")
    except Exception as exc:
        return DependencyStatus(False, str(exc))
