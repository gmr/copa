#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "coincurve>=20",
#     "cryptography>=43",
#     "httpx>=0.27",
#     "mcp>=2,<3",
#     "pycryptodome>=3.20",
# ]
# ///
"""stdio MCP server that bridges Claude Code to an Osaurus server-side agent.

One tool is exposed, named after the configured agent (for example
``ask_rhona``). Each call appends to the per-session conversation history,
posts the history to ``POST /agents/{id}/run``, consumes the SSE stream, and
returns the agent's final text.

The run request goes through the Osaurus Secure Channel (``/secure/session``
and ``/secure/call``). Osaurus refuses plaintext agent runs with 426 when it
does not trust the caller as local, for example when "Expose to network" is
on. The server signs the handshake with the agent key; the client verifies
the signature against ``OSAURUS_AGENT_ADDRESS`` when it is set, or else
against the agent ``address`` from ``GET /agents``.

``GET /agents`` is plaintext and carries the access key, so it is refused for
a non-loopback ``http://`` server. Set ``OSAURUS_AGENT_ID`` and
``OSAURUS_AGENT_ADDRESS`` to skip it.

Configuration (environment):
    OSAURUS_BASE_URL      Default http://127.0.0.1:1337
    OSAURUS_AGENT         Agent name to resolve through GET /agents
    OSAURUS_AGENT_ID      Agent UUID; selects the agent by id, not by name
    OSAURUS_AGENT_ADDRESS Agent address (0x...) to pin for the secure channel.
                          With OSAURUS_AGENT_ID, skips GET /agents
    OSAURUS_MODEL         Overrides the agent's default_model
    OSAURUS_API_KEY       osk-v1 key; required when Osaurus is exposed to the network
    OSAURUS_HISTORY_TURNS Turns of history to resend. Default 20
    OSAURUS_TRANSCRIPT_DIR  Where Markdown transcripts are written

Run with ``--list`` to print the available agents and their UUIDs.
"""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import hashlib
import ipaddress
import json
import os
import pathlib
import re
import sys
import typing
from datetime import datetime, timezone
from urllib.parse import urlsplit

import coincurve
import httpx
from Crypto.Hash import keccak
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from mcp.server.mcpserver import MCPServer

BASE_URL = os.environ.get('OSAURUS_BASE_URL', 'http://127.0.0.1:1337').rstrip('/')
AGENT_NAME = os.environ.get('OSAURUS_AGENT', 'agent')
AGENT_ID = os.environ.get('OSAURUS_AGENT_ID')
AGENT_ADDRESS = (os.environ.get('OSAURUS_AGENT_ADDRESS') or '').lower() or None
MODEL = os.environ.get('OSAURUS_MODEL')
API_KEY = os.environ.get('OSAURUS_API_KEY')
HISTORY_TURNS = int(os.environ.get('OSAURUS_HISTORY_TURNS', '20'))
TRANSCRIPT_DIR = pathlib.Path(
    os.environ.get('OSAURUS_TRANSCRIPT_DIR')
    or pathlib.Path(__file__).resolve().parent.parent / 'transcripts'
)

Message = dict[str, str]

HISTORY: dict[str, list[Message]] = {}
RESOLVED: dict[str, str | None] = {}

SC_VERSION = 1
SC_DOMAIN = 'osaurus-sc1'
SC_SIGNING_PREFIX = 'Osaurus Secure Channel'
SC_EXPIRY_MARGIN = 60


def slug(value: str) -> str:
    """Return value as a lower-case identifier usable in a tool name."""
    return re.sub(r'[^a-z0-9]+', '_', value.lower()).strip('_') or 'agent'


TOOL_NAME = f'ask_{slug(AGENT_NAME)}'


def headers() -> dict[str, str]:
    value = {'Content-Type': 'application/json'}
    if API_KEY:
        value['Authorization'] = f'Bearer {API_KEY}'
    return value


def plaintext_remote() -> bool:
    """Return True when BASE_URL is plain HTTP to a non-loopback host."""
    parts = urlsplit(BASE_URL)
    if parts.scheme != 'http' or parts.hostname == 'localhost':
        return False
    try:
        return not ipaddress.ip_address(parts.hostname or '').is_loopback
    except ValueError:
        return True


