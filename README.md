# Starkins Dashboard

Windows dashboard built with PyQt6. A GitHub Actions workflow builds and publishes a new Windows executable whenever application source changes are pushed to `main`. The app checks the latest stable release on startup, verifies the downloaded executable with SHA-256, caches it in `%LOCALAPPDATA%`, and starts the updated copy. No Python installation is needed to run the published executable.

## Automatic update flow

1. Push an application change to `main`.
2. GitHub Actions builds `StarkinsDashboard.exe`, assigns an increasing `1.2.<run>` version, and publishes a GitHub Release containing the executable and checksum.
3. The updater-enabled app downloads and verifies the release at next startup. The first updater-enabled executable must be installed once; subsequent releases update automatically.

## Run from source

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt pyinstaller==6.22.3
python main.py
```

## Build locally

Run `build.bat` from this folder. Local builds use the version in `version.py`. For automatic releases, push changes to the `main` branch instead; GitHub Actions builds and publishes the downloadable app.

NVIDIA telemetry uses `nvidia-ml-py` when supported. The dashboard remains usable if NVIDIA monitoring or sensor data is unavailable.

