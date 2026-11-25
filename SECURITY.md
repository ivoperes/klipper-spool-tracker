# Security policy

## Supported versions

Only the latest commit on `main` is supported. Update with the Moonraker
update manager or `git pull`.

## Reporting a vulnerability

Please **don't open a public issue** for security problems. Use GitHub's
private reporting instead: on the repository page, go to **Security → Report a
vulnerability**. Include the steps to reproduce and what an attacker could do.

You can expect an acknowledgement within a week. Fixes come with a
regression test, and the reporter is credited unless they'd rather not be.

## Scope

In scope:

- `klipper/spool_tracker.py`: handling of G-code parameters, G-code files and
  the state file; anything that can crash or stop Klipper, read files outside
  the gcodes folder, or inject content into dialogs
- `klipperscreen/spool_indicator.py`
- `install.sh`

Out of scope: Klipper, KlipperScreen, Moonraker, Mainsail and Fluidd
themselves (report those to their projects), and attacks that need someone
who already has full Moonraker API access (they control the printer anyway).

## Hardening your printer

Spool Tracker trusts any client that can send G-code, like every Klipper
module. Access control is Moonraker's job:

- **Never expose Moonraker, Mainsail or Fluidd to the internet** (no port
  forwarding). Use a VPN such as Tailscale or WireGuard for remote access.
- In `moonraker.conf` `[authorization]`, set `trusted_clients` to only your
  home network range, and consider enabling `force_logins`.
- Keep Klipper, Moonraker and KlipperScreen updated.
- Install and run Spool Tracker as the normal printer user, never with `sudo`.

The full threat model and the OWASP review are in
[docs/DEVELOPER_NOTES.md](docs/DEVELOPER_NOTES.md#10-security-review-owasp).
