"""Shared constants (no heavy imports)."""
# Sev0..Sev4 -> human label (matches Azure portal)
SEV_LABEL = {
    "Sev0": "Critical",
    "Sev1": "Error",
    "Sev2": "Warning",
    "Sev3": "Informational",
    "Sev4": "Verbose",
}
# label -> ordinal (0 worst)
SEV_ORD = {"Critical": 0, "Error": 1, "Warning": 2, "Informational": 3, "Verbose": 4}
