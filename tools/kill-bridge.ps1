$p = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
     Where-Object { $_.CommandLine -match 'pilot\.py' }
if ($p) {
    $p | ForEach-Object {
        Write-Output ("killing bridge pid " + $_.ProcessId)
        Stop-Process -Id $_.ProcessId -Force
    }
} else {
    Write-Output "no bridge running"
}
