"""dockai: natural-language Docker management over MCP."""

__version__ = "0.1.0"

# Every resource dockai creates carries this label; destructive tools only touch labelled resources.
MANAGED_LABEL_KEY = "managed-by"
MANAGED_LABEL_VALUE = "dockai"
MANAGED_LABELS = {MANAGED_LABEL_KEY: MANAGED_LABEL_VALUE}
