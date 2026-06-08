$cutoff = (Get-Date).AddMinutes(-30)
Get-WinEvent -LogName Application -MaxEvents 200 -ErrorAction SilentlyContinue |
    Where-Object {
        $_.TimeCreated -gt $cutoff -and
        ($_.LevelDisplayName -in @('Error','Warning','Critical')) -and
        ($_.Message -match 'Mynah|python|libsndfile|portaudio|ffmpeg' -or
         $_.ProviderName -match 'Application Error|Windows Error Reporting|\.NET Runtime')
    } |
    Select-Object TimeCreated, ProviderName, Id, LevelDisplayName,
        @{n='Msg';e={ $_.Message.Substring(0,[Math]::Min(500,$_.Message.Length)) }} |
    Format-List
