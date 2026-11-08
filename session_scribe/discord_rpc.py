"""Minimal client for the Discord desktop app's local RPC (IPC) interface.

This talks to the Discord app already running on this computer over its local
named pipe. Nothing joins the voice channel and no bot is involved; we only ask
the Discord client to tell us who is in the channel and who is speaking.

Requires a Discord application (https://discord.com/developers/applications)
owned by the same account that is logged into Discord, with a redirect URI
registered under OAuth2 (we default to http://localhost).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable

log = logging.getLogger(__name__)

OP_HANDSHAKE = 0
OP_FRAME = 1
OP_CLOSE = 2
OP_PING = 3
OP_PONG = 4

SCOPES = ["rpc", "rpc.voice.read"]
TOKEN_URL = "https://discord.com/api/oauth2/token"
USER_AGENT = "SessionScribe/0.1 (+https://github.com/ERFrederickson/session-scribe)"

EventHandler = Callable[[str, dict], Awaitable[None] | None]


class RPCError(Exception):
    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------- framing ---

def encode_frame(op: int, payload: dict) -> bytes:
    data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return struct.pack("<II", op, len(data)) + data


def decode_header(header: bytes) -> tuple[int, int]:
    op, length = struct.unpack("<II", header)
    return op, length


def ipc_paths() -> list[str]:
    if sys.platform == "win32":
        return [rf"\\?\pipe\discord-ipc-{i}" for i in range(10)]
    base = (
        os.environ.get("XDG_RUNTIME_DIR")
        or os.environ.get("TMPDIR")
        or os.environ.get("TMP")
        or "/tmp"
    )
    return [os.path.join(base, f"discord-ipc-{i}") for i in range(10)]


# ------------------------------------------------------------ token store ---

class TokenStore:
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> dict | None:
        try:
            return json.loads(self.path.read_text("utf-8"))
        except (OSError, ValueError):
            return None

    def save(self, token: dict) -> None:
        token = dict(token)
        token["expires_at"] = time.time() + float(token.get("expires_in", 0)) - 60
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(token, indent=2), "utf-8")

    def clear(self) -> None:
        try:
            self.path.unlink()
        except OSError:
            pass


def _post_token(form: dict) -> dict:
    body = urllib.parse.urlencode(form).encode()
    req = urllib.request.Request(
        TOKEN_URL,
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        raise RPCError(f"Discord token request failed ({e.code}): {detail}") from e


# ----------------------------------------------------------------- client ---

class DiscordRPC:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        token_path: Path,
        redirect_uri: str = "http://localhost",
        on_event: EventHandler | None = None,
        on_status: Callable[[str], None] | None = None,
    ):
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.tokens = TokenStore(token_path)
        self.on_event = on_event
        self.on_status = on_status or (lambda s: None)

        self.user: dict | None = None
        self._reader: asyncio.StreamReader | None = None
        self._transport: Any = None
        self._pending: dict[str, asyncio.Future] = {}
        self._ready: asyncio.Future | None = None
        self._read_task: asyncio.Task | None = None
        self._tasks: set[asyncio.Task] = set()
        self.closed = asyncio.Event()

    # -- connection -------------------------------------------------------

    async def connect(self) -> dict:
        loop = asyncio.get_running_loop()
        last_err: Exception | None = None
        for path in ipc_paths():
            try:
                reader = asyncio.StreamReader()
                protocol = asyncio.StreamReaderProtocol(reader)
                if sys.platform == "win32":
                    transport, _ = await loop.create_pipe_connection(  # type: ignore[attr-defined]
                        lambda: protocol, path
                    )
                else:
                    transport, _ = await loop.create_unix_connection(lambda: protocol, path)
                self._reader = reader
                self._transport = transport
                break
            except (FileNotFoundError, ConnectionRefusedError, OSError) as e:
                last_err = e
                continue
        else:
            raise RPCError(
                "Could not find the Discord desktop app. Make sure Discord is running."
            ) from last_err

        self.closed.clear()
        self._ready = loop.create_future()
        self._read_task = asyncio.create_task(self._read_loop())
        self._send(OP_HANDSHAKE, {"v": 1, "client_id": self.client_id})
        ready = await asyncio.wait_for(self._ready, timeout=10)
        return ready

    def _send(self, op: int, payload: dict) -> None:
        if self._transport is None:
            raise RPCError("Not connected to Discord")
        self._transport.write(encode_frame(op, payload))

    async def _read_loop(self) -> None:
        assert self._reader is not None
        try:
            while True:
                header = await self._reader.readexactly(8)
                op, length = decode_header(header)
                body = await self._reader.readexactly(length)
                msg = json.loads(body.decode("utf-8")) if body else {}
                if op == OP_PING:
                    self._send(OP_PONG, msg)
                elif op == OP_CLOSE:
                    err = RPCError(msg.get("message", "Discord closed the connection"), msg.get("code"))
                    if self._ready and not self._ready.done():
                        self._ready.set_exception(err)
                    raise err
                elif op == OP_FRAME:
                    await self._handle(msg)
        except (asyncio.IncompleteReadError, ConnectionError, RPCError) as e:
            log.info("Discord RPC connection closed: %s", e)
        except asyncio.CancelledError:
            pass
        except Exception:  # pragma: no cover - defensive
            log.exception("Discord RPC reader crashed")
        finally:
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(RPCError("Discord connection closed"))
            self._pending.clear()
            self.closed.set()

    async def _handle(self, msg: dict) -> None:
        cmd, evt, nonce = msg.get("cmd"), msg.get("evt"), msg.get("nonce")
        if cmd == "DISPATCH" and evt == "READY":
            if self._ready and not self._ready.done():
                self._ready.set_result(msg.get("data", {}))
            return
        if nonce and nonce in self._pending:
            fut = self._pending.pop(nonce)
            if evt == "ERROR":
                data = msg.get("data") or {}
                fut.set_exception(RPCError(data.get("message", "RPC error"), data.get("code")))
            else:
                fut.set_result(msg.get("data"))
            return
        if cmd == "DISPATCH" and evt and self.on_event:
            res = self.on_event(evt, msg.get("data") or {})
            if asyncio.iscoroutine(res):
                # Never await here: the handler may send commands whose replies
                # arrive through this same read loop.
                task = asyncio.create_task(res)
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)

    async def command(self, cmd: str, args: dict | None = None, evt: str | None = None,
                      timeout: float = 30) -> Any:
        nonce = str(uuid.uuid4())
        fut = asyncio.get_running_loop().create_future()
        self._pending[nonce] = fut
        payload: dict[str, Any] = {"cmd": cmd, "args": args or {}, "nonce": nonce}
        if evt:
            payload["evt"] = evt
        self._send(OP_FRAME, payload)
        return await asyncio.wait_for(fut, timeout=timeout)

    async def close(self) -> None:
        if self._read_task:
            self._read_task.cancel()
        if self._transport is not None:
            try:
                self._transport.close()
            except Exception:
                pass
        self._transport = None

    # -- auth ---------------------------------------------------------------

    async def login(self) -> dict:
        """Authenticate, reusing a saved token when possible. Returns the user."""
        token = self.tokens.load()
        if token and token.get("expires_at", 0) < time.time() and token.get("refresh_token"):
            try:
                token = await asyncio.to_thread(self._refresh, token["refresh_token"])
            except RPCError as e:
                log.info("Token refresh failed, re-authorizing: %s", e)
                token = None
        if token:
            try:
                return await self._authenticate(token["access_token"])
            except RPCError as e:
                log.info("Saved token rejected, re-authorizing: %s", e)
                self.tokens.clear()

        self.on_status("waiting for you to approve in Discord")
        data = await self.command(
            "AUTHORIZE", {"client_id": self.client_id, "scopes": SCOPES}, timeout=300
        )
        token = await asyncio.to_thread(self._exchange, data["code"])
        return await self._authenticate(token["access_token"])

    async def _authenticate(self, access_token: str) -> dict:
        data = await self.command("AUTHENTICATE", {"access_token": access_token})
        self.user = data.get("user")
        return self.user or {}

    def _exchange(self, code: str) -> dict:
        token = _post_token({
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.redirect_uri,
        })
        self.tokens.save(token)
        return token

    def _refresh(self, refresh_token: str) -> dict:
        token = _post_token({
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        })
        self.tokens.save(token)
        return token

    # -- voice helpers ------------------------------------------------------

    async def get_selected_voice_channel(self) -> dict | None:
        return await self.command("GET_SELECTED_VOICE_CHANNEL")

    async def subscribe(self, evt: str, args: dict | None = None) -> None:
        await self.command("SUBSCRIBE", args or {}, evt=evt)

    async def unsubscribe(self, evt: str, args: dict | None = None) -> None:
        try:
            await self.command("UNSUBSCRIBE", args or {}, evt=evt)
        except RPCError:
            pass

    CHANNEL_EVENTS = (
        "SPEAKING_START",
        "SPEAKING_STOP",
        "VOICE_STATE_CREATE",
        "VOICE_STATE_UPDATE",
        "VOICE_STATE_DELETE",
    )

    async def watch_channel(self, channel_id: str) -> None:
        for evt in self.CHANNEL_EVENTS:
            await self.subscribe(evt, {"channel_id": channel_id})

    async def unwatch_channel(self, channel_id: str) -> None:
        for evt in self.CHANNEL_EVENTS:
            await self.unsubscribe(evt, {"channel_id": channel_id})
