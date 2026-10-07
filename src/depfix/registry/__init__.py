"""npm and PyPI registry HTTP clients."""

from depfix.registry.client import (
    NpmRegistryClient,
    PackageNotFoundError,
    RegistryError,
    RegistryUnavailableError,
)
from depfix.registry.models import PackageMetadata
from depfix.registry.pypi_client import (
    DEFAULT_PYPI_URL,
    PypiPackageNotFoundError,
    PypiRegistryClient,
    PypiRegistryError,
    PypiUnavailableError,
)
from depfix.registry.pypi_models import PypiPackageMetadata

__all__ = [
    "DEFAULT_PYPI_URL",
    "NpmRegistryClient",
    "PackageMetadata",
    "PackageNotFoundError",
    "PypiPackageMetadata",
    "PypiPackageNotFoundError",
    "PypiRegistryClient",
    "PypiRegistryError",
    "PypiUnavailableError",
    "RegistryError",
    "RegistryUnavailableError",
]
