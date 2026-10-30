"""Thin wrapper over server/sim.sh (artisan commands return one JSON line)."""
from __future__ import annotations

import json
import pathlib
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[1]
SIM_SH = ROOT / "server" / "sim.sh"


def _run(*args: str, timeout: int = 900) -> str:
    proc = subprocess.run(
        [str(SIM_SH), *args], capture_output=True, text=True, timeout=timeout
    )
    if proc.returncode != 0:
        raise RuntimeError(f"sim.sh {' '.join(args)} failed: {proc.stderr.strip()[-2000:]}")
    return proc.stdout


def artisan_json(*args: str, timeout: int = 900) -> dict:
    out = _run("artisan", *args, timeout=timeout)
    for line in reversed(out.strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            return json.loads(line)
    raise RuntimeError(f"no JSON in artisan output: {out[-500:]}")


def reset() -> None:
    _run("reset", timeout=300)


def up() -> None:
    _run("up", timeout=1800)


def init(speed: int, clock: int | None = None) -> dict:
    args = ["ogamex:sim:init", f"--speed={speed}"]
    if clock is not None:
        args.append(f"--clock={clock}")
    return artisan_json(*args)


def clock() -> int:
    return int(artisan_json("ogamex:sim:clock")["clock"])


def seed(prefix: str, count: int, *, start: int = 1, pad: int = 2, klass: str = "",
         inactive_days: int = 0, mines: int = 0, resources: int = 0) -> dict:
    return artisan_json(
        "ogamex:sim:seed-bots", f"--prefix={prefix}", f"--count={count}", f"--start={start}",
        f"--pad={pad}",
        f"--class={klass}", f"--inactive-days={inactive_days}", f"--mines={mines}",
        f"--resources={resources}",
    )


def advance(seconds: int, scores: bool = False) -> dict:
    args = ["ogamex:sim:advance", f"--seconds={seconds}"]
    if scores:
        args.append("--scores")
    return artisan_json(*args)


def scores() -> dict:
    return artisan_json("ogamex:sim:scores")


def dump(path: pathlib.Path) -> None:
    _run("dump", str(path), timeout=900)


def restore(path: pathlib.Path) -> None:
    _run("restore", str(path), timeout=900)


def ensure_readonly_db_user() -> None:
    _run("compose", "exec", "-T", "ogamex-db", "mariadb", "-uroot", "-ptoor", "-e",
         "CREATE USER IF NOT EXISTS 'botread'@'%' IDENTIFIED BY 'botread'; "
         "GRANT SELECT ON laravel.* TO 'botread'@'%'; FLUSH PRIVILEGES;")
