Write-Host '=== crash.log search ==='
$cands = @(
    "$env:LOCALAPPDATA\Mynah\crash.log",
    "$HOME\.local\share\Mynah\crash.log",
    "$PWD\crash.log",
    "$PWD\dist\Mynah\crash.log"
)
foreach ($c in $cands) {
    if (Test-Path $c) {
        Write-Host "FOUND: $c"
        Get-Item $c | Select-Object FullName, LastWriteTime, Length | Format-List
        Write-Host '--- contents ---'
        Get-Content $c
        Write-Host '----------------'
    }
}
Write-Host ''
Write-Host '=== newest recording dirs (top 3) ==='
Get-ChildItem 'Recordings' -Directory -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending |
    Select-Object -First 3 |
    ForEach-Object {
        Write-Host ('--- ' + $_.Name + ' (' + $_.LastWriteTime + ') ---')
        Get-ChildItem $_.FullName | Select-Object Name, Length, LastWriteTime | Format-Table -AutoSize | Out-String | Write-Host
    }
Write-Host '=== process ==='
$p = Get-Process Mynah -ErrorAction SilentlyContinue
if ($p) { $p | Select-Object Id, Responding } else { Write-Host '(not running)' }
