# host-agent

A tiny Python HTTP daemon that runs on the Mac mini *outside* Docker, so the
dry-dock backend can wake remote machines on the LAN and send them shutdown
commands. The backend container can't broadcast Wake-on-LAN packets itself
(it's behind Docker's NAT) and can't easily run `ssh`, so this is the escape
hatch.

The agent is stdlib-only Python — no `pip install` required.

## Prerequisites on the Mac mini

```bash
brew install wakeonlan          # for the WoL magic-packet sender
```

`ssh` is built in. Make sure `~/.ssh/config` has an entry like:

```
Host windows
    HostName 192.168.1.123
    User you
    IdentityFile ~/.ssh/id_ed25519
```

(matches what you already do interactively).

## Install + run

These commands assume your repo lives at `/Users/<you>/workspace/dry-dock`.
If it's somewhere else, substitute that path everywhere it appears.

```bash
# 0. (One-time) note where your clone is, so the rest of these commands work.
REPO=/Users/$(whoami)/workspace/dry-dock      # ← adjust if yours is elsewhere
ls "$REPO/host-agent/agent.py"                # should print the path, not error

# 1. Pick a strong token. The dry-dock backend env will use the same value.
TOKEN=$(openssl rand -hex 32)
echo "$TOKEN"

# 2. Copy the plist into LaunchAgents, then patch in the real path + token.
cp "$REPO/host-agent/com.drydock.host-agent.plist" ~/Library/LaunchAgents/
sed -i '' \
  -e "s|/Users/CHANGE_ME/dry-dock|$REPO|" \
  -e "s|CHANGE_ME_strong_random_value|$TOKEN|" \
  ~/Library/LaunchAgents/com.drydock.host-agent.plist

# 3. Load + start.
launchctl load -w ~/Library/LaunchAgents/com.drydock.host-agent.plist

# 4. Confirm it's listening.
curl http://127.0.0.1:8088/healthz
# → {"status": "ok"}

# 5. Tail logs while you wire up the backend.
tail -f /tmp/drydock-host-agent.out.log /tmp/drydock-host-agent.err.log
```

To stop or reload:

```bash
launchctl unload ~/Library/LaunchAgents/com.drydock.host-agent.plist
launchctl load -w ~/Library/LaunchAgents/com.drydock.host-agent.plist
```

## Tell the backend about it

Set these env vars in Beachhead (Admin Settings → Environment Variables), with
no Target Service so they land in `.env`:

| Var | Value |
|---|---|
| `DRYDOCK_HOST_AGENT_URL` | `http://host.docker.internal:8088` |
| `DRYDOCK_HOST_AGENT_TOKEN` | same `$TOKEN` you put in the plist |
| `REMOTE_MACHINES_JSON` | JSON array of machines (see below) |

`REMOTE_MACHINES_JSON` example for one Windows PC:

```json
[
  {
    "name": "windows-rig",
    "display_name": "Windows (RTX 3080)",
    "mac": "D8:BB:C1:51:84:DF",
    "ssh_host": "windows",
    "broadcast": "192.168.1.255",
    "hardware_class": "windows-rtx3080"
  }
]
```

`hardware_class` is the same value you set in the worker's `.env` — the
dashboard uses it to cross-reference live workers with this machine, so the
status chip flips to "online" the moment a worker registers from there.

After setting the env vars, redeploy the backend on Beachhead. The homepage
will gain a "Remote machines" panel with a Wake + Shutdown button per
machine.

## Threat model & hardening notes

The agent binds to `0.0.0.0` by default because Docker Desktop's
`host.docker.internal` doesn't resolve to `127.0.0.1`. That means *anything on
your LAN* that knows the token can wake / shut down machines.

If that worries you:

- The bearer token is a uniformly random 256-bit string — brute-force is not
  the threat.
- Restrict the listening interface by setting `DRYDOCK_HOST_AGENT_BIND` to the
  Docker bridge IP (look it up with `ifconfig` — usually a `vmnet*` or
  `bridge0` address on macOS Docker Desktop).
- Or use `pf` to drop inbound TCP 8088 from anything that isn't the bridge.
- Or stash the agent's port behind localhost and add a tiny socat/ssh tunnel
  from the Docker bridge IP to 127.0.0.1.

For a personal home lab on a network you control, the default is fine.

## What the agent does NOT do

- It does not run arbitrary commands from the request body. The wake path
  takes a MAC + broadcast IP (both validated by regex). The shutdown path
  takes an SSH host alias and runs a fixed `shutdown /s /t 0` against it.
  The status path runs `ping`.
- It does not store state. The list of machines lives in the backend's env;
  the agent is just a remote subprocess executor.
- It does not retry. If WoL fails or the target is unreachable, the result
  bubbles up to the dashboard verbatim.
