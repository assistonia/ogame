#!/usr/bin/env python3
"""Turn-based OGameX simulation.

Each turn:
  1. pin the bot-side clock to the server's virtual clock
  2. (every N turns) LLM strategists rewrite the policies of the "llm" group
  3. every active account plays one turn against the real OGameX server
  4. the server clock advances by `turn_seconds` and all events are settled
  5. scores are recorded; DB checkpoints are written periodically

Usage:
  python3 -m sim.run_sim --config sim/configs/pilot.json --fresh
  python3 -m sim.run_sim --config sim/configs/pilot.json            # resume
  python3 -m sim.run_sim --config sim/configs/pilot.json --turns 20  # stop early
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import datetime as dt
import json
import os
import pathlib
import random
import shutil
import sqlite3
import sys
import time
import traceback

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("OGAME_BASE_URL", "http://127.0.0.1:8000")

from bots.ai_agents import policy_engine  # noqa: E402
from bots.rule_based import miner_bot  # noqa: E402
from bots.shared import simclock  # noqa: E402
from bots.shared.client_pool import OGameClientPool  # noqa: E402
from sim import server, strategist  # noqa: E402
from sim.llm import BudgetExceeded, ClaudeCLI  # noqa: E402

# Runtime state directories the v1 bots keep next to their code. A fresh
# universe must not inherit cooldowns/ledgers from an older run.
BOT_STATE_DIRS = [
    "bots/shared/.sessions",
    "bots/ai_agents/state",
    *[f"bots/rule_based/{d}" for d in (
        ".raider_state", ".build_backoff", ".resource_transfer_state", ".battle_guard_state",
        ".colony_state", ".intercept_state", ".phalanx_intel_state", ".battle_sim_state",
        ".turtle_reflex_state", ".strategy_state", ".colony_dev_state")],
]


class Run:
    def __init__(self, config_path: pathlib.Path):
        self.cfg = json.loads(config_path.read_text())
        self.name = self.cfg["name"]
        self.dir = ROOT / "runs" / self.name
        self.policy_dir = self.dir / "policies"
        self.state_path = self.dir / "state.json"
        self.log_path = self.dir / "run.log"
        self.db_path = self.dir / "metrics.db"
        self.password = self.cfg.get("password", "botpass")
        self.accounts: list[dict] = []
        self.engine_log: dict[str, list[str]] = {}
        self.pool = OGameClientPool()
        llm_cfg = self.cfg.get("llm", {})
        self.llm = None
        if llm_cfg.get("enabled", True):
            self.dir.mkdir(parents=True, exist_ok=True)
            self.llm = ClaudeCLI(self.dir / "llm_ledger.json", llm_cfg.get("budget_usd", 20.0),
                                 llm_cfg.get("model", "claude-sonnet-5-5"), llm_cfg.get("effort"))

    # ---------- bookkeeping ----------
    def log(self, msg: str) -> None:
        stamp = dt.datetime.now().strftime("%H:%M:%S")
        line = f"{stamp} {msg}"
        print(line, flush=True)
        with self.log_path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    def db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS scores(turn INT, clock INT, account TEXT, grp TEXT,
              personality TEXT, general INT, economy INT, research INT, military INT, planets INT);
            CREATE TABLE IF NOT EXISTS turns(turn INT PRIMARY KEY, clock INT, wall_sec REAL,
              bot_errors INT, missions INT, advance_errors INT);
            CREATE TABLE IF NOT EXISTS llm_calls(turn INT, clock INT, account TEXT, ok INT,
              cost REAL, personality TEXT, note TEXT, error TEXT, policy TEXT);
            CREATE TABLE IF NOT EXISTS actions(turn INT, account TEXT, line TEXT);
            CREATE TABLE IF NOT EXISTS messages(turn INT, sender TEXT, recipient TEXT, text TEXT);
        """)
        return conn

    def save_state(self, turn: int) -> None:
        self.state_path.write_text(json.dumps({"next_turn": turn, "accounts": self.accounts}, indent=1))

    # ---------- setup ----------
    def build_accounts(self) -> list[dict]:
        accounts = []
        for g in self.cfg["groups"]:
            personalities = g.get("personalities") or ["hybrid"]
            for i in range(g["count"]):
                name = f"{g['prefix']}{i + 1:02d}"
                accounts.append({
                    "account": name,
                    "email": f"{name}@bots.local",
                    "group": g["name"],
                    "controller": g["controller"],  # classic | policy_static | policy_llm
                    "personality": personalities[i % len(personalities)],
                    "persona": g.get("persona", ""),
                    # policy keys the LLM may not change (they stay at the initial policy)
                    "locked": list(g.get("llm_locked_keys", [])),
                    # free-world player: no role, sets its own goal and targets
                    "free": bool(g.get("free", False)),
                })
        return accounts

    def fresh_start(self) -> None:
        if self.dir.exists():
            shutil.rmtree(self.dir)
        self.dir.mkdir(parents=True)
        if self.llm is not None:
            self.llm.ledger_path.write_text(json.dumps({"spent_usd": 0.0, "calls": 0, "errors": 0}))
        for rel in BOT_STATE_DIRS:
            shutil.rmtree(ROOT / rel, ignore_errors=True)
        self.log("resetting universe")
        server.reset()
        server.up()
        server.ensure_readonly_db_user()
        info = server.init(self.cfg["universe"]["speed"])
        self.log(f"server init {info}")
        self.accounts = self.build_accounts()
        klass = self.cfg["universe"].get("class", "")
        # Seed round-robin (one account per group, then a farm) so planets are
        # interleaved across systems: every raider has farms within scan range
        # and no group gets a systematically better neighbourhood.
        farms = self.cfg.get("farms") or {}
        queues = [[(g["prefix"], i + 1, {"klass": klass}) for i in range(g["count"])]
                  for g in self.cfg["groups"]]
        queues.append([("farm", i + 1, {"inactive_days": farms.get("inactive_days", 10),
                                         "mines": farms.get("mines", 10),
                                         "resources": farms.get("resources", 0)})
                       for i in range(farms.get("count", 0))])
        seeded = 0
        while any(queues):
            for q in queues:
                if q:
                    prefix, num, opts = q.pop(0)
                    seeded += len(server.seed(prefix, 1, start=num, **opts)["created"])
        self.log(f"seeded {seeded} accounts (interleaved)")
        now = server.clock()
        ttl = self.policy_ttl_hours()
        for a in self.accounts:
            if a["controller"].startswith("policy"):
                strategist.write_policy(self.policy_dir, a["account"],
                                        strategist.initial_policy(a["personality"], now, ttl))
        server.advance(0, scores=True)
        self.save_state(0)

    def policy_ttl_hours(self) -> float:
        # Static policies must never expire; LLM ones are refreshed well before.
        turns = self.cfg["universe"]["turns"]
        return round(turns * self.cfg["universe"]["turn_seconds"] / 3600 * 2 + 24, 1)

    def resume(self) -> int:
        server.up()
        server.ensure_readonly_db_user()
        state = json.loads(self.state_path.read_text())
        emails = {p["email"] for p in server.scores().get("players", [])}
        if state["accounts"] and state["accounts"][0]["email"] not in emails:
            # New machine / wiped DB: roll back to the last checkpoint.
            ckpt = self.dir / "checkpoint.sql.gz"
            if not ckpt.exists():
                raise SystemExit("universe is empty and no checkpoint exists; use --fresh")
            self.log("server DB is empty: restoring last checkpoint")
            server.restore(ckpt)
            shutil.copy(self.dir / "checkpoint_state.json", self.state_path)
            bots_tar = self.dir / "checkpoint_bots.tar.gz"
            if bots_tar.exists():
                for rel in BOT_STATE_DIRS:
                    shutil.rmtree(ROOT / rel, ignore_errors=True)
                shutil.unpack_archive(bots_tar, ROOT)
            state = json.loads(self.state_path.read_text())
        self.accounts = state["accounts"]
        return int(state["next_turn"])

    # ---------- one account turn ----------
    def play(self, a: dict, turn: int) -> str:
        with self.pool.lease(a["email"], self.password) as client:
            if a["controller"] == "classic":
                return miner_bot.tick(a["email"], password=self.password,
                                      personality_key=a["personality"], tick_index=turn, client=client)
            return policy_engine.policy_tick(a["email"], password=self.password,
                                             tick_index=turn, client=client)

    # ---------- LLM refresh ----------
    def standings(self, scores: dict) -> dict:
        players = [p for p in scores.get("players", []) if not p["email"].startswith(("farm", "simadmin"))]
        players.sort(key=lambda p: p["general"], reverse=True)
        top = [{"player": p["email"].split("@")[0], "score": p["general"], "military": p["military"],
                "planets": p["planets"]} for p in players[:8]]
        return {"players": len(players), "top": top,
                "by_account": {p["email"].split("@")[0]: {"rank": i + 1, "score": p["general"]}
                               for i, p in enumerate(players)}}

    def world_view(self, scores: dict, now: int, window_sec: int) -> dict:
        """Who lives where and who attacked whom, for free-world players."""
        names = dict(server.query("SELECT id, username FROM users"))
        planets: dict[str, list[str]] = {}
        for uid, g, sy, pos in server.query(
                "SELECT user_id, galaxy, system, planet FROM planets "
                "WHERE planet_type=1 AND destroyed=0 ORDER BY id"):
            planets.setdefault(names.get(uid, "?"), []).append(f"{g}:{sy}:{pos}")
        self.planets_by_player = planets
        by = {p["email"].split("@")[0]: p for p in scores.get("players", [])}
        players = []
        for name, coords in sorted(planets.items()):
            if name in ("Legor", "Admin", "?"):
                continue
            p = by.get(name, {})
            players.append({"player": name, "planets": coords[:6],
                            "score": p.get("general"), "military": p.get("military"),
                            **({"inactive": True} if name.startswith("farm") else {})})
        attacks: dict[tuple[str, str], list[int]] = {}
        for owner, target, dep in server.query(
                "SELECT f.user_id, p.user_id, f.time_departure FROM fleet_missions f "
                "JOIN planets p ON p.id = f.planet_id_to "
                "WHERE f.mission_type = 1 AND f.parent_id IS NULL AND f.user_id <> p.user_id"):
            a, v = names.get(owner, "?"), names.get(target, "?")
            if v.startswith("farm"):
                continue
            c = attacks.setdefault((a, v), [0, 0])
            c[0] += 1
            c[1] += int(dep) >= now - window_sec
        log = [{"attacker": a, "victim": v, "total": c[0], "recent": c[1]}
               for (a, v), c in sorted(attacks.items(), key=lambda kv: -kv[1][0])]
        return {"players": players, "attacks_between_players": log[:40],
                "note": "recent = since your last policy; farm raids not listed"}

    def chat_view(self, conn: sqlite3.Connection, turn: int, account: str, since_turn: int) -> dict:
        """Inbox, own recent messages and public no-attack declarations for one player."""
        inbox = [{"turn": t, "from": f, "to": r, "text": x} for t, f, r, x in conn.execute(
            "SELECT turn, sender, recipient, text FROM messages WHERE turn >= ? AND sender <> ? "
            "AND recipient IN (?, 'all') ORDER BY turn DESC, rowid DESC LIMIT 30",
            (since_turn, account, account))]
        sent = [{"turn": t, "to": r, "text": x} for t, r, x in conn.execute(
            "SELECT turn, recipient, text FROM messages WHERE turn >= ? AND sender = ? "
            "ORDER BY turn DESC LIMIT 6", (since_turn, account))]
        return {"inbox": inbox, "your_recent_messages": sent,
                "no_attack_declarations": self.no_attack}

    def apply_excluded_players(self, policy: dict) -> dict:
        """Translate targeting.excluded_players into exact planet coords for the engine."""
        tgt = policy.get("targeting") or {}
        coords = [c for name in tgt.get("excluded_players", [])
                  for c in getattr(self, "planets_by_player", {}).get(name, [])]
        tgt["excluded_targets"] = coords
        policy["targeting"] = tgt
        return policy

    def refresh_llm(self, turn: int, conn: sqlite3.Connection, scores: dict) -> None:
        targets = [a for a in self.accounts if a["controller"] == "policy_llm"]
        if not targets or self.llm is None:
            return
        now = simclock.now()
        iso = dt.datetime.fromtimestamp(now, dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        stand = self.standings(scores)
        ttl = self.policy_ttl_hours()
        world = None
        if any(a.get("free") for a in targets):
            window = self.cfg["llm"].get("every_turns", 32) * self.cfg["universe"]["turn_seconds"]
            world = self.world_view(scores, int(now), window)
        chat = bool(self.cfg.get("chat"))
        every = self.cfg["llm"].get("every_turns", 32)
        if chat and not hasattr(self, "no_attack"):
            self.no_attack = {}
            for a in targets:
                try:
                    pol = json.loads((self.policy_dir / f"{a['account']}.json").read_text())
                    names = (pol.get("targeting") or {}).get("excluded_players") or []
                    if names:
                        self.no_attack[a["account"]] = names
                except (OSError, ValueError):
                    pass
        known = set(getattr(self, "planets_by_player", {}))

        def one(a: dict):
            path = self.policy_dir / f"{a['account']}.json"
            base = json.loads(path.read_text())
            mine = stand["by_account"].get(a["account"], {})
            prompt = strategist.build_prompt(
                email=a["email"], persona=a["persona"] or f"{a['personality']} player", policy=base,
                standings={"you": mine, "players": stand["players"], "top": stand["top"]},
                turn=turn, game_time_iso=iso, engine_log=self.engine_log.get(a["account"], []),
                locked=a.get("locked", []),
                world=({**world, **views[a["account"]]} if chat else world) if a.get("free") else None,
                chat=chat and a.get("free", False))
            try:
                if a.get("free"):
                    system = strategist.CHAT_SYSTEM_PROMPT if chat else strategist.FREE_SYSTEM_PROMPT
                else:
                    system = strategist.SYSTEM_PROMPT
                text, cost = self.llm.ask(system, prompt)
                update = strategist.parse_policy(text)
                merged = strategist.merge_policy(base, update, now, ttl, free=a.get("free", False))
                for key in a.get("locked", []):
                    merged[key] = base.get(key)
                msgs = []
                if chat and a.get("free"):
                    merged = self.apply_excluded_players(merged)
                    msgs = strategist.parse_messages(update, a["account"], known)
                strategist.write_policy(self.policy_dir, a["account"], merged)
                return (a, True, cost, merged, None, msgs)
            except BudgetExceeded as exc:
                return (a, False, 0.0, base, str(exc), [])
            except Exception as exc:  # noqa: BLE001 - keep previous policy
                return (a, False, 0.0, base, f"{type(exc).__name__}: {exc}"[:500], [])

        workers = self.cfg.get("llm", {}).get("parallel", 4)
        views = {a["account"]: self.chat_view(conn, turn, a["account"], turn - 2 * every)
                 for a in targets if chat and a.get("free")}
        with cf.ThreadPoolExecutor(max_workers=workers) as ex:
            results = list(ex.map(one, targets))
        for a, ok, cost, pol, err, msgs in results:
            if chat and ok and a.get("free"):
                self.no_attack[a["account"]] = (pol.get("targeting") or {}).get("excluded_players", [])
                if not self.no_attack[a["account"]]:
                    self.no_attack.pop(a["account"])
                for to, text in msgs:
                    conn.execute("INSERT INTO messages VALUES(?,?,?,?)", (turn, a["account"], to, text))
                    self.log(f"  msg {a['account']} -> {to}: {text}"[:220])
        for a, ok, cost, pol, err, _ in results:
            conn.execute("INSERT INTO llm_calls VALUES(?,?,?,?,?,?,?,?,?)",
                         (turn, int(now), a["account"], int(ok), cost, pol.get("personality"),
                          pol.get("note", ""), err, json.dumps(pol, ensure_ascii=False) if ok else None))
            self.log(f"  llm {a['account']}: {'ok' if ok else 'FAIL'} ${cost:.3f} "
                     f"{pol.get('personality')} | {(pol.get('note') if ok else err) or ''}"[:220])
        conn.commit()
        if self.llm is not None:
            self.log(f"  llm ledger {self.llm.ledger()}")

    # ---------- main loop ----------
    def record_scores(self, conn: sqlite3.Connection, turn: int, scores: dict) -> None:
        by_email = {a["email"]: a for a in self.accounts}
        for p in scores.get("players", []):
            a = by_email.get(p["email"])
            grp = a["group"] if a else ("farm" if p["email"].startswith("farm") else "other")
            pers = a["personality"] if a else None
            if a and a["controller"].startswith("policy"):
                try:
                    pers = json.loads((self.policy_dir / f"{a['account']}.json").read_text()).get("personality")
                except (OSError, ValueError):
                    pass
            conn.execute("INSERT INTO scores VALUES(?,?,?,?,?,?,?,?,?,?)",
                         (turn, scores.get("clock"), p["email"].split("@")[0], grp, pers, p["general"],
                          p["economy"], p["research"], p["military"], p["planets"]))
        conn.commit()

    def run(self, fresh: bool, max_turns: int | None) -> None:
        if fresh or not self.state_path.exists():
            self.fresh_start()
            start = 0
        else:
            start = self.resume()
        policy_engine.POLICY_DIR = self.policy_dir
        u = self.cfg["universe"]
        total = u["turns"] if max_turns is None else min(u["turns"], start + max_turns)
        llm_every = self.cfg.get("llm", {}).get("every_turns", 16)
        score_every = self.cfg.get("score_every", 4)
        ckpt_every = self.cfg.get("checkpoint_every", 96)
        parallel = self.cfg.get("parallel", 6)
        conn = self.db()
        scores = server.scores()
        self.log(f"run {self.name}: turns {start}..{total - 1}, {len(self.accounts)} accounts")

        for turn in range(start, total):
            t0 = time.time()
            clock = server.clock()
            simclock.set_now(clock)
            if turn % llm_every == 0:
                self.refresh_llm(turn, conn, scores)

            order = list(self.accounts)
            random.Random(f"{self.cfg.get('seed', 0)}-{turn}").shuffle(order)
            errors = 0
            with cf.ThreadPoolExecutor(max_workers=parallel) as ex:
                futures = {ex.submit(self.play, a, turn): a for a in order}
                for fut in cf.as_completed(futures):
                    a = futures[fut]
                    try:
                        line = fut.result()
                    except Exception as exc:  # noqa: BLE001
                        errors += 1
                        line = f"ERROR {type(exc).__name__}: {exc}"
                        if errors <= 3:
                            self.log(f"  {a['account']} {line}\n{traceback.format_exc(limit=3)}")
                    log = self.engine_log.setdefault(a["account"], [])
                    log.append(f"t{turn} {line}"[:300])
                    del log[:-20]
                    conn.execute("INSERT INTO actions VALUES(?,?,?)", (turn, a["account"], line[:500]))

            want_scores = (turn + 1) % score_every == 0
            adv = server.advance(u["turn_seconds"], scores=want_scores)
            if want_scores:
                scores = server.scores()
                self.record_scores(conn, turn, scores)
            wall = time.time() - t0
            conn.execute("INSERT OR REPLACE INTO turns VALUES(?,?,?,?,?,?)",
                         (turn, adv["to"], wall, errors, adv["missions_processed"], adv["error_count"]))
            conn.commit()
            self.log(f"turn {turn} done in {wall:.1f}s, bot errors {errors}, "
                     f"missions {adv['missions_processed']}, adv errors {adv['error_count']}")
            if adv["error_count"]:
                self.log(f"  advance errors: {adv['errors'][:3]}")
            self.save_state(turn + 1)
            if (turn + 1) % ckpt_every == 0:
                self.checkpoint(turn + 1)
        if total % ckpt_every != 0:
            self.checkpoint(total)
        self.log("run finished")

    def checkpoint(self, turn: int) -> None:
        path = self.dir / "checkpoint.sql.gz"
        server.dump(path)
        shutil.copy(self.state_path, self.dir / "checkpoint_state.json")
        import tarfile
        with tarfile.open(self.dir / "checkpoint_bots.tar.gz", "w:gz") as tar:
            for rel in BOT_STATE_DIRS:
                if (ROOT / rel).exists():
                    tar.add(ROOT / rel, arcname=rel)
        self.log(f"checkpoint at turn {turn} -> {path}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--fresh", action="store_true", help="wipe the universe and start over")
    ap.add_argument("--turns", type=int, default=None, help="play at most this many turns now")
    args = ap.parse_args()
    Run(pathlib.Path(args.config)).run(args.fresh, args.turns)


if __name__ == "__main__":
    main()
