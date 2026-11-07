import secrets
import sys


def main() -> None:
    args = sys.argv[1:]
    if args == ["generate-key"]:
        print(secrets.token_urlsafe(48))
    elif args[:1] == ["hindcast"] and len(args) == 2:
        # python -m stormtracker.cli hindcast <data dir containing radar/>
        from pathlib import Path

        from .nowcast.hindcast import evaluate, format_report
        print(format_report(evaluate(Path(args[1]) / "radar")))
    else:
        print("usage: python -m stormtracker.cli generate-key | hindcast <data dir>")
        raise SystemExit(2)


if __name__ == "__main__":
    main()
