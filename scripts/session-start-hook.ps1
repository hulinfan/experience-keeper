# DSH SessionStart hook: emit AGENTS.md + the experience-library opening line as
# hookSpecificOutput JSON.
#
# Why this exists: `experience.py prompt` takes ~1.5s per run, ~1.2s of which is
# the skill shelling out `git ls-files` once per registered asset (11 subprocess
# x ~107ms). It ran on every session start. This script caches that one line per
# project and only recomputes it past the TTL; AGENTS.md is always read live.
#
# Budget: spawning Windows PowerShell for a hook already costs ~0.7s, so the body
# below stays on single syscalls per file and avoids crypto/subprocess work on
# the cached path.
#
# Output contract: stdout carries exactly one line of compressed JSON. Anything
# else (warnings, errors) goes to stderr.
#
# ASCII-only on purpose: Windows PowerShell reads a BOM-less script using the
# ANSI code page, so non-ASCII comments would be mis-decoded and break parsing.

$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false) } catch { }

$agentsFile = Join-Path $env:USERPROFILE '.agents\AGENTS.md'
$skillEntry = Join-Path $env:USERPROFILE '.agents\skills\experience-keeper\scripts\experience.py'
$ttlSeconds = 1800

# Same meaning as the skill's find_project_root: nearest ancestor holding
# .git/.hg/.svn, else the starting directory. The path also seeds the cache file
# name, so no hashing is needed.
function Get-ProjectRoot([string]$Start) {
    $dir = $Start
    for ($i = 0; $i -lt 64; $i++) {
        foreach ($marker in '.git', '.hg', '.svn') {
            if (Test-Path -LiteralPath (Join-Path $dir $marker)) { return $dir }
        }
        $parent = [System.IO.Path]::GetDirectoryName($dir)
        if (-not $parent -or $parent -eq $dir) { break }
        $dir = $parent
    }
    return $Start
}

function Get-CachePath([string]$Root) {
    $home2 = if ($env:EXPERIENCE_HOME) { $env:EXPERIENCE_HOME } else { Join-Path $env:USERPROFILE '.agents\experience' }
    $leaf = ($Root.ToLowerInvariant() -replace '[^a-z0-9._-]', '_').Trim('_')
    if ($leaf.Length -gt 120) { $leaf = $leaf.Substring($leaf.Length - 120) }
    return (Join-Path (Join-Path $home2 'cache\session-start') ($leaf + '.txt'))
}

function Get-CachedPrompt([string]$CachePath) {
    $written = $null
    try { $written = [System.IO.File]::GetLastWriteTimeUtc($CachePath) } catch { }
    if ($written -and (((Get-Date).ToUniversalTime() - $written).TotalSeconds -lt $ttlSeconds)) {
        return [System.IO.File]::ReadAllText($CachePath, [System.Text.Encoding]::UTF8)
    }

    $text = $null
    try {
        if (Test-Path -LiteralPath $skillEntry) {
            $text = & python $skillEntry prompt 2>$null | Out-String
        }
    } catch {
        Write-Warning "[dsh] experience prompt failed: $($_.Exception.Message)"
    }

    if ($text -and $text.Trim() -ne '') {
        try {
            $dir = [System.IO.Path]::GetDirectoryName($CachePath)
            if (-not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
            $tmp = "$CachePath.$PID.tmp"
            [System.IO.File]::WriteAllText($tmp, $text, (New-Object System.Text.UTF8Encoding($false)))
            Move-Item -LiteralPath $tmp -Destination $CachePath -Force
        } catch {
            Write-Warning "[dsh] experience prompt cache write failed: $($_.Exception.Message)"
        }
        return $text
    }

    # Generation failed: fall back to the last known line rather than dropping
    # the context silently.
    if ($written) {
        try { return [System.IO.File]::ReadAllText($CachePath, [System.Text.Encoding]::UTF8) } catch { }
    }
    return ''
}

$cachePath = Get-CachePath (Get-ProjectRoot (Get-Location).Path)
$prompt = Get-CachedPrompt $cachePath

$agents = ''
try { $agents = [System.IO.File]::ReadAllText($agentsFile, [System.Text.Encoding]::UTF8) } catch { }

$payload = @{
    hookSpecificOutput = @{
        hookEventName     = 'SessionStart'
        additionalContext = ($agents + [Environment]::NewLine + $prompt)
    }
}

$payload | ConvertTo-Json -Depth 5 -Compress
