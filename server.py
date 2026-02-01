"""
Servidor simple (WebSocket) para sesiones estilo "sala" con:
- Crear sesión -> código de 4 caracteres
- Unirse jugadores (max 6)
  - Al unirse: le manda al que se unió el estado de la sala y jugadores existentes
  - Broadcast a todos: nuevo jugador
- "Ready" por jugador y start de partida cuando TODOS estén listos
- Rondas (cantidad y tiempo configurables al crear sala)
- Recibir 1 respuesta por jugador por ronda (answer_id)
- Al finalizar cada ronda: leaderboard + puntos ganados en esa ronda (por jugador)
- Al finalizar todas las rondas: puntajes finales y cierra sesión

Dependencia:
  pip install websockets

Ejecutar:
  python server.py
Conecta desde Unity usando WebSocket a:
  ws://<IP>:8765

Protocolo (JSON):
Cliente -> Servidor:
  { "type":"create_session", "name":"Host", "round_count":3, "round_time_sec":20 }
  { "type":"join_session", "code":"AB12", "name":"Player2" }
  { "type":"set_ready", "code":"AB12", "ready":true }
  { "type":"start_session", "code":"AB12" }
  { "type":"submit_answer", "code":"AB12", "round_id":1, "answer_id":"X3" }

Servidor -> Cliente (ejemplos):
  session_created, joined, room_state, player_joined, player_left, player_ready,
  game_started, round_started, round_ended, session_ended, error
"""

import asyncio
import json
import random
import string
import time
from dataclasses import dataclass, field
from typing import Dict, Optional, Set, Any

import websockets
from websockets.server import WebSocketServerProtocol


# -----------------------------
# Modelos
# -----------------------------

def now_ms() -> int:
    return int(time.time() * 1000)


def make_code(existing: Set[str]) -> str:
    alphabet = string.ascii_uppercase + string.digits
    while True:
        code = "".join(random.choice(alphabet) for _ in range(4))
        if code not in existing:
            return code


@dataclass
class Player:
    player_id: str
    name: str
    ws: WebSocketServerProtocol
    ready: bool = False
    total_points: int = 0

    # Para evitar doble respuesta por ronda:
    answered_rounds: Set[int] = field(default_factory=set)


@dataclass
class Session:
    code: str
    host_player_id: str
    round_count: int
    round_time_sec: int
    players: Dict[str, Player] = field(default_factory=dict)
    state: str = "lobby"  # lobby | running | ended
    current_round: int = 0

    # Respuestas por ronda: { round_id: { player_id: answer_id } }
    answers: Dict[int, Dict[str, str]] = field(default_factory=dict)

    def to_room_state(self) -> Dict[str, Any]:
        return {
            "code": self.code,
            "state": self.state,
            "host_player_id": self.host_player_id,
            "config": {
                "round_count": self.round_count,
                "round_time_sec": self.round_time_sec,
            },
            "players": [
                {
                    "player_id": p.player_id,
                    "name": p.name,
                    "ready": p.ready,
                    "total_points": p.total_points,
                }
                for p in self.players.values()
            ],
            "current_round": self.current_round,
        }


# -----------------------------
# Estado global en memoria
# -----------------------------
SESSIONS: Dict[str, Session] = {}  # code -> Session
WS_TO_PLAYER: Dict[WebSocketServerProtocol, tuple[str, str]] = {}  # ws -> (code, player_id)
LOCK = asyncio.Lock()


# -----------------------------
# Utilidades de envío
# -----------------------------

async def safe_send(ws: WebSocketServerProtocol, payload: Dict[str, Any]) -> None:
    try:
        await ws.send(json.dumps(payload))
    except Exception:
        # Socket muerto / error; se manejará en disconnect
        pass


async def broadcast(session: Session, payload: Dict[str, Any]) -> None:
    await asyncio.gather(*(safe_send(p.ws, payload) for p in session.players.values()))


async def send_error(ws: WebSocketServerProtocol, message: str, details: Optional[Dict[str, Any]] = None) -> None:
    payload = {"type": "error", "message": message}
    if details:
        payload["details"] = details
    await safe_send(ws, payload)


def gen_player_id() -> str:
    return "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(10))


# -----------------------------
# Lógica de juego
# -----------------------------

def all_ready(session: Session) -> bool:
    return len(session.players) > 0 and all(p.ready for p in session.players.values())


def compute_round_points(session: Session, round_id: int) -> Dict[str, int]:
    """
    Puntaje SIMPLE:
      - Si el jugador envió una respuesta en la ronda => +10
      - Si no envió => +0
    Cambiá esta función para tu lógica real (correct/incorrect, rapidez, etc.).
    """
    answers = session.answers.get(round_id, {})
    pts: Dict[str, int] = {}
    for pid in session.players.keys():
        pts[pid] = 10 if pid in answers else 0
    return pts


