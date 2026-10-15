# Windows setup guide

This guide covers setting up Job Radar on Windows for local development and manual runs.

## What works on Windows?

Job Radar's main application is Python and uses SQLite, HTTP requests, YAML configuration, and FastAPI for its local web UI. These parts are not inherently macOS-only, but this repository's helper scripts and scheduled notifications are written for macOS.

- `install.sh`, `run.sh`, and `run-web.sh` are Bash scripts and are not directly executable in standard PowerShell.
- `install.sh` registers scheduled jobs with `launchctl` and `~/Library/LaunchAgents`. Windows Task Scheduler setup is not provided by this guide.
- macOS desktop banners are platform-specific. Email alerts require valid SMTP configuration.
- This guide describes a manual setup; it does not claim that every command or notification feature has been verified on Windows.

## Prerequisites

1. Install **Python 3.11 or newer** from [python.org](https://www.python.org/downloads/windows/). During installation, enable the option to add Python to PATH if available.
2. Install **Git for Windows** from [git-scm.com](https://git-scm.com/download/win).
3. Open PowerShell and check that both are available:

   ```powershell
   py --version
   git --version
   ```

   If `py` is not recognized, reopen PowerShell after installation or repair the Python installation/PATH.

## Get the source code

Clone the repository and enter its folder:

```powershell
git clone https://github.com/suvamneog/jobradar.git
Set-Location jobradar
```

For contributing changes, work in your own fork and a feature branch rather than committing directly to the maintainer's repository.

## Create a virtual environment

From the repository root, run:

```powershell
py -3.11 -m venv .venv
`.venv\Scripts\Activate.ps1`
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

If PowerShell blocks virtual-environment activation because of the execution policy, you can avoid activation and run the environment's Python directly:

```powershell
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

If the `py -3.11` launcher command is unavailable, use the installed Python version to create the environment, provided it is Python 3.11 or newer.

## Create your local configuration

Copy the example configuration without overwriting an existing personal configuration:

```powershell
if (-not (Test-Path config.yaml)) {
    Copy-Item config.example.yaml config.yaml
}
```

Open `config.yaml` and set your email, resume path, companies, and LLM provider settings. Keep your personal resume in the repository root only if that is the path you configure.

The CLI's default config path is the repository's root-level `config.yaml`. The application reports an actionable error if it is missing.

## Run the application manually

The repository's `run.sh` and `run-web.sh` wrappers depend on Bash and Unix paths. From PowerShell, use the virtual environment's Python directly to run a CLI command:

```powershell
.venv\Scripts\python.exe -m jobradar.main --help
.venv\Scripts\python.exe -m jobradar.main scan
```

The scan makes network requests to job boards. If the configured scoring provider uses an API, configure its environment variable in your current PowerShell session before scanning. For example:

```powershell
$env:XKIRO_API_KEY = "your-key"
```

Use the variable that matches your configured provider; do not paste real keys into source files, documentation, screenshots, or issue reports.

To start the local web UI, install dependencies first and run Uvicorn with the app target shown by the repository's `run-web.sh` script:

```powershell
.venv\Scripts\python.exe -m uvicorn jobradar.web.server:app --host 127.0.0.1 --port 8765
```

Then open [http://localhost:8765](http://localhost:8765) in your browser. Keep the server bound to `127.0.0.1` for local-only access.

## Run the existing tests

The project documents tests as individual Python scripts under `tests/test_*.py`. Run them using the environment's Python interpreter:

```powershell
Get-ChildItem tests\test_*.py | ForEach-Object {
    & .\.venv\Scripts\python.exe $_.FullName
    if ($LASTEXITCODE -ne 0) {
        throw "Test failed: $($_.Name)"
    }
}
```

This follows the repository's documented test approach and does not assume that pytest is configured as the test runner. A passing local run is useful evidence, but it does not confirm that macOS-only scheduling or notifications work on Windows.

## Scheduling and notifications

No Windows Task Scheduler configuration is included here. Until a Windows-specific scheduler is implemented and tested, run commands manually or adapt scheduling separately. Do not run the macOS `install.sh` on Windows expecting it to configure Windows services.

The project documents launchd scheduling and macOS desktop banners. Email delivery is a separate feature and depends on valid SMTP settings and network access.

## Keep secrets and personal data private

- Do not commit `config.yaml`, `run.sh`, API keys, Gmail App Passwords, database files, logs, or your resume.
- The repository's `.gitignore` already excludes several of these local files, but always check `git status` before committing.
- If a key is accidentally exposed, revoke or rotate it with the provider immediately.
- Use placeholders when sharing commands, logs, or screenshots. Remove email addresses, resume content, and other personal information.
