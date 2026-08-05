"""Datasource package for BotPlatform — database connectivity as agent data sources."""

from src.core.datasource.errors import DataSourceError
from src.core.datasource.service import DataSourceService

__all__ = ["DataSourceError", "DataSourceService"]
