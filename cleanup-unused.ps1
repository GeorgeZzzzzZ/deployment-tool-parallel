[CmdletBinding(SupportsShouldProcess = $true)]
param()

$ErrorActionPreference = 'Stop'
$cleanupRoot = 'D:\AI\tool\deployment-tool-parallel'
$legacyRoot = 'C:\Users\15706\Desktop\deployment-tool'
if ((Resolve-Path -LiteralPath $PSScriptRoot).Path -ne $cleanupRoot) {
    throw 'Run this script only from the approved project directory.'
}

$relativeTargets = @(
    '__pycache__', '.ui-review', 'build', 'dist', 'dist-tray-fix'
    'release\cloud-target-update', 'release\ui-refined', '.agents'
    'ui-batch-check.png', 'ui-batch-forced-check.png', 'ui-batch-ssh-check.png'
)
$candidates = @($relativeTargets | ForEach-Object { Join-Path $cleanupRoot $_ })
$candidates += $legacyRoot
$validatedTargets = @()

# Resolve and validate all paths before deleting any files.
foreach ($candidate in $candidates) {
    if (-not (Test-Path -LiteralPath $candidate)) { continue }
    $target = (Resolve-Path -LiteralPath $candidate).Path
    if ($target -ne $legacyRoot -and
        -not $target.StartsWith($cleanupRoot + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw "Target outside the approved directories: $target"
    }
    if ($target -eq $legacyRoot -and (Test-Path -LiteralPath (Join-Path $target '.git'))) {
        throw 'The legacy directory now contains Git history; review it before deletion.'
    }
    $item = Get-Item -LiteralPath $target -Force
    $ancestor = $item
    while ($null -ne $ancestor) {
        if ($ancestor.Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw "Filesystem link detected: $($ancestor.FullName)"
        }
        if ($ancestor.PSIsContainer) { $ancestor = $ancestor.Parent }
        else { $ancestor = $ancestor.Directory }
    }
    $entries = if ($item.PSIsContainer) { @(Get-ChildItem -LiteralPath $target -Force -Recurse) } else { @($item) }
    if (@($entries | Where-Object { $_.Attributes -band [IO.FileAttributes]::ReparsePoint }).Count) {
        throw "Filesystem link inside target: $target"
    }
    $files = @($entries | Where-Object { -not $_.PSIsContainer })
    $validatedTargets += [PSCustomObject]@{
        Path = $target
        Files = $files.Count
        Bytes = [long](($files | Measure-Object Length -Sum).Sum)
    }
}

$protectedFiles = @(Get-ChildItem -LiteralPath $cleanupRoot -Force -File |
    Where-Object { $_.FullName -notin $validatedTargets.Path })
$protectedFiles += @(Get-ChildItem -LiteralPath (Join-Path $cleanupRoot 'assets'),
    (Join-Path $cleanupRoot 'design-system') -Recurse -File)
$releaseFile = Join-Path $cleanupRoot 'release\DeployConsoleBatchSafe.exe'
$protectedFiles += Get-Item -LiteralPath $releaseFile
$protectedHashes = @{}
foreach ($file in $protectedFiles) {
    $protectedHashes[$file.FullName] = (Get-FileHash -LiteralPath $file.FullName).Hash
}
if ($protectedHashes[$releaseFile] -ne 'D3AD0FF19B9D364CA90E1CDD1334955CA90DE240F1FE12B93EDADC9F814F5006') {
    throw 'Release package changed since this cleanup plan; review before proceeding.'
}
if (-not (Test-Path -LiteralPath (Join-Path $cleanupRoot '.git'))) {
    throw 'The current project Git directory is missing.'
}

$plannedBytes = ($validatedTargets | Measure-Object Bytes -Sum).Sum
$plannedFiles = ($validatedTargets | Measure-Object Files -Sum).Sum
Write-Output ('Validated {0} files, {1:N2} MiB.' -f $plannedFiles, ($plannedBytes / 1MB))
$validatedTargets | Select-Object Path, Files, @{Name = 'MiB'; Expression = { [math]::Round($_.Bytes / 1MB, 2) }}
foreach ($target in $validatedTargets) {
    if ($PSCmdlet.ShouldProcess($target.Path, 'Delete obsolete project files')) {
        Remove-Item -LiteralPath $target.Path -Recurse -Force
        if (Test-Path -LiteralPath $target.Path) { throw "Deletion incomplete: $($target.Path)" }
    }
}
foreach ($protectedPath in $protectedHashes.Keys) {
    if ((Get-FileHash -LiteralPath $protectedPath).Hash -ne $protectedHashes[$protectedPath]) {
        throw "Retained file changed: $protectedPath"
    }
}
Write-Output ('Verified {0} retained files unchanged.' -f $protectedHashes.Count)
if ($WhatIfPreference) { Write-Output 'Preview only; no files were deleted.' }
