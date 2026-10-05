param(
    [Parameter(Mandatory = $true)][string]$Root,
    [Parameter(Mandatory = $true)][string]$Relative,
    [Parameter(Mandatory = $true)][long]$ExpectedBytes,
    [Parameter(Mandatory = $true)][decimal]$ExpectedMtimeNs,
    [Parameter(Mandatory = $true)][string]$ExpectedSha256
)
$ErrorActionPreference = 'Stop'
$rootPath = [IO.Path]::GetFullPath($Root).TrimEnd('\', '/')
if ($Relative.Contains('\') -or $Relative.Contains(':') -or $Relative.StartsWith('/')) {
    throw 'Invalid relative retention path'
}
foreach ($part in $Relative.Split('/')) {
    if (-not $part -or $part -eq '.' -or $part -eq '..' -or $part.EndsWith('.') -or $part.EndsWith(' ')) {
        throw 'Invalid retention path component'
    }
}
$targetPath = [IO.Path]::GetFullPath((Join-Path $rootPath $Relative))
if (-not $targetPath.StartsWith($rootPath + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Retention target escaped companion'
}
function Assert-Snapshot {
    $node = $targetPath
    while ($node) {
        $entry = Get-Item -LiteralPath $node -Force
        if ($entry.Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw 'Retention refuses reparse points'
        }
        if ($entry.PSIsContainer -and $node -ne $rootPath -and
            $node.StartsWith($rootPath + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase) -and
            (Test-Path -LiteralPath (Join-Path $node '.git'))) {
            throw 'Retention refuses nested repositories'
        }
        $node = [IO.Path]::GetDirectoryName($node)
    }
    $target = Get-Item -LiteralPath $targetPath -Force
    if ($null -eq $target.PSObject.Properties['LinkType'] -or $target.LinkType) {
        throw 'Retention requires an ordinary file with verified link metadata'
    }
    $mtimeNs = ([decimal]$target.LastWriteTimeUtc.Ticks - 621355968000000000) * 100
    if ($target.PSIsContainer -or $target.Length -ne $ExpectedBytes -or $mtimeNs -ne $ExpectedMtimeNs) {
        throw 'Retention file snapshot changed'
    }
}
Assert-Snapshot
$stream = [IO.File]::OpenRead($targetPath)
$hasher = [Security.Cryptography.SHA256]::Create()
try {
    $actualHash = [BitConverter]::ToString($hasher.ComputeHash($stream)).Replace('-', '')
}
finally {
    $hasher.Dispose()
    $stream.Dispose()
}
if ($actualHash -ne $ExpectedSha256) {
    throw 'Retention file hash changed'
}
Assert-Snapshot
Remove-Item -LiteralPath $targetPath -Force
