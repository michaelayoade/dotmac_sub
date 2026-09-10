"""Stateless optional-module declaration for document rendering."""

from dotmac_kernel.modules import ModuleManifest

module = ModuleManifest(
    code="document_rendering",
    version="0.1.0a1",
    core=False,
    # Deliberately no short_code, migration prefix, tables or plane declaration.
    # Scope is input data; one behavior serves both security planes.
)

__all__ = ["module"]