def build_leaderboard(session: Session, round_points: Optional[Dict[str, int]] = None) -> list[Dict[str, Any]]:
    items = []
    for p in session.players.values():
        rp = round_points.get(p.player_id, 0) if round_points else 0
        items.append({
            "player_id": p.player_id,
            "name": p.name,
            "total_points": p.total_points,
            "round_points": rp,
        })
    # Orden: total desc, name asc (para estable)
    items.sort(key=lambda x: (-x["total_points"], x["name"]))
    return items


async def run_game(session: Session) -> None:
    session.state = "running"
    session.current_round = 0
    await broadcast(session, {
        "type": "game_started",
        "code": session.code,
        "config": {"round_count": session.round_count, "round_time_sec": session.round_time_sec},
        "timestamp_ms": now_ms(),
    })

    for r in range(1, session.round_count + 1):
        session.current_round = r
        session.answers[r] = {}

        ends_at = now_ms() + session.round_time_sec * 1000
        await broadcast(session, {
            "type": "round_started",
            "code": session.code,
            "round_id": r,
            "duration_sec": session.round_time_sec,
            "ends_at_ms": ends_at,
            "timestamp_ms": now_ms(),
        })

        # Esperar a que termine el tiempo de ronda
        await asyncio.sleep(session.round_time_sec)

        # Calcular puntajes de ronda
        round_points = compute_round_points(session, r)
        for pid, pts in round_points.items():
            if pid in session.players:
                session.players[pid].total_points += pts

        leaderboard = build_leaderboard(session, round_points)

        await broadcast(session, {
            "type": "round_ended",
            "code": session.code,
            "round_id": r,
            "leaderboard": leaderboard,
            "timestamp_ms": now_ms(),
        })

    session.state = "ended"
    final_lb = build_leaderboard(session, round_points=None)

    await broadcast(session, {
        "type": "session_ended",
        "code": session.code,
        "leaderboard": final_lb,
        "timestamp_ms": now_ms(),
    })


# -----------------------------
# Handlers de mensajes
# -----------------------------

async def handle_create_session(ws: WebSocketServerProtocol, msg: Dict[str, Any]) -> None:
    name = (msg.get("name") or "Host").strip()[:24]
    round_count = int(msg.get("round_count", 3))
    round_time_sec = int(msg.get("round_time_sec", 20))

    if round_count < 1 or round_count > 50:
        await send_error(ws, "round_count inválido (1..50)")
        return
    if round_time_sec < 3 or round_time_sec > 600:
        await send_error(ws, "round_time_sec inválido (3..600)")
        return

    async with LOCK:
        code = make_code(set(SESSIONS.keys()))
        pid = gen_player_id()
        session = Session(code=code, host_player_id=pid, round_count=round_count, round_time_sec=round_time_sec)
        session.players[pid] = Player(player_id=pid, name=name, ws=ws)
        SESSIONS[code] = session
        WS_TO_PLAYER[ws] = (code, pid)

    await safe_send(ws, {
        "type": "session_created",
        "code": code,
        "player": {"player_id": pid, "name": name},
        "config": {"round_count": round_count, "round_time_sec": round_time_sec},
        "room_state": session.to_room_state(),
    })


async def handle_join_session(ws: WebSocketServerProtocol, msg: Dict[str, Any]) -> None:
    code = (msg.get("code") or "").strip().upper()
    name = (msg.get("name") or "Player").strip()[:24]

    async with LOCK:
        session = SESSIONS.get(code)
        if not session:
            await send_error(ws, "Sesión no existe", {"code": code})
            return
        if session.state != "lobby":
            await send_error(ws, "La sesión ya comenzó o terminó", {"state": session.state})
            return
        if len(session.players) >= 6:
            await send_error(ws, "Sala llena (máx 6)")
            return

        pid = gen_player_id()
        session.players[pid] = Player(player_id=pid, name=name, ws=ws)
        WS_TO_PLAYER[ws] = (code, pid)

        # Estado para el que se une
        room_state = session.to_room_state()

    # Respuesta al que se une (incluye jugadores existentes)
    await safe_send(ws, {
        "type": "joined",
        "code": code,
        "player": {"player_id": pid, "name": name},
        "room_state": room_state,
    })

    # Broadcast a todos (incluye al que se unió; si no lo querés, filtralo)
    await broadcast(session, {
        "type": "player_joined",
        "code": code,
        "player": {"player_id": pid, "name": name, "ready": False, "total_points": 0},
        "timestamp_ms": now_ms(),
    })


async def handle_set_ready(ws: WebSocketServerProtocol, msg: Dict[str, Any]) -> None:
    code = (msg.get("code") or "").strip().upper()
    ready = bool(msg.get("ready", False))

    async with LOCK:
        session = SESSIONS.get(code)
        if not session:
            await send_error(ws, "Sesión no existe", {"code": code})
            return
        mapping = WS_TO_PLAYER.get(ws)
        if not mapping or mapping[0] != code:
            await send_error(ws, "No estás en esa sesión")
            return
        pid = mapping[1]
        if pid not in session.players:
            await send_error(ws, "Jugador inválido")
            return
        if session.state != "lobby":
            await send_error(ws, "No se puede cambiar ready fuera de lobby", {"state": session.state})
            return

        session.players[pid].ready = ready

    await broadcast(session, {
        "type": "player_ready",
        "code": code,
        "player_id": pid,
        "ready": ready,
        "all_ready": all_ready(session),
        "timestamp_ms": now_ms(),
    })


