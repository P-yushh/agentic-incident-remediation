"""Errors shared across schema tool transports and consumers."""


class SchemaToolError(RuntimeError):
    """Base error for schema selection, lookup, discovery, or execution failures."""


class SchemaLookupError(SchemaToolError):
    """Raised when an unsupported or malformed schema query is requested."""


class SchemaMCPError(SchemaToolError):
    """Raised when schema discovery or MCP execution returns an unusable result."""
