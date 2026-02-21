"""
Iron Fog - Game Server
Run: python server.py
Requires: pip install websockets
"""

import asyncio
import json
import websockets
import random
import math
import time
import uuid
import os
import threading
import http.server
import socketserver
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Tuple
from enum import Enum
import logging

logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(message)s')
log = logging.getLogger(__name__)

# ─── Constants ────────────────────────────────────────────────────────────────
MAP_RADIUS      = 8          # hex grid radius
FOG_RANGE       = 3          # visibility radius in hexes
FORT_COUNT      = 8         # total forts on map
CAPTURE_TIME    = 5.0        # seconds to capture a fort
MAX_PLAYERS     = 4
TICK_RATE       = 20         # server ticks per second
FUEL_PER_HEX    = 4          # fuel cost per hex moved      (was 3)
AMMO_SHOOT      = 8          # ammo cost per shot
SHELL_SPEED     = 2.5        # hexes per second
SHELL_DAMAGE    = 40
FORT_FUEL_GEN   = 1.2        # fuel per second per fort     (was 2.0 — too abundant)
FORT_AMMO_GEN   = 0.9        # ammo per second per fort     (was 1.2 — slight trim)
FORT_GEAR_GEN   = 0.10       # gears per second per fort    (was 0.08 — slight bump)
MOVE_SPEED_BASE = 2.5        # hexes per second base

# ── Loot / Death penalty floors ───────────────────────────────────────────────
LOOT_FUEL_FLOOR  = 15.0      # victim fuel cannot drop below this on death
LOOT_AMMO_FLOOR  = 10.0      # victim ammo cannot drop below this on death
LOOT_GEAR_FLOOR  = 0.0       # victim gears floor (can be zeroed out)
FORT_RELEASE_KEEP = 2        # forts kept by victim on death (rest go neutral)
TANK_MAX_HP     = 100
RESPAWN_TIME    = 8.0        # seconds
FIRE_COOLDOWN   = 0.8        # seconds between shots        (was 1.0)
MATCH_TIME      = 600.0      # 10 minute matches
POST_MATCH_TIME = 10.0       # leaderboard display duration

PLAYER_COLORS   = ["#e74c3c", "#3498db", "#2ecc71", "#f39c12"]
UPGRADE_MAX_LVL = 3          # max level per upgrade
UPGRADE_COSTS   = {
    "engine":  {"gears": 5,  "desc": "Move 20% faster"},
    "armor":   {"gears": 5,  "desc": "+20 max HP"},
    "cannon":  {"gears": 5,  "desc": "+10 shell damage"},
    "sensor":  {"gears": 5,  "desc": "+1 vision range"},
    "loader":  {"gears": 5,  "desc": "-2 ammo cost per shot"},
}
# Cost scales per level: Lv1=5, Lv2=10, Lv3=18

# ─── Hex Math ─────────────────────────────────────────────────────────────────
def hex_distance(a, b):
    return (abs(a[0]-b[0]) + abs(a[0]+a[1]-b[0]-b[1]) + abs(a[1]-b[1])) // 2

def hex_neighbors(q, r):
    dirs = [(1,0),(1,-1),(0,-1),(-1,0),(-1,1),(0,1)]
    return [(q+dq, r+dr) for dq, dr in dirs]

def hex_ring(center, radius):
    if radius == 0:
        return [center]
    results = []
    q, r = center[0] + 0 * radius, center[1] + (-1) * radius
    dirs = [(1,1),(0,1),(-1,0),(-1,-1),(0,-1),(1,0)]
    q, r = center[0] + (-1)*radius, center[1] + 0*radius
    dirs_hex = [(1,-1),(1,0),(0,1),(-1,1),(-1,0),(0,-1)]
    q = center[0]
    r = center[1] - radius
    for i, (dq, dr) in enumerate([(1,0),(0,1),(-1,1),(-1,0),(0,-1),(1,-1)]):
        for _ in range(radius):
            results.append((q, r))
            q += dq
            r += dr
    return results

def hex_range(center, radius):
    results = []
    for dq in range(-radius, radius+1):
        for dr in range(max(-radius, -dq-radius), min(radius, -dq+radius)+1):
            results.append((center[0]+dq, center[1]+dr))
    return results

