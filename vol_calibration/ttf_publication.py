"""Dashboard authorization and immutable-content cache for publication services."""

from options.vol_calibration.publication import service as _implementation
from options.vol_calibration.api import load_publication_contents
from vol_calibration.auth import Permission, authorize
from workspace_cache import WorkspaceLoadCache
from source_identity import source_config_fingerprint

__all__ = _implementation.__all__
_PUBLICATION_CONTENT_CACHE = WorkspaceLoadCache(max_entries=8)


def __getattr__(name):
    return getattr(_implementation, name)


def _cached_publication_contents(engine, publication, configuration, *, force_refresh):
    key = (str(id(engine)), str(publication["publication_id"]),
           source_config_fingerprint() + "|governed-grid-v1")
    return _PUBLICATION_CONTENT_CACHE.get_or_load(
        key,
        lambda: load_publication_contents(
            engine, publication, configuration, include_surface=True,
        ),
        force_refresh=force_refresh, degraded=lambda value: False,
        healthy_ttl_seconds=3600, degraded_ttl_seconds=1,
    )


def load_latest_ttf_publication(*args, **kwargs):
    kwargs["contents_loader"] = _cached_publication_contents
    return _implementation.load_latest_ttf_publication(*args, **kwargs)


def load_latest_hybrid_publication(engine, trading_date, *, commodity, **kwargs):
    return load_latest_ttf_publication(engine, trading_date, commodity=commodity, **kwargs)


def publish_ttf_surface(engine, surface, expiry_results, *, identity, **kwargs):
    authorize(identity, Permission.PUBLISH)
    return _implementation.publish_ttf_surface(
        engine, surface, expiry_results, actor_subject=identity.subject, **kwargs,
    )


def publish_hybrid_surface(engine, surface, expiry_results, *, commodity, **kwargs):
    return publish_ttf_surface(engine, surface, expiry_results, commodity=commodity, **kwargs)
