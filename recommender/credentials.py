"""Server-side brapi credentials: environment first, Windows DPAPI fallback."""

from functools import lru_cache
import os
from pathlib import Path
import subprocess


@lru_cache(maxsize=1)
def brapi_token():
    token = os.environ.get("BRAPI_API_KEY", "").strip()
    if token:
        return token
    credential = Path(__file__).resolve().parents[1] / ".sites-runtime/brapi.credential.xml"
    if os.name != "nt" or not credential.is_file():
        return None
    command = "$c=Import-Clixml -LiteralPath $env:RF_BRAPI_CREDENTIAL_FILE;[Console]::Write($c.GetNetworkCredential().Password)"
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
                            env={**os.environ, "RF_BRAPI_CREDENTIAL_FILE": str(credential)},
                            capture_output=True, text=True, timeout=20,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:
        raise ValueError("Falha ao abrir a credencial local da brapi. Configure-a novamente.")
    return result.stdout.strip() or None
