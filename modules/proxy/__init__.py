"""Proxy pool acquisition and health reporting."""

from .pool import ProxyPoolAdapter, create_application_proxy_pool_adapter

__all__ = ["ProxyPoolAdapter", "create_application_proxy_pool_adapter"]
