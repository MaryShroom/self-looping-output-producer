# Sandbox (bash tool)

SLOP can run the commands the model asks for inside a
[bubblewrap](https://github.com/containers/bubblewrap) (`bwrap`) sandbox.
You still approve every command first; the sandbox limits what an approved
command can do.

## What a sandboxed command can and cannot do

| | |
|---|---|
| System folders (`/usr`, ...) | visible, read-only |
| The project folder | the only writable place |
| Your home folder (`~/.ssh`, other projects, ...) | **not visible** |
| `.env` files in the project | **unreadable**: they are listed, but reading one fails with "Permission denied" |
| `.git/hooks`, `.git/config` | read-only |
| Network | **off** (unless you turn it on) |
| Timeout / Ctrl-C | kills the command and everything it started |
| Environment | API keys and other secret-looking variables removed |

Not a VM: bubblewrap shares your kernel. It stops mistakes and a misbehaving
model, not a determined kernel exploit. For fully autonomous use, run SLOP in a VM.

## Install

```bash
sudo apt install bubblewrap       # Ubuntu / Debian
sudo pacman -S bubblewrap         # Arch / CachyOS
```

### Ubuntu 24.04: AppArmor profile

If the self-test fails with `loopback: Failed RTM_NEWADDR: Operation not permitted`
and this prints `1`:

```bash
sysctl kernel.apparmor_restrict_unprivileged_userns
```

Ubuntu is blocking bwrap. Allow it (this applies to `bwrap` only, not to the
commands it runs; it does exempt `bwrap` from Ubuntu's restriction for every program):

```bash
sudo tee /etc/apparmor.d/bwrap > /dev/null <<'EOF'
abi <abi/4.0>,
include <tunables/global>

profile bwrap /usr/bin/bwrap flags=(unconfined) {
  userns,
  include if exists <local/bwrap>
}
EOF

sudo systemctl reload apparmor
```

(`which bwrap` must print `/usr/bin/bwrap`.) Then run `/sandbox test` in SLOP.

## Modes

Set `SLOP_SANDBOX` in `.env`, or change it while running with `/sandbox`:

| Mode | Meaning |
|---|---|
| `auto` (default) | Use the sandbox when it works. If not, run **UNSANDBOXED** and say so in the prompt. |
| `on` | Require it. If it isn't working, commands are refused. |
| `off` | Never use it. |

A typo in `SLOP_SANDBOX` is treated as `on` (fail closed).

Commands typed at the SLOP prompt:

```
/sandbox            show settings and whether bubblewrap works
/sandbox test       run the self-test again (after installing, or adding the profile)
/sandbox on|auto|off
/sandbox net on|off
```

Only you can change these; the model can't. The prompt tells you how each command
will run:

```
Run command [sandbox: bwrap, network off] (timeout 30s, in /home/you/project):
Run command [UNSANDBOXED (bubblewrap not working, see /sandbox)] (timeout 30s, ...):
```

## What changes for the model's commands

- No network by default, so `pip install`, `git push` and downloads fail.
  Use `/sandbox net on` when you want them. (All or nothing: no per-site rules.)
- Your home folder isn't there. Tools installed under it (pyenv, conda, `~/.cargo`,
  `~/.cache/ms-playwright`) are invisible; add them read-only with
  `SLOP_SANDBOX_RO=~/.pyenv:~/.cache/ms-playwright`. A project `.venv` works.
- `git commit` may complain about missing user name/email (your `~/.gitconfig` isn't
  visible), and `.git/config` can't be changed from inside.
- Each command gets its own sandbox: a server started in one command is gone in the
  next. Start the server, use it and stop it in the same command.

## Headless browsers and GUI checks

- Chromium/Playwright need `--no-sandbox` inside bwrap (bwrap is the outer sandbox).
  It may also need `--disable-dev-shm-usage`. Make the browsers visible with
  `SLOP_SANDBOX_RO`.
- GUI programs: use a virtual display inside the sandbox (`xvfb-run`). Don't pass your
  real display in: programs on your X11 session can watch your keystrokes.
- SLOP sends only text to the model today, so it can't look at a screenshot. Text
  checks (page text, test assertions) work now; image support is a future feature.