async def handle_start_session(ws: WebSocketServerProtocol, msg: Dict[str, Any]) -> None:
    code = (msg.get("code") or "").strip().upper()

    async with LOCK:
        session = SESSIONS.get(code)
        if not session:
            await send_error(ws, "Sesión no existe", {"code": code})
            return

        mapping = WS_TO_PLAYER.get(ws)
        if not mapping or mapping[0] != code:
            await send_error(ws, "No estás en esa sesión")
            return
        pid = mapping[1]
        if pid != session.host_player_id:
            await send_error(ws, "Solo el host puede iniciar")
            return

        if session.state != "lobby":
            await send_error(ws, "La sesión ya comenzó o terminó", {"state": session.state})
            return

        #if not all_ready(session):
        #    await send_error(ws, "No todos están listos", {"players": session.to_room_state()["players"]})
        #    return

        # Arrancar el juego en background (sin bloquear el handler)
        asyncio.create_task(run_game(session))


async def handle_submit_answer(ws: WebSocketServerProtocol, msg: Dict[str, Any]) -> None:
    code = (msg.get("code") or "").strip().upper()
    round_id = int(msg.get("round_id", 0))
    answer_id = str(msg.get("answer_id", "")).strip()

    if not answer_id:
        await send_error(ws, "answer_id vacío")
        return

    async with LOCK:
        session = SESSIONS.get(code)
        if not session:
            await send_error(ws, "Sesión no existe", {"code": code})
            return
        mapping = WS_TO_PLAYER.get(ws)
        if not mapping or mapping[0] != code:
            await send_error(ws, "No estás en esa sesión")
            return
        pid = mapping[1]
        player = session.players.get(pid)
        if not player:
            await send_error(ws, "Jugador inválido")
            return
        if session.state != "running":
            await send_error(ws, "La sesión no está corriendo", {"state": session.state})
            return
        if round_id != session.current_round:
            await send_error(ws, "round_id no coincide con la ronda actual", {"current_round": session.current_round})
            return

        # 1 respuesta por jugador por ronda
        if round_id in player.answered_rounds:
            await send_error(ws, "Ya enviaste respuesta para esta ronda", {"round_id": round_id})
            return

        player.answered_rounds.add(round_id)
        session.answers.setdefault(round_id, {})[pid] = answer_id

    # Opcional: confirmar al jugador (ACK)
    await safe_send(ws, {
        "type": "answer_received",
        "code": code,
        "round_id": round_id,
        "answer_id": answer_id,
        "timestamp_ms": now_ms(),
    })


# -----------------------------
# Conexión / Desconexión
# -----------------------------

async def disconnect(ws: WebSocketServerProtocol) -> None:
    async with LOCK:
        mapping = WS_TO_PLAYER.pop(ws, None)
        if not mapping:
            return
        code, pid = mapping
        session = SESSIONS.get(code)
        if not session:
            return
        was_host = (pid == session.host_player_id)

        player = session.players.pop(pid, None)

        # Si se fue el host, transferir host al primero que quede (si existe)
        if was_host and session.players:
            session.host_player_id = next(iter(session.players.keys()))

        # Si no quedan jugadores, borrar sesión
        if not session.players:
            SESSIONS.pop(code, None)
            return

    # Broadcast fuera del lock
    if session and player:
        await broadcast(session, {
            "type": "player_left",
            "code": code,
            "player_id": pid,
            "new_host_player_id": session.host_player_id,
            "timestamp_ms": now_ms(),
        })


# -----------------------------
# Router principal
# -----------------------------

async def handler(ws: WebSocketServerProtocol) -> None:
    try:
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await send_error(ws, "JSON inválido")
                continue

            mtype = msg.get("type")
            if mtype == "create_session":
                await handle_create_session(ws, msg)
            elif mtype == "join_session":
                await handle_join_session(ws, msg)
            elif mtype == "set_ready":
                await handle_set_ready(ws, msg)
            elif mtype == "start_session":
                await handle_start_session(ws, msg)
            elif mtype == "submit_answer":
                await handle_submit_answer(ws, msg)
            else:
                await send_error(ws, "Tipo de mensaje desconocido", {"type": mtype})
    except websockets.ConnectionClosed:
        pass
    finally:
        await disconnect(ws)


# -----------------------------
# Main
# -----------------------------

async def main() -> None:
    host = "0.0.0.0"
    port = 8765
    print(f"WS server escuchando en ws://{host}:{port}")
    async with websockets.serve(handler, host, port):
        await asyncio.Future()  # run forever


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Servidor detenido.")