def hex_line(a, b):
    """Return hexes along a straight line from a to b."""
    n = hex_distance(a, b)
    if n == 0:
        return [a]
    results = []
    for i in range(n+1):
        t = i / n
        fq = a[0] + (b[0]-a[0])*t + 1e-6
        fr = a[1] + (b[1]-a[1])*t + 1e-6
        # round cube coords
        x, z = fq, fr
        y = -x-z
        rx, ry, rz = round(x), round(y), round(z)
        dx, dy, dz = abs(rx-x), abs(ry-y), abs(rz-z)
        if dx > dy and dx > dz:
            rx = -ry-rz
        elif dy > dz:
            ry = -rx-rz
        else:
            rz = -rx-ry
        results.append((rx, rz))
    return results

def all_hexes_in_radius(radius):
    return hex_range((0,0), radius)

# ─── Game Objects ─────────────────────────────────────────────────────────────
class FortType(Enum):
    FUEL    = "fuel"
    AMMO    = "ammo"
    GEAR    = "gear"
    MIXED   = "mixed"

@dataclass
class Fort:
    id: str
    q: int
    r: int
    ftype: str         # "fuel","ammo","gear","mixed"
    owner: Optional[str] = None   # player id
    capture_progress: float = 0.0
    capturing_player: Optional[str] = None
    was_owned: bool = False        # True once any player has ever owned this fort

    def to_dict(self):
        return {
            "id": self.id, "q": self.q, "r": self.r,
            "ftype": self.ftype, "owner": self.owner,
            "capture_progress": self.capture_progress,
            "capturing_player": self.capturing_player,
            "was_owned": self.was_owned,
        }

@dataclass
class Shell:
    id: str
    owner_id: str
    q: float
    r: float
    target_q: int
    target_r: int
    speed: float
    damage: int
    created: float

    def to_dict(self):
        return {
            "id": self.id, "owner_id": self.owner_id,
            "q": self.q, "r": self.r,
            "target_q": self.target_q, "target_r": self.target_r,
        }

@dataclass
class Tank:
    id: str
    player_id: str
    q: float
    r: float
    target_q: Optional[int]  = None
    target_r: Optional[int]  = None
    path: List               = field(default_factory=list)
    hp: int                  = TANK_MAX_HP
    max_hp: int              = TANK_MAX_HP
    fuel: float              = 80.0       # was 150 — forces early fort capture
    ammo: float              = 50.0       # was 80  — early game more cautious
    gears: float             = 0.0
    move_speed: float        = MOVE_SPEED_BASE
    vision: int              = FOG_RANGE
    shell_damage: int        = SHELL_DAMAGE
    ammo_cost: float         = AMMO_SHOOT
    alive: bool              = True
    respawn_timer: float     = 0.0
    color: str               = "#e74c3c"
    facing: float            = 0.0   # radians
    upgrades: Dict           = field(default_factory=dict)
    last_shot: float = 0.0
    score: int               = 0
    kills: int               = 0
    deaths: int              = 0
    fort_captures: int       = 0



    def to_dict(self, for_player=None):
        d = {
            "id": self.id, "player_id": self.player_id,
            "q": self.q, "r": self.r,
            "target_q": self.target_q, "target_r": self.target_r,
            "hp": self.hp, "max_hp": self.max_hp,
            "alive": self.alive, "color": self.color,
            "facing": self.facing, "score": self.score,
            "path": self.path,
        }
        if for_player == self.player_id:
            d.update({
                "fuel": self.fuel, "ammo": self.ammo,
                "gears": self.gears, "move_speed": self.move_speed,
                "vision": self.vision, "ammo_cost": self.ammo_cost,
                "upgrades": self.upgrades, "respawn_timer": self.respawn_timer,
            })
        return d

