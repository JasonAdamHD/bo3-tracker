# Builds the image and pushes it to a registry.
# Usage:  .\publish.ps1 ghcr.io/YOUR_GITHUB_USERNAME/bo3-tracker
# Log in once first:  docker login ghcr.io   (username + a GitHub token, see README)
param(
    [Parameter(Mandatory = $true)][string]$Image,
    [string]$Tag = "latest"
)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$full = "${Image}:${Tag}"
Write-Host "Building $full ..."
docker build --platform linux/amd64 -t $full .
if ($LASTEXITCODE -ne 0) { throw "Build failed" }

Write-Host "Pushing $full ..."
docker push $full
if ($LASTEXITCODE -ne 0) { throw "Push failed. Did you run 'docker login' for this registry?" }

Write-Host "Done. In Unraid, use repository: $full"
