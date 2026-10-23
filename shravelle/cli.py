"""Command line tools.

shravelle-run <command ...>      run any Python program with Shravelle enabled (no code changes)
shravelle deploy --version 1.2.3 [--commit SHA] [--description TEXT]
"""
import argparse
import os
import sys


def run() -> None:
    if len(sys.argv) < 2:
        print("usage: shravelle-run <command> [args...]   e.g. shravelle-run gunicorn app.wsgi", file=sys.stderr)
        sys.exit(2)
    boot = os.path.join(os.path.dirname(__file__), "_bootstrap")
    os.environ["PYTHONPATH"] = os.pathsep.join(filter(None, [boot, os.environ.get("PYTHONPATH", "")]))
    cmd = sys.argv[1:]
    if cmd[0].endswith(".py"):
        cmd = [sys.executable] + cmd
    os.execvp(cmd[0], cmd)


def main() -> None:
    p = argparse.ArgumentParser(prog="shravelle")
    sub = p.add_subparsers(dest="cmd")
    d = sub.add_parser("deploy", help="record a deployment marker")
    d.add_argument("--version", required=True)
    d.add_argument("--commit", default="")
    d.add_argument("--description", default="")
    d.add_argument("--service", default=None)
    a = p.parse_args()
    if a.cmd == "deploy":
        from . import record_deployment
        record_deployment(a.version, a.commit, a.description, os.environ.get("USER", ""), a.service)
        print("deployment recorded")
    else:
        p.print_help()