# ─── Game State ───────────────────────────────────────────────────────────────
class GameState:
    def _end_match(self):
        self.match_over = True
        self.post_match_timer = POST_MATCH_TIME

    def _reset_match(self):
        # Reset tanks
        for tank in self.tanks.values():
            tank.hp = TANK_MAX_HP
            tank.max_hp = TANK_MAX_HP
            tank.fuel = 80.0
            tank.ammo = 50.0
            tank.gears = 0.0
            tank.move_speed = MOVE_SPEED_BASE
            tank.vision = FOG_RANGE
            tank.shell_damage = SHELL_DAMAGE
            tank.ammo_cost = AMMO_SHOOT
            tank.alive = True
            tank.respawn_timer = 0.0
            tank.path = []
            tank.upgrades = {}
            tank.score = 0
            tank.kills = 0
            tank.deaths = 0
            tank.fort_captures = 0

        # Reset forts
        for fort in self.forts.values():
            fort.owner = None
            fort.capture_progress = 0
            fort.capturing_player = None
            fort.was_owned = False

        # Clear shells
        self.shells.clear()
        self.pending_events.clear()

        # Reset match state
        self.match_timer = MATCH_TIME
        self.match_over = False
        self.rematch_votes.clear()

    def __init__(self):
        self.players: Dict[str, dict]   = {}   # ws_id -> player info
        self.tanks:   Dict[str, Tank]   = {}   # player_id -> tank
        self.forts:   Dict[str, Fort]   = {}
        self.shells:  Dict[str, Shell]  = {}
        self.map_hexes = set(map(tuple, all_hexes_in_radius(MAP_RADIUS)))
        self.tick     = 0
        self.started  = False
        self.last_tick_time = time.time()
        self._generate_forts()
        self.match_timer = MATCH_TIME
        self.match_over = False
        self.post_match_timer = 0.0
        self.pending_events: List[dict] = []  # kill/capture feed events
        self.rematch_votes: set = set()       # player_ids who voted to rematch


    def _generate_forts(self):
        hexes = list(self.map_hexes)
        # exclude center and near-center
        hexes = [h for h in hexes if hex_distance(h, (0,0)) >= 3]
        random.shuffle(hexes)
        types = ([FortType.FUEL.value]*4 + [FortType.AMMO.value]*4 +
                 [FortType.GEAR.value]*2 + [FortType.MIXED.value]*2)
        for i, ftype in enumerate(types):
            if i >= len(hexes): break
            q, r = hexes[i]
            fid = f"fort_{i}"
            self.forts[fid] = Fort(id=fid, q=q, r=r, ftype=ftype)

    def add_player(self, ws_id: str, name: str) -> str:
        if len(self.players) >= MAX_PLAYERS:
            return None
        pid = str(uuid.uuid4())[:8]
        color = PLAYER_COLORS[len(self.players) % len(PLAYER_COLORS)]
        # spawn at distinct starting positions
        starts = [(0,4),(-4,0),(0,-4),(4,-4),(4,0),(-4,4)]
        sq, sr = starts[len(self.players) % len(starts)]
        self.players[ws_id] = {"id": pid, "name": name, "color": color, "ws_id": ws_id}
        tank = Tank(id=f"tank_{pid}", player_id=pid,
                    q=float(sq), r=float(sr), color=color)
        self.tanks[pid] = tank
        log.info(f"Player {name}({pid}) joined at ({sq},{sr})")
        return pid

    def remove_player(self, ws_id: str):
        if ws_id in self.players:
            pid = self.players[ws_id]["id"]
            del self.players[ws_id]
            if pid in self.tanks:
                del self.tanks[pid]
            # release forts (was_owned intentionally kept — disconnected fort stays "hot")
            for fort in self.forts.values():
                if fort.owner == pid:
                    fort.owner = None
                    fort.capture_progress = 0
                if fort.capturing_player == pid:
                    fort.capturing_player = None
                    fort.capture_progress = 0

    def set_tank_path(self, player_id: str, path: List[Tuple]):
        tank = self.tanks.get(player_id)
        if not tank or not tank.alive:
            return {"error": "tank not available"}
        if not path:
            tank.path = []
            tank.target_q = None
            tank.target_r = None
            return {"ok": True}
        # validate path is connected hexes
        tank.path = [list(p) for p in path]
        if tank.path:
            tank.target_q = tank.path[-1][0]
            tank.target_r = tank.path[-1][1]
        return {"ok": True, "path": tank.path}

    def shoot(self, player_id: str, target_q: int, target_r: int):
        tank = self.tanks.get(player_id)
        if not tank or not tank.alive:
            return {"error": "tank not available"}
        now = time.time()
        if now - tank.last_shot < FIRE_COOLDOWN:
            return {"error": "cooldown"}
        tank.last_shot = now

        if tank.ammo < tank.ammo_cost:
            return {"error": "not enough ammo"}
        dist = hex_distance((round(tank.q), round(tank.r)), (target_q, target_r))
        max_range = 5 + tank.upgrades.get("cannon", 0)
        if dist > max_range:
            return {"error": "out of range"}
        tank.ammo -= tank.ammo_cost
        sid = str(uuid.uuid4())[:8]
        shell = Shell(
            id=sid, owner_id=player_id,
            q=tank.q, r=tank.r,
            target_q=target_q, target_r=target_r,
            speed=SHELL_SPEED,
            damage=tank.shell_damage + tank.upgrades.get("cannon", 0)*5,
            created=time.time()
        )
        self.shells[sid] = shell
        return {"ok": True, "shell_id": sid}

    def upgrade(self, player_id: str, upgrade_type: str):
        tank = self.tanks.get(player_id)
        if not tank:
            return {"error": "no tank"}
        cost = UPGRADE_COSTS.get(upgrade_type)
        if not cost:
            return {"error": "unknown upgrade"}
        current_lvl = tank.upgrades.get(upgrade_type, 0)
        if current_lvl >= UPGRADE_MAX_LVL:
            return {"error": "max level reached"}
        # Escalating cost: Lv1=5, Lv2=10, Lv3=18
        lvl_costs = [5, 10, 18]
        gear_cost = lvl_costs[current_lvl]
        if tank.gears < gear_cost:
            return {"error": "not enough gears"}
        tank.gears -= gear_cost
        lvl = current_lvl + 1
        tank.upgrades[upgrade_type] = lvl
        # apply effect
        if upgrade_type == "engine":
            tank.move_speed += 0.4
        elif upgrade_type == "armor":
            tank.max_hp += 20
            tank.hp = min(tank.hp + 20, tank.max_hp)
        elif upgrade_type == "cannon":
            tank.shell_damage += 10
        elif upgrade_type == "sensor":
            tank.vision += 1
        elif upgrade_type == "loader":
            tank.ammo_cost = max(2, tank.ammo_cost - 2)  # 8→6→4→2, all 3 levels meaningful
        return {"ok": True, "upgrade": upgrade_type, "level": lvl, "max_level": UPGRADE_MAX_LVL}

    def cast_vote(self, player_id: str) -> dict:
        if not self.match_over:
            return {"error": "match not over"}
        self.rematch_votes.add(player_id)
        total = len(self.tanks)
        cast  = len(self.rematch_votes)
        # If all players voted, start new match immediately
        if cast >= total and total > 0:
            self._reset_match()
        return {"ok": True, "voted": cast, "total": total}

    def tick_update(self, dt: float):

        if not self.match_over:
            self.match_timer -= dt
            if self.match_timer <= 0:
                self.match_timer = 0
                self._end_match()

        if self.match_over:
            self.post_match_timer -= dt
            if self.post_match_timer <= 0:
                self._reset_match()
            return

        self._move_tanks(dt)
        self._move_shells(dt)
        self._update_captures(dt)
        self._generate_resources(dt)
        self._check_respawns(dt)
        self.tick += 1

    def _move_tanks(self, dt: float):
        for tank in self.tanks.values():
            if not tank.alive or not tank.path:
                continue
            next_q, next_r = tank.path[0]
            dq = next_q - tank.q
            dr = next_r - tank.r
            dist = math.sqrt(dq*dq + dr*dr)
            step = tank.move_speed * dt
            if dist <= step:
                # arrived at waypoint
                if tank.fuel < FUEL_PER_HEX:
                    tank.path = []
                    tank.target_q = None
                    tank.target_r = None
                    continue
                tank.fuel -= FUEL_PER_HEX
                tank.q = float(next_q)
                tank.r = float(next_r)
                tank.path.pop(0)
                if not tank.path:
                    tank.target_q = None
                    tank.target_r = None
            else:
                # interpolate
                tank.facing = math.atan2(dr, dq)
                tank.q += (dq/dist) * step
                tank.r += (dr/dist) * step

    def _move_shells(self, dt: float):
        dead = []
        for sid, shell in self.shells.items():
            dq = shell.target_q - shell.q
            dr = shell.target_r - shell.r
            dist = math.sqrt(dq*dq + dr*dr)
            step = shell.speed * dt
            if dist <= step or (time.time() - shell.created) > 3.0:
                # impact
                dead.append(sid)
                self._shell_impact(shell)
            else:
                shell.q += (dq/dist)*step
                shell.r += (dr/dist)*step
        for sid in dead:
            del self.shells[sid]

    def _shell_impact(self, shell: Shell):
        tq, tr = shell.target_q, shell.target_r
        for pid, tank in self.tanks.items():
            if pid == shell.owner_id or not tank.alive:
                continue
            if hex_distance((round(tank.q), round(tank.r)), (tq, tr)) <= 1:
                tank.hp -= shell.damage
                if tank.hp <= 0:
                    tank.hp = 0
                    tank.alive = False
                    tank.respawn_timer = RESPAWN_TIME
                    tank.path = []
                    # give score to shooter
                    shooter = self.tanks.get(shell.owner_id)
                    if shooter:
                        shooter.score += 10
                        shooter.kills += 1
                        tank.deaths += 1

                        # ── Resource loot: victim loses a random portion of fuel, ammo, gears ──
                        # Loss ratio is random (20%–60%), but resources never drop below floor
                        fuel_ratio = random.uniform(0.20, 0.60)
                        ammo_ratio = random.uniform(0.20, 0.60)
                        gear_ratio = random.uniform(0.20, 0.60)

                        fuel_lost = max(0, min(tank.fuel * fuel_ratio, tank.fuel - LOOT_FUEL_FLOOR))
                        ammo_lost = max(0, min(tank.ammo * ammo_ratio, tank.ammo - LOOT_AMMO_FLOOR))
                        gear_lost = max(0, min(tank.gears * gear_ratio, tank.gears - LOOT_GEAR_FLOOR))

                        tank.fuel  = max(LOOT_FUEL_FLOOR,  tank.fuel  - fuel_lost)
                        tank.ammo  = max(LOOT_AMMO_FLOOR,  tank.ammo  - ammo_lost)
                        tank.gears = max(LOOT_GEAR_FLOOR,  tank.gears - gear_lost)

                        # Killer gets gears only — fuel/ammo penalty stays with victim, nothing transfers
                        gear_gained = random.uniform(5, max(5, gear_lost))
                        shooter.gears = min(99, shooter.gears + gear_gained)

                        # ── Fort release on death: keep only FORT_RELEASE_KEEP forts ──
                        victim_forts = [f for f in self.forts.values() if f.owner == pid]
                        if len(victim_forts) > FORT_RELEASE_KEEP:
                            # sort: release forts farthest from victim's current position first
                            victim_forts.sort(
                                key=lambda f: abs(f.q - tank.q) + abs(f.r - tank.r),
                                reverse=True
                            )
                            forts_to_release = victim_forts[FORT_RELEASE_KEEP:]
                            for f in forts_to_release:
                                f.owner = None
                                f.capture_progress = 0
                                f.capturing_player = None
                                # was_owned stays True — these become hot contested forts
                    # kill feed event
                    shooter_info = next((p for p in self.players.values() if p["id"]==shell.owner_id), None)
                    victim_info  = next((p for p in self.players.values() if p["id"]==pid), None)
                    self.pending_events.append({
                        "type":         "killfeed",
                        "killer":       shooter_info["name"]  if shooter_info else "?",
                        "killer_color": shooter_info["color"] if shooter_info else "#fff",
                        "victim":       victim_info["name"]   if victim_info  else "?",
                        "victim_color": victim_info["color"]  if victim_info  else "#888",
                        "ts":           time.time(),
                    })

    def _update_captures(self, dt: float):
        RECAPTURE_MULTIPLIER = 1.5   # reclaiming a previously-owned fort takes 1.5x longer

        for fort in self.forts.values():
            fq, fr = fort.q, fort.r
            # effective capture time: longer if the fort has ever been owned
            effective_capture_time = CAPTURE_TIME * (RECAPTURE_MULTIPLIER if fort.was_owned else 1.0)

            # find tanks on this hex
            on_fort = []
            for pid, tank in self.tanks.items():
                if tank.alive and round(tank.q) == fq and round(tank.r) == fr:
                    on_fort.append(pid)

            if len(on_fort) == 0:
                # decay capture progress if nobody is here
                if fort.capturing_player and fort.capture_progress > 0:
                    fort.capture_progress = max(0, fort.capture_progress - dt*0.5)
                    if fort.capture_progress == 0:
                        fort.capturing_player = None
            elif len(on_fort) == 1:
                capturer = on_fort[0]
                if fort.owner == capturer:
                    pass  # already owned, nothing to do
                else:
                    if fort.capturing_player == capturer:
                        # same player continuing their capture attempt
                        fort.capture_progress += dt
                        if fort.capture_progress >= effective_capture_time:
                            fort.capture_progress = effective_capture_time
                            fort.owner = capturer
                            fort.was_owned = True       # mark as having been owned
                            fort.capturing_player = None
                            # score
                            self.tanks[capturer].score += 5
                            self.tanks[capturer].fort_captures += 1
                            # capture feed event
                            cap_info = next((p for p in self.players.values() if p["id"]==capturer), None)
                            self.pending_events.append({
                                "type":       "capturefeed",
                                "player":     cap_info["name"]  if cap_info else "?",
                                "color":      cap_info["color"] if cap_info else "#fff",
                                "fort_type":  fort.ftype,
                                "ts":         time.time(),
                            })
                    else:
                        # new player contesting — reset progress and assign to them
                        # (they must fill the full bar from 0, accounting for recapture penalty)
                        fort.capturing_player = capturer
                        fort.capture_progress = max(0, fort.capture_progress - dt*1.5)
            else:
                # multiple players contesting — progress decays fast
                fort.capture_progress = max(0, fort.capture_progress - dt*2)

    def _generate_resources(self, dt: float):
        owned_by = {}
        for fort in self.forts.values():
            if fort.owner:
                if fort.owner not in owned_by:
                    owned_by[fort.owner] = []
                owned_by[fort.owner].append(fort.ftype)

        for pid, ftypes in owned_by.items():
            tank = self.tanks.get(pid)
            if not tank or not tank.alive:
                continue
            for ftype in ftypes:
                if ftype in ("fuel", "mixed"):
                    tank.fuel = min(120, tank.fuel + FORT_FUEL_GEN*dt)   # cap was 200
                if ftype in ("ammo", "mixed"):
                    tank.ammo = min(100, tank.ammo + FORT_AMMO_GEN*dt)   # cap was 150
                if ftype in ("gear", "mixed"):
                    tank.gears = min(99, tank.gears + FORT_GEAR_GEN*dt)

    def _check_respawns(self, dt: float):
        for pid, tank in self.tanks.items():
            if not tank.alive:
                tank.respawn_timer -= dt
                if tank.respawn_timer <= 0:
                    tank.alive = True
                    tank.hp = tank.max_hp
                    tank.respawn_timer = 0
                    # respawn at random edge
                    starts = [(0,4),(-4,0),(0,-4),(4,-4),(4,0),(-4,4)]
                    sq, sr = random.choice(starts)
                    tank.q = float(sq)
                    tank.r = float(sr)
                    tank.path = []

    def get_state_for(self, player_id: str) -> dict:
        """Build game state snapshot visible to a specific player."""
        tank = self.tanks.get(player_id)
        if not tank:
            return {}

        # compute visible hexes
        visible = set(map(tuple, hex_range(
            (round(tank.q), round(tank.r)), tank.vision
        )))

        # tanks (show enemies only if visible)
        tanks_data = {}
        for pid, t in self.tanks.items():
            tpos = (round(t.q), round(t.r))
            if pid == player_id or tpos in visible:
                tanks_data[pid] = t.to_dict(for_player=player_id)

        # forts (show only if visible or owned)
        forts_data = {}
        for fid, f in self.forts.items():
            fpos = (f.q, f.r)
            if fpos in visible or f.owner == player_id:
                forts_data[fid] = f.to_dict()

        # shells visible
        shells_data = {}
        for sid, s in self.shells.items():
            spos = (round(s.q), round(s.r))
            if spos in visible or s.owner_id == player_id:
                shells_data[sid] = s.to_dict()

        # leaderboard
        scores = [
            {"pid": pid, "name": next((p["name"] for p in self.players.values() if p["id"]==pid), pid),
             "score": t.score, "color": t.color,
             "forts": sum(1 for f in self.forts.values() if f.owner==pid),
             "kills": t.kills, "deaths": t.deaths, "captures": t.fort_captures}
            for pid, t in self.tanks.items()
        ]
        scores.sort(key=lambda x: x["score"], reverse=True)

        # votes state for match over screen
        votes_data = {
            "cast":     len(self.rematch_votes),
            "total":    len(self.tanks),
            "voted_pids": list(self.rematch_votes),
            "my_voted": player_id in self.rematch_votes,
        }

        # winner info (top scorer)
        winner = scores[0] if scores else None

        return {
            "type": "state",
            "tick": self.tick,
            "player_id": player_id,
            "tanks": tanks_data,
            "forts": forts_data,
            "shells": shells_data,
            "visible_hexes": [list(h) for h in visible],
            "leaderboard": scores,
            "map_radius": MAP_RADIUS,
            "match_timer": self.match_timer,
            "match_over": self.match_over,
            "post_match_timer": self.post_match_timer,
            "votes": votes_data,
            "winner_name":  winner["name"]  if winner and self.match_over else None,
            "winner_color": winner["color"] if winner and self.match_over else None,
        }


