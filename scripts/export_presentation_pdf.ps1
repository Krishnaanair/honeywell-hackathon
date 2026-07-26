param(
    [Parameter(Mandatory = $true)]
    [string]$InputPath,

    [Parameter(Mandatory = $true)]
    [string]$OutputPath
)

$ErrorActionPreference = "Stop"
$presentation = $null
$powerPoint = $null

try {
    $resolvedInput = (Resolve-Path -LiteralPath $InputPath).Path
    $resolvedOutput = [System.IO.Path]::GetFullPath($OutputPath)
    $outputParent = [System.IO.Path]::GetDirectoryName($resolvedOutput)
    [System.IO.Directory]::CreateDirectory($outputParent) | Out-Null

    $powerPoint = New-Object -ComObject PowerPoint.Application
    $presentation = $powerPoint.Presentations.Open(
        $resolvedInput,
        $true,
        $false,
        $false
    )
    # 32 is ppSaveAsPDF in the PowerPoint object model.
    $presentation.SaveAs($resolvedOutput, 32)
}
finally {
    if ($null -ne $presentation) {
        $presentation.Close()
        [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($presentation)
    }
    if ($null -ne $powerPoint) {
        $powerPoint.Quit()
        [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($powerPoint)
    }
    [GC]::Collect()
    [GC]::WaitForPendingFinalizers()
}

if (-not (Test-Path -LiteralPath $OutputPath -PathType Leaf)) {
    throw "PowerPoint exited without producing the requested PDF: $OutputPath"
}