async def list_agents(client: httpx.AsyncClient) -> list[dict[str, typing.Any]]:
    if plaintext_remote():
        raise RuntimeError(
            f'Refusing plaintext GET /agents to {BASE_URL}: it would expose the '
            'access key and the agent address. Use https, or set '
            'OSAURUS_AGENT_ID and OSAURUS_AGENT_ADDRESS.'
        )
    response = await client.get(f'{BASE_URL}/agents', headers=headers())
    response.raise_for_status()
    return response.json().get('agents', [])


async def resolve_agent(client: httpx.AsyncClient) -> tuple[str, str | None, str]:
    """Return the (agent id, model, address) to run, resolving the agent once."""
    if 'id' not in RESOLVED and AGENT_ID and AGENT_ADDRESS:
        RESOLVED['id'], RESOLVED['model'] = AGENT_ID, MODEL
        RESOLVED['address'] = AGENT_ADDRESS
    if 'id' not in RESOLVED:
        agents = await list_agents(client)
        if AGENT_ID:
            match = next(
                (a for a in agents if a['id'].lower() == AGENT_ID.lower()), None
            )
        else:
            match = next(
                (a for a in agents if a['name'].lower() == AGENT_NAME.lower()), None
            )
        if match is None:
            names = ', '.join(sorted(a['name'] for a in agents)) or 'none'
            raise RuntimeError(
                f'No Osaurus agent {AGENT_ID or AGENT_NAME!r}. Available: {names}'
            )
        if not match.get('address'):
            raise RuntimeError(
                f'Osaurus agent {match["name"]!r} has no address, so the secure '
                'channel cannot verify it. Use a custom agent.'
            )
        if AGENT_ADDRESS and match['address'].lower() != AGENT_ADDRESS:
            raise RuntimeError(
                f'Osaurus agent {match["name"]!r} has address {match["address"]}, '
                f'not the pinned OSAURUS_AGENT_ADDRESS {AGENT_ADDRESS}.'
            )
        RESOLVED['id'] = match['id']
        RESOLVED['model'] = MODEL or match.get('default_model')
        RESOLVED['address'] = match['address'].lower()
    return RESOLVED['id'], RESOLVED['model'], RESOLVED['address']


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode()


def unb64url(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + '=' * (-len(value) % 4))


def keccak256(data: bytes) -> bytes:
    return keccak.new(digest_bits=256, data=data).digest()


def hkdf(key: bytes, salt: bytes | None, info: str) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(), length=32, salt=salt, info=info.encode()
    ).derive(key)


def sc_nonce(seq: int) -> bytes:
    """12-byte AEAD nonce: 4 zero bytes and the big-endian sequence number."""
    return bytes(4) + seq.to_bytes(8, 'big')


def recover_address(payload: bytes, signature: bytes) -> str:
    """Return the address that made a 65-byte secure channel signature."""
    if len(signature) != 65:
        raise ValueError('signature must be 65 bytes')
    header = f'\x19{SC_SIGNING_PREFIX}:\n{len(payload)}'.encode()
    recovery_id = signature[64] - 27 if signature[64] >= 27 else signature[64]
    if recovery_id not in range(4):
        raise ValueError('invalid recovery id')
    public_key = coincurve.PublicKey.from_signature_and_message(
        signature[:64] + bytes([recovery_id]),
        keccak256(header + payload),
        hasher=None,
    )
    return '0x' + keccak256(public_key.format(compressed=False)[1:])[-20:].hex()


@dataclasses.dataclass
class SecureSession:
    sid: str
    send_key: bytes
    receive_key: bytes
    expires_at: int
    next_seq: int = 1

    @property
    def usable(self) -> bool:
        return (
            datetime.now(timezone.utc).timestamp() + SC_EXPIRY_MARGIN < self.expires_at
        )


SESSIONS: dict[str, SecureSession] = {}


