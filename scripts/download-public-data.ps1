param([Parameter(Mandatory=$true)][string]$Uri, [Parameter(Mandatory=$true)][string]$OutFile)
$ErrorActionPreference = 'Stop'
$parsed = [Uri]$Uri
if ($parsed.Scheme -ne 'https' -or $parsed.Host -notin @('www.tesourotransparente.gov.br', 'tesourotransparente.gov.br')) {
    throw 'Fonte de dados nao autorizada.'
}
# Use the Windows certificate store without disabling TLS certificate validation.
Invoke-WebRequest -Uri $Uri -OutFile $OutFile -TimeoutSec 120 -UseBasicParsing
