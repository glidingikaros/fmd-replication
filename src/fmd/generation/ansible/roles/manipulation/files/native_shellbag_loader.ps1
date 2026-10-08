
$fmdNativeShellbagTemporaryNames = @(
    "LocalNativeShellbagPayloadBase64",
    "LocalNativeShellbagExpectedSha256",
    "fmdNativeShellbagPayloadBytes",
    "fmdNativeShellbagPayloadStream",
    "fmdNativeShellbagGzipStream",
    "fmdNativeShellbagExpandedStream",
    "fmdNativeShellbagHelperBytes",
    "fmdNativeShellbagSha256",
    "fmdNativeShellbagActualSha256",
    "fmdNativeShellbagUtf8",
    "fmdNativeShellbagSource",
    "LocalNativeShellbagScriptBlock"
)

try {
    if ([string]::IsNullOrWhiteSpace([string]$LocalNativeShellbagPayloadBase64)) {
        throw "Native Shellbag transport payload is missing"
    }
    if ([string]::IsNullOrWhiteSpace([string]$LocalNativeShellbagExpectedSha256)) {
        throw "Native Shellbag transport hash is missing"
    }

    $fmdNativeShellbagPayloadBytes = [Convert]::FromBase64String(
        [string]$LocalNativeShellbagPayloadBase64
    )
    $fmdNativeShellbagPayloadStream = [IO.MemoryStream]::new(
        [byte[]]$fmdNativeShellbagPayloadBytes,
        $false
    )
    $fmdNativeShellbagGzipStream = [IO.Compression.GZipStream]::new(
        $fmdNativeShellbagPayloadStream,
        [IO.Compression.CompressionMode]::Decompress,
        $false
    )
    $fmdNativeShellbagExpandedStream = [IO.MemoryStream]::new()
    $fmdNativeShellbagGzipStream.CopyTo($fmdNativeShellbagExpandedStream)
    $fmdNativeShellbagHelperBytes = $fmdNativeShellbagExpandedStream.ToArray()

    $fmdNativeShellbagSha256 = [Security.Cryptography.SHA256]::Create()
    try {
        $fmdNativeShellbagActualSha256 = -join @(
            $fmdNativeShellbagSha256.ComputeHash(
                [byte[]]$fmdNativeShellbagHelperBytes
            ) | ForEach-Object { $_.ToString("x2") }
        )
    } finally {
        $fmdNativeShellbagSha256.Dispose()
    }
    if (-not [String]::Equals(
        $fmdNativeShellbagActualSha256,
        ([string]$LocalNativeShellbagExpectedSha256).Trim().ToLowerInvariant(),
        [StringComparison]::Ordinal
    )) {
        throw "Native Shellbag transport hash verification failed"
    }

    $fmdNativeShellbagUtf8 = [Text.UTF8Encoding]::new($false, $true)
    $fmdNativeShellbagSource = $fmdNativeShellbagUtf8.GetString(
        [byte[]]$fmdNativeShellbagHelperBytes
    )
    $LocalNativeShellbagScriptBlock = [ScriptBlock]::Create(
        $fmdNativeShellbagSource
    )
    . $LocalNativeShellbagScriptBlock
} finally {
    if ($null -ne $fmdNativeShellbagGzipStream) {
        $fmdNativeShellbagGzipStream.Dispose()
    }
    if ($null -ne $fmdNativeShellbagPayloadStream) {
        $fmdNativeShellbagPayloadStream.Dispose()
    }
    if ($null -ne $fmdNativeShellbagExpandedStream) {
        $fmdNativeShellbagExpandedStream.Dispose()
    }
    foreach ($fmdNativeShellbagTemporaryName in $fmdNativeShellbagTemporaryNames) {
        Clear-Variable -Name $fmdNativeShellbagTemporaryName `
            -ErrorAction SilentlyContinue
        Remove-Variable -Name $fmdNativeShellbagTemporaryName `
            -ErrorAction SilentlyContinue
    }
    Clear-Variable -Name fmdNativeShellbagTemporaryNames `
        -ErrorAction SilentlyContinue
    Remove-Variable -Name fmdNativeShellbagTemporaryNames `
        -ErrorAction SilentlyContinue
    Clear-Variable -Name fmdNativeShellbagTemporaryName `
        -ErrorAction SilentlyContinue
    Remove-Variable -Name fmdNativeShellbagTemporaryName `
        -ErrorAction SilentlyContinue
}