async def handshake(client: httpx.AsyncClient, address: str) -> SecureSession:
    """Open a secure channel session and verify that the agent signed it."""
    ephemeral = X25519PrivateKey.generate()
    hello = {
        'v': SC_VERSION,
        'agentAddress': address,
        'encPub': b64url(ephemeral.public_key().public_bytes_raw()),
        'nonce': b64url(os.urandom(16)),
    }
    response = await client.post(f'{BASE_URL}/secure/session', json=hello)
    if response.status_code != 200:
        raise RuntimeError(
            f'Secure channel handshake failed with {response.status_code}: '
            f'{response.text[:500]}'
        )
    server = response.json()
    if server.get('v') != SC_VERSION:
        raise RuntimeError(f'Unsupported secure channel version {server.get("v")!r}')
    transcript = (
        f'{SC_DOMAIN}|v={SC_VERSION}|aA={address}'
        f'|eC={hello["encPub"]}|nC={hello["nonce"]}'
        f'|sid={server["sid"]}|eS={server["encPub"]}|exp={server["expiresAt"]}'
    ).encode()
    signature = bytes.fromhex(server['signature'].removeprefix('0x'))
    if recover_address(transcript, signature) != address:
        raise RuntimeError(
            f'Secure channel identity check failed: the server is not agent {address}'
        )
    shared = ephemeral.exchange(
        X25519PublicKey.from_public_bytes(unb64url(server['encPub']))
    )
    salt = hashlib.sha256(transcript).digest()
    return SecureSession(
        sid=server['sid'],
        send_key=hkdf(shared, salt, f'{SC_DOMAIN}:c2s'),
        receive_key=hkdf(shared, salt, f'{SC_DOMAIN}:s2c'),
        expires_at=int(server['expiresAt']),
    )


async def secure_session(client: httpx.AsyncClient, address: str) -> SecureSession:
    session = SESSIONS.get(address)
    if session is None or not session.usable:
        session = SESSIONS[address] = await handshake(client, address)
    return session


class ResponseOpener:
    """Opens the response frames of one call in strict order."""

    def __init__(self, session: SecureSession, request_seq: int) -> None:
        self.sid = session.sid
        self.request_seq = request_seq
        self.aead = ChaCha20Poly1305(
            hkdf(session.receive_key, None, f'{SC_DOMAIN}:respkey:{request_seq}')
        )
        self.expected = 0
        self.finished = False

    def open(self, frame: dict[str, typing.Any]) -> bytes:
        if self.finished or frame.get('seq') != self.expected:
            raise RuntimeError('Secure channel response frame is out of order')
        fin = frame.get('fin') is True
        aad = (
            f'{SC_DOMAIN}:resp:{self.sid}:{self.request_seq}:{self.expected}:{int(fin)}'
        )
        plaintext = self.aead.decrypt(
            sc_nonce(self.expected), unb64url(frame['ct']), aad.encode()
        )
        self.expected += 1
        self.finished = fin
        return plaintext


def seal_call(
    session: SecureSession, inner: dict[str, typing.Any]
) -> tuple[dict[str, typing.Any], ResponseOpener]:
    """Seal one inner request into a ``/secure/call`` body."""
    seq = session.next_seq
    session.next_seq += 1
    aad = f'{SC_DOMAIN}:req:{session.sid}:{seq}'.encode()
    ct = ChaCha20Poly1305(session.send_key).encrypt(
        sc_nonce(seq), json.dumps(inner).encode(), aad
    )
    call = {'v': SC_VERSION, 'sid': session.sid, 'seq': seq, 'ct': b64url(ct)}
    return call, ResponseOpener(session, seq)


async def secure_stream(
    client: httpx.AsyncClient, address: str, inner: dict[str, typing.Any]
) -> typing.AsyncIterator[str]:
    """Send one request through the secure channel and yield inner SSE lines.

    Re-handshakes once when the server no longer knows the session. Raises
    when the inner status is an error or the stream ends without ``fin``.
    """
    for attempt in range(2):
        session = await secure_session(client, address)
        call, opener = seal_call(session, inner)
        async with client.stream(
            'POST',
            f'{BASE_URL}/secure/call',
            headers={'Content-Type': 'application/json', 'Accept': 'text/event-stream'},
            json=call,
        ) as response:
            if response.status_code != 200:
                await response.aread()
                if (
                    attempt == 0
                    and response.status_code == 401
                    and 'secure_session_unknown' in response.text
                ):
                    SESSIONS.pop(address, None)
                    continue
                raise RuntimeError(
                    f'Osaurus returned {response.status_code}: {response.text[:500]}'
                )
            if not response.headers.get('content-type', '').startswith(
                'text/event-stream'
            ):
                await response.aread()
                result = json.loads(opener.open(response.json()))
                body = unb64url(result.get('body') or '').decode('utf-8', 'replace')
                if result['status'] >= 400:
                    raise RuntimeError(
                        f'Osaurus returned {result["status"]}: {body[:500]}'
                    )
                for line in body.splitlines():
                    yield line
                return
            pending = ''
            async for line in response.aiter_lines():
                if not line.startswith('data:'):
                    continue
                pending += opener.open(json.loads(line[5:])).decode('utf-8')
                *lines, pending = pending.split('\n')
                for inner_line in lines:
                    yield inner_line
            if not opener.finished:
                raise RuntimeError('Osaurus response stream ended without fin frame.')
            if pending:
                yield pending
            return