# ─── WebSocket Server ─────────────────────────────────────────────────────────
game = GameState()
connections: Dict[str, websockets.WebSocketServerProtocol] = {}  # ws_id -> ws
player_ws: Dict[str, str] = {}  # player_id -> ws_id

async def broadcast(msg: dict):
    dead = []
    for ws_id, ws in list(connections.items()):
        try:
            await ws.send(json.dumps(msg))
        except:
            dead.append(ws_id)
    for ws_id in dead:
        await handle_disconnect(ws_id)

async def handle_disconnect(ws_id: str):
    if ws_id in connections:
        del connections[ws_id]
    pid = next((p["id"] for p in game.players.values() if p.get("ws_id") == ws_id), None)
    game.remove_player(ws_id)
    if pid:
        await broadcast({"type":"player_left","player_id":pid})
    log.info(f"Player disconnected: {ws_id}")

async def handle_message(ws_id: str, data: dict):
    player_id = next((p["id"] for p in game.players.values() if p.get("ws_id") == ws_id), None)

    mtype = data.get("type")

    if mtype == "join":
        name = data.get("name", "Tank")[:20]
        pid = game.add_player(ws_id, name)
        if pid is None:
            await connections[ws_id].send(json.dumps({"type":"error","msg":"Game full"}))
            return
        player_ws[pid] = ws_id
        ws = connections[ws_id]
        await ws.send(json.dumps({"type":"joined","player_id":pid,
                                   "color":game.players[ws_id]["color"],
                                   "upgrades": UPGRADE_COSTS,
                                   "upgrade_max_level": UPGRADE_MAX_LVL,
                                   "map_radius": MAP_RADIUS}))
        await broadcast({"type":"player_joined","name":name,"color":game.players[ws_id]["color"]})
        return

    if not player_id:
        return

    if mtype == "move":
        path = data.get("path", [])
        result = game.set_tank_path(player_id, [tuple(p) for p in path])
        ws = connections[ws_id]
        await ws.send(json.dumps({"type":"move_ack", **result}))

    elif mtype == "shoot":
        tq, tr = data.get("target_q", 0), data.get("target_r", 0)
        result = game.shoot(player_id, tq, tr)
        ws = connections[ws_id]
        await ws.send(json.dumps({"type":"shoot_ack", **result}))

    elif mtype == "upgrade":
        utype = data.get("upgrade_type","")
        result = game.upgrade(player_id, utype)
        ws = connections[ws_id]
        await ws.send(json.dumps({"type":"upgrade_ack", **result}))

    elif mtype == "vote_rematch":
        result = game.cast_vote(player_id)
        ws = connections[ws_id]
        await ws.send(json.dumps({"type":"vote_ack", **result}))

    elif mtype == "chat":
        text = str(data.get("text","")).strip()[:120]
        if text:
            pinfo = next((p for p in game.players.values() if p["id"]==player_id), None)
            name  = pinfo["name"]  if pinfo else "?"
            color = pinfo["color"] if pinfo else "#fff"
            await broadcast({"type":"chat","name":name,"color":color,
                             "text":text,"ts":time.time()})

    elif mtype == "ping":
        ws = connections[ws_id]
        await ws.send(json.dumps({"type":"pong"}))

