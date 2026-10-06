"""
Cache infrastructure.

``resilient.ResilientRedisCache`` is the backend ``core.settings`` configures
whenever REDIS_URL is set. See its docstring for what degrades and what does
not, and the "Cache and Redis" section of CLAUDE.md for the product-level
contract.
"""
