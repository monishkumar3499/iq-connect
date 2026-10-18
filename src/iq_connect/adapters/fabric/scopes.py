"""The Microsoft resources Fabric work needs tokens for (one resource per token)."""

FABRIC_API_SCOPE = "https://api.fabric.microsoft.com/.default"
# The double slash is intentional: it is the documented form for the SQL
# resource "https://database.windows.net/" plus "/.default".
SQL_SCOPE = "https://database.windows.net//.default"
POWERBI_SCOPE = "https://analysis.windows.net/powerbi/api/.default"
