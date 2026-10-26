"""`python -m first_in_bot` starts the bot; `python -m first_in_bot check` runs the
preflight checks instead (setup.sh does this before starting the service).

Anything else is refused rather than starting the bot: a mistyped "check" must
never start a second copy next to a running one."""
import sys

USAGE = ("Usage: .venv/bin/python -m first_in_bot          start the bot\n"
         "       .venv/bin/python -m first_in_bot check    check the setup without starting it")

args = sys.argv[1:]
if not args:
    from .main import main
elif args == ["check"]:
    from .preflight import main
else:
    print(USAGE, file=sys.stderr)
    sys.exit(2)

main()
