<#
.SYNOPSIS
    Regenerates docs/*.png and docs/demo.gif from the fictitious demo.
.DESCRIPTION
    Builds the static demo with tools/build_demo.py (file:// links), captures
    several views with headless Microsoft Edge and joins them with FFmpeg
    cross-fades into a 960-pixel-wide GIF. Then renders docs/social-preview.png
    from tools/social-preview.html.
    Requires the project's virtual environment (.venv), Microsoft Edge and FFmpeg on PATH.
#>
param([string] $Docs = (Join-Path $PSScriptRoot '..\docs'))

$ErrorActionPreference = 'Stop'
$root = Resolve-Path (Join-Path $PSScriptRoot '..')
$python = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) { $python = 'python' }
$edge = @("${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe",
          "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe") |
    Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $edge) { throw 'Microsoft Edge not found' }
New-Item -ItemType Directory -Force -Path $Docs | Out-Null
$Docs = (Resolve-Path $Docs).Path

$work = Join-Path ([IO.Path]::GetTempPath()) ("deskscope-shots-" + [guid]::NewGuid())
New-Item -ItemType Directory -Path $work | Out-Null
try {
    $site = Join-Path $work 'site'
    & $python (Join-Path $root 'tools\build_demo.py') $site --file-urls | Out-Null
    if ($LASTEXITCODE) { throw "build_demo.py failed ($LASTEXITCODE)" }

    # The PCScope disk ticket (its number depends on the generated history).
    $disk = Get-ChildItem -Path (Join-Path $site 'en\carlos.ruiz\tickets') -Recurse -Filter index.html |
        Where-Object { (Get-Content $_.FullName -Raw -Encoding UTF8) -match '<title>#\d+ OPS-PC04: Drive C' } |
        Select-Object -First 1
    if (-not $disk) { throw 'PCScope demo ticket not found' }

    function Get-PageUrl([string] $relative) {
        'file:///' + ((Join-Path $site $relative) -replace '\\', '/')
    }

    # name, page, window height
    $views = @(
        @('tickets', 'en\carlos.ruiz\tickets\index.html', 760),
        @('ticket', $disk.FullName.Substring($site.Length + 1), 1900),
        @('reports', 'en\laura.sanchez\reports\index.html', 1000),
        @('kb', 'en\ana.garcia\kb\index.html', 760),
        @('tickets-es', 'es\carlos.ruiz\tickets\index.html', 760)
    )
    $frames = @()
    foreach ($view in $views) {
        $png = Join-Path $Docs "$($view[0]).png"
        Start-Process -FilePath $edge -Wait -WindowStyle Hidden -ArgumentList @(
            '--headless=new', '--disable-gpu', '--hide-scrollbars', '--force-device-scale-factor=1',
            "--user-data-dir=$work\profile", "--window-size=1280,$($view[2])",
            "--screenshot=$png", (Get-PageUrl $view[1]))
        if (-not (Test-Path $png)) { throw "screenshot failed: $($view[0])" }
        # GIF frames are the top 1280x760 of each page.
        $frame = Join-Path $work "frame-$($view[0]).png"
        & ffmpeg -v error -y -i $png -vf 'crop=1280:760:0:0' $frame
        $frames += $frame
    }

    # Each view shows for $hold seconds and cross-fades into the next one in $fade seconds.
    $hold, $fade = 2.6, 0.4
    $inputs = foreach ($frame in $frames) { '-loop', '1', '-t', "$hold", '-i', $frame }
    $chain, $last = @(), '[0:v]'
    for ($i = 1; $i -lt $frames.Count; $i++) {
        $offset = [string]::Format([Globalization.CultureInfo]::InvariantCulture, '{0:0.0#}', $i * ($hold - $fade))
        $chain += "$last[$($i):v]xfade=transition=fade:duration=$($fade):offset=$offset[x$i]"
        $last = "[x$i]"
    }
    $filter = ($chain -join ';') + ";$($last)fps=12,scale=960:-1:flags=lanczos,split[a][b];" +
              '[a]palettegen=max_colors=128[p];[b][p]paletteuse=dither=sierra2_4a'
    $gif = Join-Path $Docs 'demo.gif'
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    & ffmpeg -v error -y @inputs -filter_complex $filter -loop 0 $gif
    $ErrorActionPreference = $previous
    if ($LASTEXITCODE) { throw "ffmpeg failed ($LASTEXITCODE)" }

    $preview = Join-Path $Docs 'social-preview.png'
    $source = 'file:///' + ((Join-Path $root 'tools\social-preview.html') -replace '\\', '/')
    Start-Process -FilePath $edge -Wait -WindowStyle Hidden -ArgumentList @(
        '--headless=new', '--disable-gpu', '--hide-scrollbars', '--force-device-scale-factor=1',
        "--user-data-dir=$work\profile", '--window-size=1280,640', "--screenshot=$preview", $source)

    Get-ChildItem $Docs | Select-Object Name, Length
}
finally {
    Remove-Item -Recurse -Force $work -ErrorAction SilentlyContinue
}
