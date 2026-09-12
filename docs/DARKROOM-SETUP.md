# Darkroom setup — standing up the imager

Everything in the repo is done. What's left is on the Windows box and in the
Beachhead env. Roughly 30 minutes, most of it a model download.

---

## 1. ComfyUI on the Windows machine

Download the portable build (ComfyUI_windows_portable) and unzip it wherever
you keep things. Then put at least one SDXL checkpoint in
`ComfyUI\models\checkpoints\` — any `.safetensors` SDXL model works.
Juggernaut XL and the base `sd_xl_base_1.0.safetensors` are both fine starting
points; the base model is the safest first test because it's the one everything
else is derived from.

Launch it so the container can reach it. The bundled `run_nvidia_gpu.bat`
does **not** forward arguments to Python, so passing the flag on its command
line silently does nothing — write your own launcher next to it instead.
Create `run_drydock.bat` in the `ComfyUI_windows_portable` folder:

```bat
.\python_embeded\python.exe -s ComfyUI\main.py --windows-standalone-build --listen 0.0.0.0 --disable-auto-launch
```

`--disable-auto-launch` stops ComfyUI opening a browser tab on every start —
`--windows-standalone-build` turns that on, and it's pure noise when the UI you
actually use is Darkroom.

**`--listen 0.0.0.0` is not optional.** ComfyUI binds `127.0.0.1` by default,
and a worker container reaching `host.docker.internal:8188` is not localhost as
far as ComfyUI is concerned — you'd get a connection refused and an imager that
registers with an empty checkpoint list. If Windows Firewall prompts, allow it
on the private network.

Also worth knowing: the RTX 5080 is a Blackwell card, and older portable builds
ship a PyTorch with no kernels for it. `no kernel image is available for
execution on the device` or an `sm_120` complaint means the build is too old,
not that anything here is misconfigured — take a current release.

Check it from a browser on the Windows box: `http://localhost:8188` should load
the ComfyUI canvas, and `http://localhost:8188/system_stats` should return JSON.

### Making it survive a reboot

**Docker Desktop is the binding constraint here, not ComfyUI.** Both the
Startup folder and Task Scheduler's "at log on" trigger require an interactive
logon session, and so does Docker Desktop — which the imager container needs
regardless. So don't engineer ComfyUI's autostart separately; match it to
whatever already brings Docker back.

This machine already auto-logs in and starts Docker Desktop on boot (Will,
Sept 2026), so the session exists and ComfyUI only has to join it. The
**Startup folder** is the simplest way and needs no Task Scheduler:

Two small files do it. Write them from PowerShell rather than Notepad — it
avoids the "saved as .txt" trap and the Properties dialog entirely:

```powershell
$comfy = "<COMFY>"                       # your ComfyUI folder
$startup = "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Startup"

# 1. launcher batch - runs ComfyUI, all output to comfyui.log
@"
@echo off
cd /d "%~dp0"
.\python_embeded\python.exe -s ComfyUI\main.py --windows-standalone-build --listen 0.0.0.0 --disable-auto-launch > comfyui.log 2>&1
"@ | Set-Content "$comfy\run_drydock.bat" -Encoding ASCII

# 2. one-line hidden launcher, written straight into Startup
"CreateObject(""WScript.Shell"").Run ""$comfy\run_drydock.bat"", 0, False" |
  Set-Content "$startup\comfyui-hidden.vbs" -Encoding ASCII
```

The `0` is the window style: fully hidden, not minimized. The batch redirects
to `comfyui.log`, which is both how you debug it later and — see below — what
makes it run at all.

**Do not try to do this with `pythonw.exe`.** It looks like the obvious answer
(it's the windowless build of the same interpreter, sitting right there in
`python_embeded`) and it fails, silently. With no console attached, Python sets
`sys.stdout` and `sys.stderr` to `None`; ComfyUI prints a banner on startup,
that `print()` raises `AttributeError: 'NoneType' object has no attribute
'write'`, and the traceback goes nowhere because stderr is also `None`. The
process dies in under a second with no window, no log and no exit message.
Redirecting output to a file fixes it because the handles then exist — which is
exactly what the batch above does. Hide the *launch*, not the interpreter.

Verify without rebooting:

```powershell
& "$startup\comfyui-hidden.vbs"
Start-Sleep 45
Get-Process python
Invoke-RestMethod http://localhost:8188/system_stats
```

A process plus JSON means it's up. Note that ComfyUI spends 20-60s importing
torch before it listens, so checking too early is its own false failure — worth
remembering for the reboot test, where the machine can be up a full minute
before Darkroom can reach it.

One caveat: Microsoft has deprecated VBScript, so it will become an optional
feature in some future Windows release. It works on 10 and 11 today. The
boring alternative is a shortcut to `run_drydock.bat` set to *Run: Minimized* —
a taskbar button, but no window on screen and the same log file.