async def handler(ws):
    ws_id = str(uuid.uuid4())[:8]
    connections[ws_id] = ws
    log.info(f"New connection: {ws_id}")
    try:
        async for raw in ws:
            try:
                data = json.loads(raw)
                await handle_message(ws_id, data)
            except json.JSONDecodeError:
                pass
            except Exception as e:
                log.error(f"Error handling msg from {ws_id}: {e}")
    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        await handle_disconnect(ws_id)

async def game_loop():
    """Main game tick loop."""
    last_match_over = False  # track to only broadcast match_over once
    while True:
        now = time.time()
        dt  = now - game.last_tick_time
        game.last_tick_time = now

        game.tick_update(min(dt, 0.1))

        # broadcast kill/capture feed events
        for evt in game.pending_events:
            await broadcast(evt)
        game.pending_events.clear()

        # broadcast match_over exactly once when it transitions
        if game.match_over and not last_match_over:
            scores = [
                {"pid": pid, "name": next((p["name"] for p in game.players.values() if p["id"]==pid), pid),
                 "score": t.score, "color": t.color,
                 "forts": sum(1 for f in game.forts.values() if f.owner==pid)}
                for pid, t in game.tanks.items()
            ]
            scores.sort(key=lambda x: x["score"], reverse=True)
            await broadcast({"type": "match_over", "leaderboard": scores})
        last_match_over = game.match_over

        # send per-player state
        for ws_id, ws in list(connections.items()):
            player_info = game.players.get(ws_id)
            if not player_info:
                continue
            pid = player_info["id"]
            try:
                state = game.get_state_for(pid)
                await ws.send(json.dumps(state))
            except:
                pass

        await asyncio.sleep(1.0 / TICK_RATE)