def transcript_path(session_id: str) -> pathlib.Path:
    day = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    return TRANSCRIPT_DIR / f'{day}-{slug(AGENT_NAME)}-{slug(session_id)}.md'


def record(session_id: str, prompt: str, reply: str) -> None:
    """Append one exchange to the session's Markdown transcript."""
    path = transcript_path(session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).isoformat(timespec='seconds')
    if not path.exists():
        path.write_text(
            f'# {AGENT_NAME} — session `{session_id}`\n\n'
            f'- Started: {stamp}\n- Osaurus: {BASE_URL}\n',
            encoding='utf-8',
        )
    with path.open('a', encoding='utf-8') as handle:
        handle.write(f'\n## {stamp}\n\n### Claude Code\n\n{prompt}\n')
        handle.write(f'\n### {AGENT_NAME}\n\n{reply}\n')


def delta(payload: dict[str, typing.Any]) -> str:
    """Extract assistant text from one SSE payload, ignoring run hints."""
    for choice in payload.get('choices') or []:
        content = (choice.get('delta') or choice.get('message') or {}).get('content')
        if isinstance(content, str):
            return content
    return ''


async def run_agent(prompt: str, session_id: str) -> str:
    messages = HISTORY.setdefault(session_id, [])
    messages.append({'role': 'user', 'content': prompt})
    timeout = httpx.Timeout(connect=10.0, read=None, write=30.0, pool=10.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        agent_id, model, address = await resolve_agent(client)
        body: dict[str, typing.Any] = {
            'messages': messages[-(HISTORY_TURNS * 2) :],
            'stream': True,
            'session_id': session_id,
        }
        if model:
            body['model'] = model
        inner: dict[str, typing.Any] = {
            'method': 'POST',
            'path': f'/agents/{agent_id}/run',
            'accept': 'text/event-stream',
            'contentType': 'application/json',
            'body': b64url(json.dumps(body).encode()),
        }
        if API_KEY:
            inner['authorization'] = f'Bearer {API_KEY}'
        chunks: list[str] = []
        try:
            async for line in secure_stream(client, address, inner):
                if not line.startswith('data:'):
                    continue
                data = line[5:].strip()
                if not data or data == '[DONE]':
                    continue
                try:
                    payload = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if isinstance(payload, dict):
                    if 'error' in payload:
                        raise RuntimeError(str(payload['error']))
                    chunks.append(delta(payload))
        except Exception:
            messages.pop()
            raise
    reply = ''.join(chunks).strip()
    if not reply:
        messages.pop()
        raise RuntimeError('Osaurus agent returned an empty response.')
    messages.append({'role': 'assistant', 'content': reply})
    record(session_id, prompt, reply)
    return reply


mcp = MCPServer(name=f'osaurus-{slug(AGENT_NAME)}', version='0.2.1')


@mcp.tool(
    name=TOOL_NAME,
    description=(
        f'Ask the Osaurus agent {AGENT_NAME!r} a question. The agent runs its own '
        'tool loop locally and returns a final answer. Conversation history is kept '
        'per session_id, so pass the same session_id to continue a discussion.'
    ),
)
async def ask(prompt: str, session_id: str = 'default') -> str:
    return await run_agent(prompt, session_id)


def main() -> None:
    if '--list' in sys.argv:

        async def show() -> None:
            async with httpx.AsyncClient(timeout=10.0) as client:
                for agent in await list_agents(client):
                    print(
                        f'{agent["id"]}  {agent["name"]}  {agent.get("address")}  '
                        f'{agent.get("default_model")}'
                    )

        asyncio.run(show())
        return
    mcp.run('stdio')


if __name__ == '__main__':
    main()
