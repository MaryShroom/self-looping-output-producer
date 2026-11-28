"""Slash commands the USER types (/sandbox, /auto, /help). The model can never run these:
only text typed at the prompt reaches this module.

Auto-approve is deliberately only a runtime switch. It is not read from .env, because SLOP loads
.env from the project folder, and a project you just cloned must not be able to turn off your
approval prompts.
"""
import shell
import tools

ask = input  # replaceable in tests

HELP = (
    "commands:\n"
    "  /auto               show auto-approve settings\n"
    "  /auto files on|off  apply write_file/edit_file changes without asking (diffs still shown)\n"
    "  /auto shell on|off  run sandboxed, safe-looking commands without asking\n"
    "  /auto off           turn both off\n"
    "  /sandbox ...        shell sandbox settings (try /sandbox)\n"
    "  /help               this text\n"
    "  exit                quit"
)

AUTO_USAGE = (
    "usage:\n"
    "  /auto               show the settings\n"
    "  /auto files on|off\n"
    "  /auto shell on|off\n"
    "  /auto off           turn both off"
)


def auto_status() -> str:
    _, label, error = shell._plan()
    files = "ON  - file changes are applied without asking (the diff is still printed)" \
        if tools.auto_approve else "off - you approve each file change"
    if not shell.auto_approve:
        sh = "off - you approve each command"
    elif error or label.startswith("UNSANDBOXED"):
        sh = "ON  - but the sandbox is not in use right now, so every command still asks"
    else:
        sh = ("ON  - sandboxed commands run without asking; risky-looking or unsandboxed "
              "ones still ask")
    return f"auto-approve files: {files}\nauto-approve shell: {sh}"


def _enable_shell() -> str:
    _, label, error = shell._plan()
    print("Auto-approving shell commands means the model's commands run without your review.")
    if error or label.startswith("UNSANDBOXED"):
        print("The sandbox is not in use right now, so nothing would be auto-approved yet.")
    else:
        print("They run inside the sandbox; commands that look risky still ask. A sandbox is "
              "a strong barrier, not a perfect one.")
    try:
        answer = ask("Type 'yes' to turn it on: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        answer = ""
    if answer != "yes":
        return "not enabled.\n" + auto_status()
    shell.auto_approve = True
    return auto_status()


def _auto(args: list[str]) -> str:
    if not args:
        return auto_status()
    if args == ["off"]:
        tools.auto_approve = False
        shell.auto_approve = False
        return auto_status()
    if len(args) == 2 and args[0] in ("files", "file", "shell") and args[1] in ("on", "off"):
        on = args[1] == "on"
        if args[0] == "shell":
            if on:
                return _enable_shell()
            shell.auto_approve = False
        else:
            tools.auto_approve = on
        return auto_status()
    return AUTO_USAGE


def handle_slash(line: str):
    """Returns the text to print, or None when the line is not a command we know."""
    parts = line.strip().split()
    if not parts or not parts[0].startswith("/"):
        return None
    name, args = parts[0].lower(), [p.lower() for p in parts[1:]]
    if name == "/sandbox":
        return shell.handle_slash(line)
    if name == "/auto":
        return _auto(args)
    if name in ("/help", "/?"):
        return HELP
    return None
