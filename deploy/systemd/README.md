# systemd user timers

Two timers. Neither can submit an application.

| Unit | When | What |
|---|---|---|
| `jobpipe-prepare.timer` | Mon/Wed/Fri 06:00 (±15m) | `applypilot run` — discover through gate |
| `jobpipe-otp.timer` | every 15 min | `applypilot otp --once` |

`jobpipe-prepare` stops at the gate stage. Submission additionally requires an
approved review batch, which only `applypilot review` can grant. Submitting
stays a manual `applypilot apply` after the gate clears.

## Install

    ./install.sh

Then check:

    systemctl --user list-timers 'jobpipe-*'

## Notes

- These are **user** units, so they only run while you are logged in. To let
  them run without a session: `sudo loginctl enable-linger $USER`.
- The OTP timer is pointless until Gmail auth is configured; it will fail
  every 15 minutes and log it. Leave it disabled until then:
  `systemctl --user disable --now jobpipe-otp.timer`.
- Logs: `journalctl --user -u jobpipe-prepare.service -n 100`.