There's no tray icon on offer — ComfyUI has no tray mode, and the practical
equivalent is what's above: no window at all. (If a real tray icon matters,
third-party utilities like RBTray can send an existing window there, but that
means keeping the visible console you were trying to get rid of.)

Task Scheduler is only worth it for auto-restart-on-crash, which the Startup
folder can't do. If you go that way: trigger *At log on*; action the bat; set
**Start in** to the `ComfyUI_windows_portable` folder; **untick** "Stop the
task if it runs longer than 3 days" (on by default, would kill ComfyUI every
third day); set "If the task fails, restart every 1 minute".

A true no-login setup (Task Scheduler "run whether user is logged on or not",
or NSSM as a service) is possible, but it buys nothing while Docker Desktop
still needs a session — and GPU access from session 0 is one more thing to
verify. If auto sign-in is ever switched off, nothing on that box recovers
unattended, Docker and the existing workers included.

The worker tolerates the race either way: it now waits up to
`COMFYUI_STARTUP_WAIT_SECONDS` (240 by default) for ComfyUI to answer before
registering, so a container that restarts with Docker doesn't advertise an
empty checkpoint list. If ComfyUI never comes up it registers anyway — a
visible imager with a legible error beats a machine that looks absent — and
re-probes on the next render, so it recovers without a restart.

## 2. The imager worker

Copy `worker/envs/windows-imager-1.env.example` to
`worker/envs/windows-imager-1.env` **on the Windows box**, then fill in
`WORKER_SHARED_SECRET` — copy it from any existing env file in that directory.

(That template sets `MAX_CONTEXT` even though an imager has no use for it.
The shared compose file lists the variable without a default, so an env file
missing it makes docker compose print a "variable is not set" warning on every
command — harmless, since the worker treats a blank env var as unset, but
noisy. Setting it is purely to keep the output clean.)

Then start it the usual way:

```bash
cd worker
./workers.sh restart          # or ./workers.sh up
./workers.sh logs windows-imager-1
```

You're looking for `worker.registered` with `capabilities=['image']` and a
non-empty model list. If it logs `comfy.no_checkpoints`, ComfyUI isn't
reachable — that's step 1's `--listen` almost every time.

The imager doesn't touch Ollama, doesn't clone repos, and never asks for task
work, so it's safe to leave running alongside the existing reviewer / tester /
validator workers on that machine.

## 3. Beachhead env

Add one variable, with **no Target Service** so it lands in `.env`:

```
IMAGE_MACHINE=<the "name" of the Windows box in REMOTE_MACHINES_JSON>
```

That's what lets the API path wake the machine — the dashboard's wake button is
behind the operator session cookie, which an app holding `DRYDOCK_API_KEY`
doesn't have. If you leave it blank, everything still works while the box is
awake; requests just 503 instead of waking it.

Optional, all with working defaults: `IMAGE_RETENTION_DAYS` (start at 0, set it
to 30 if the volume grows), `IMAGE_DAILY_BUDGET` (cap API-driven renders),
`IMAGE_MAX_BATCH`, `IMAGE_WAKE_TIMEOUT_SECONDS`.

## 4. Deploy

Push to main; the webhook builds. First boot creates the `image_jobs` table and
the `dry-dock-images` volume on its own — no migration to run by hand.

## 5. Check it

1. Open `/darkroom`. The machine strip top-right should say an imager is online
   (or offer to wake it).
2. Type something and hit Generate. First render on a cold checkpoint is
   30-60s; after that it's seconds.
3. From another machine:
   ```bash
   curl -s "$DRYDOCK_BASE_URL/api/v1/image/health" -H "X-API-Key: $DRYDOCK_API_KEY"
   ```
   should report `"status":"ok"` and list the checkpoints it found.

---

## The one thing to watch

That box runs `reviewer`, `tester` and `validator` against its host Ollama, and
now ComfyUI too. An SDXL checkpoint (~7GB) plus a resident `qwen2.5-coder:14b`
(~9GB) does not fit in 16GB of VRAM — whichever loads second evicts the first,
and both get slow.

It won't break anything, but if you notice review tasks crawling while you're
generating images, the fix is to raise the `worker_priority` numbers on
`windows-reviewer` / `windows-tester` on the Settings page so text work prefers
the MacBook tier. Priority is DB state set on that page — the "priority 1"
comments in the env files record intent, not what the router does.

## Adding more workflows later

Drop a new template in `worker/app/workflows/` and restart the worker; it's
discovered and offered in the UI automatically. `worker/app/workflows/README.md`
explains the format — the short version is: build it in ComfyUI, **Save (API
Format)**, paste it in as `graph`, and write a five-line `map` of parameter →
node. You can also try a graph without touching the Windows box at all by
sending it in the request's `graph` field.
