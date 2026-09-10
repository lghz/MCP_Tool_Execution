# Run this script yourself in a local PowerShell window.
# It creates a local .env file. It makes no network requests and never prints keys.
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$taskProjectRoot = Split-Path -Parent $PSScriptRoot
$taskEnvPath = Join-Path $taskProjectRoot '.env'
if (Test-Path -LiteralPath $taskEnvPath) {
    throw 'An .env file already exists. No keys were requested or overwritten.'
}

$taskNames = @('OPENAI_API_KEY', 'ANTHROPIC_API_KEY')
$taskValues = New-Object 'System.Collections.Generic.List[string]'
$taskStream = $null
$taskPayload = $null
try {
    foreach ($taskName in $taskNames) {
        $taskSecret = Read-Host -Prompt "Enter $taskName (hidden)" -AsSecureString
        $taskPointer = [IntPtr]::Zero
        $taskPlain = $null
        try {
            $taskPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($taskSecret)
            $taskPlain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($taskPointer)
            if ([string]::IsNullOrWhiteSpace($taskPlain) -or $taskPlain -match '\s|["''#]') {
                throw 'Key input is empty or contains unexpected whitespace/quotes. Nothing was saved.'
            }
            $taskValues.Add("${taskName}=$taskPlain")
        }
        finally {
            if ($taskPointer -ne [IntPtr]::Zero) {
                [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($taskPointer)
            }
            $taskPlain = $null
            if ($null -ne $taskSecret) { $taskSecret.Dispose() }
        }
    }
    $taskEncoding = New-Object System.Text.UTF8Encoding($false)
    $taskPayload = $taskEncoding.GetBytes(($taskValues -join "`n") + "`n")
    $taskStream = [System.IO.File]::Open($taskEnvPath, [System.IO.FileMode]::CreateNew,
        [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
    $taskStream.Write($taskPayload, 0, $taskPayload.Length)
    $taskStream.Flush()
    Write-Host 'Saved two keys to the project .env file. No API calls were made.'
}
finally {
    if ($null -ne $taskStream) { $taskStream.Dispose() }
    if ($null -ne $taskPayload) { [Array]::Clear($taskPayload, 0, $taskPayload.Length) }
    $taskValues.Clear()
}
