# PiVision -- Raspberry Pi Deployment Context & Systemd Integration Plan

## Project Overview

PiVision is a Raspberry Pi--hosted computer vision system with:

-   `backend.server` -- HTTP API server (ThreadingHTTPServer)

-   `backend.worker` -- background worker watching DB and processing
    tasks

-   SQLite database located at:

        backend/data/pivision.db

Virtual environment:

    /home/djg/Projects/PiVision/.venv

Project root:

    /home/djg/Projects/PiVision

Primary runtime user:

    djg

------------------------------------------------------------------------

# Current Environment State

## Python Environment

Virtual environment created:

``` bash
python3 -m venv .venv
source .venv/bin/activate
```

OpenCV via pip FAILED on ARM (expected).

Using system package instead:

``` bash
sudo apt install python3-opencv
```

Verified:

``` bash
python3 -c "import cv2; print(cv2.__version__)"
# 4.6.0
```

Conclusion: OpenCV is provided by apt, not pip.

------------------------------------------------------------------------

# Running the Application Manually

Worker:

``` bash
python -m backend.worker
```

Server:

``` bash
python -m backend.server
```

Port:

    8080

Verified:

``` bash
curl http://127.0.0.1:8080/
```

------------------------------------------------------------------------

# Port Conflict Issue Observed

If server already running:

    OSError: [Errno 98] Address already in use

Diagnosis:

``` bash
sudo ss -lntup | grep 8080
```

Systemd must manage lifecycle cleanly to prevent duplicates.

------------------------------------------------------------------------

# Deployment Requirements

We want:

1.  Server + worker auto-start on boot
2.  Automatic restart on crash
3.  Run as user `djg`
4.  Use virtualenv Python
5.  Clean separation of services
6.  Avoid interference from legacy services

------------------------------------------------------------------------

# Required Systemd Configuration Constraints

Services must:

-   Use venv python:

        /home/djg/Projects/PiVision/.venv/bin/python

-   WorkingDirectory:

        /home/djg/Projects/PiVision

-   Installed in:

        /etc/systemd/system/

-   Use:

        WantedBy=multi-user.target

-   Restart policy:

        Restart=always
        RestartSec=2

------------------------------------------------------------------------

# Services To Create

## pivision-server.service

Should: - Run `python -m backend.server` - Set `PORT=8080` - Restart
automatically - Bind to 0.0.0.0

## pivision-worker.service

Should: - Run `python -m backend.worker` - Restart automatically - Start
after network - Optionally depend on server

------------------------------------------------------------------------

# Logging

Logs should be viewable via:

``` bash
journalctl -u pivision-server -f
journalctl -u pivision-worker -f
```

------------------------------------------------------------------------

# Legacy Services Present

Potential conflicts:

-   aifoodstand-camera.service
-   aifoodstand-server.service
-   aifoodstand-uploader.service
-   foodstand-backend.service

Codex should verify none conflict with port 8080.

------------------------------------------------------------------------

# Deliverables Requested From Codex

Codex should:

1.  Generate production-ready systemd unit files
2.  Provide installation steps:
    -   daemon-reload
    -   enable
    -   start
    -   status verification
3.  Ensure correct user, working directory, restart policy
4.  Suggest resilience improvements if appropriate

------------------------------------------------------------------------

# Systemd Unit Definitions

## pivision-server.service

```ini
[Unit]
Description=PiVision API server
After=network.target

[Service]
Type=simple
User=djg
WorkingDirectory=/home/djg/Projects/PiVision
Environment=PORT=8080
Environment=PYTHONUNBUFFERED=1
ExecStart=/home/djg/Projects/PiVision/.venv/bin/python -m backend.server
Restart=always
RestartSec=2
StartLimitIntervalSec=60
StartLimitBurst=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=pivision-server
PrivateTmp=true
ProtectSystem=full
ProtectHome=read-only
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
```

## pivision-worker.service

```ini
[Unit]
Description=PiVision background worker
After=network.target pivision-server.service
Wants=pivision-server.service

[Service]
Type=simple
User=djg
WorkingDirectory=/home/djg/Projects/PiVision
Environment=PYTHONUNBUFFERED=1
ExecStart=/home/djg/Projects/PiVision/.venv/bin/python -m backend.worker
Restart=always
RestartSec=2
StartLimitIntervalSec=60
StartLimitBurst=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=pivision-worker
PrivateTmp=true
ProtectSystem=full
ProtectHome=read-only
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
```

# Installation Checklist

1.  Copy each `.service` file to `/etc/systemd/system/` on the Pi (e.g., `sudo install -m 644 pivision-server.service /etc/systemd/system/`).
2.  Run `sudo systemctl daemon-reload` to apply the new units.
3.  Enable and start the services: `sudo systemctl enable --now pivision-server pivision-worker`.
4.  Confirm status: `sudo systemctl status pivision-server pivision-worker` and monitor logs via `journalctl -u pivision-server -f` / `journalctl -u pivision-worker -f`.
5.  Use `sudo ss -lntup | grep 8080` to ensure `pivision-server` owns port 8080 after the services are running.
6.  After `sudo reboot`, verify `systemctl is-active pivision-server pivision-worker` and `curl http://localhost:8080/` succeed without manual starts.

# Legacy Service Audit

Before deploying PiVision services, inspect and disable any legacy units that could grab port 8080:

- `aifoodstand-camera.service`
- `aifoodstand-server.service`
- `aifoodstand-uploader.service`
- `foodstand-backend.service`

Stop and disable active conflicts with `sudo systemctl stop <name> && sudo systemctl disable <name>`, then re-check `ss -lntup` to confirm the port is free for PiVision.

# Resilience & Observability Recommendations

- Keep logging in the journal so `journalctl -u pivision-<role>` surfaces crashes or startup problems quickly.
- The `StartLimitIntervalSec=60` and `StartLimitBurst=5` settings prevent rapid-fire restart loops; keep them in place.
- Harden the services with `ProtectSystem=full`, `ProtectHome=read-only`, `PrivateTmp=true`, and `NoNewPrivileges=true` once prerequisites are met.
- Consider a simple health-check cron or systemd timer that curls `http://localhost:8080/` and logs failures or notifies you so outages are visible before a manual check.
- Ensure `/home/djg/Projects/PiVision/backend/data/pivision.db` remains owned by `djg` with the proper permissions so both services can read/write it after reboots.

------------------------------------------------------------------------

# Final Goal

After:

``` bash
sudo reboot
```

The Pi should:

-   Auto-start server + worker
-   Bind correctly to 8080
-   Reconnect to DB
-   Require zero manual intervention

PiVision should operate as a stable appliance.