PORT    = 8080
WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'web')

MIME_TYPES = {
    '.html': 'text/html; charset=utf-8',
    '.js':   'application/javascript',
    '.css':  'text/css',
    '.png':  'image/png',
    '.jpg':  'image/jpeg',
    '.jpeg': 'image/jpeg',
    '.gif':  'image/gif',
    '.ico':  'image/x-icon',
    '.svg':  'image/svg+xml',
    '.json': 'application/json',
}

def _serve_file(path):
    """Return (content, mime) for a URL path, or (None, None) if not found."""
    if not path or path == '/':
        path = '/index.html'
    path = path.split('?')[0]  # strip query string
    safe = os.path.normpath(path).lstrip('/\\')
    full = os.path.join(WEB_DIR, safe)
    # Prevent directory traversal
    if not os.path.abspath(full).startswith(os.path.abspath(WEB_DIR)):
        return None, None
    try:
        with open(full, 'rb') as f:
            content = f.read()
        ext = os.path.splitext(full)[1].lower()
        mime = MIME_TYPES.get(ext, 'application/octet-stream')
        return content, mime
    except FileNotFoundError:
        return None, None

async def main():
    log.info(f"Iron Fog starting on port {PORT}")

    # Detect websockets Response/Headers classes (location varies by version)
    ResponseClass = None
    HeadersClass  = None
    for mod_path, cls_name, target in [
        ('websockets.http11',      'Response', 'R'),
        ('websockets.http',        'Response', 'R'),
        ('websockets.datastructures', 'Headers', 'H'),
        ('websockets.http11',      'Headers',  'H'),
        ('websockets.headers',     'Headers',  'H'),
    ]:
        try:
            import importlib
            mod = importlib.import_module(mod_path)
            cls = getattr(mod, cls_name)
            if target == 'R' and ResponseClass is None:
                ResponseClass = cls
                log.info(f"Response: {mod_path}.{cls_name}")
            elif target == 'H' and HeadersClass is None:
                HeadersClass = cls
                log.info(f"Headers:  {mod_path}.{cls_name}")
        except Exception:
            pass

    if ResponseClass and HeadersClass:
        async def process_request(connection, request):
            # Let WebSocket upgrades through
            try:
                upgrade = request.headers.get("Upgrade", "")
            except Exception:
                upgrade = ""
            if upgrade.lower() == "websocket":
                return None

            # Serve file from web/ folder
            try:
                req_path = request.path
            except Exception:
                req_path = "/"
            content, mime = _serve_file(req_path)
            if content is None:
                h = HeadersClass([("Content-Length", "9"), ("Connection", "close")])
                return ResponseClass(404, "Not Found", h, b"Not Found")
            h = HeadersClass([
                ("Content-Type",   mime),
                ("Content-Length", str(len(content))),
                ("Cache-Control",  "no-cache"),
                ("Connection",     "close"),
            ])
            return ResponseClass(200, "OK", h, content)

        extra = {"process_request": process_request}
    else:
        log.warning("Could not find websockets Response class — only WS will work on this port")
        extra = {}

    async with websockets.serve(handler, "0.0.0.0", PORT, **extra):
        log.info(f"Ready!")
        log.info(f"  Local:   http://localhost:{PORT}")
        log.info(f"  Friends: ngrok http {PORT}  →  share that URL")
        await game_loop()

if __name__ == "__main__":
    asyncio.run(main())