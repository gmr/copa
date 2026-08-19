#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "httpx>=0.27",
#     "mcp>=2,<3",
# ]
# ///
"""stdio MCP server that bridges Claude Code to an Osaurus server-side agent.

One tool is exposed, named after the configured agent (for example
``ask_rhona``). Each call appends to the per-session conversation history,
posts the history to ``POST /agents/{id}/run``, consumes the SSE stream, and
returns the agent's final text.

Configuration (environment):
    OSAURUS_BASE_URL      Default http://127.0.0.1:1337
    OSAURUS_AGENT         Agent name to resolve through GET /agents
    OSAURUS_AGENT_ID      Agent UUID; skips name resolution
    OSAURUS_MODEL         Overrides the agent's default_model
    OSAURUS_API_KEY       osk-v1 key; only needed for non-loopback servers
    OSAURUS_HISTORY_TURNS Turns of history to resend. Default 20
    OSAURUS_TRANSCRIPT_DIR  Where Markdown transcripts are written

Run with ``--list`` to print the available agents and their UUIDs.
"""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import re
import sys
import typing
from datetime import datetime, timezone

import httpx
from mcp.server.mcpserver import MCPServer

BASE_URL = os.environ.get('OSAURUS_BASE_URL', 'http://127.0.0.1:1337').rstrip('/')
AGENT_NAME = os.environ.get('OSAURUS_AGENT', 'agent')
AGENT_ID = os.environ.get('OSAURUS_AGENT_ID')
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


def slug(value: str) -> str:
    """Return value as a lower-case identifier usable in a tool name."""
    return re.sub(r'[^a-z0-9]+', '_', value.lower()).strip('_') or 'agent'


TOOL_NAME = f'ask_{slug(AGENT_NAME)}'


def headers() -> dict[str, str]:
    value = {'Content-Type': 'application/json'}
    if API_KEY:
        value['Authorization'] = f'Bearer {API_KEY}'
    return value


async def list_agents(client: httpx.AsyncClient) -> list[dict[str, typing.Any]]:
    response = await client.get(f'{BASE_URL}/agents', headers=headers())
    response.raise_for_status()
    return response.json().get('agents', [])


async def resolve_agent(client: httpx.AsyncClient) -> tuple[str, str | None]:
    """Return the (agent id, model) to run, resolving the name once."""
    if 'id' not in RESOLVED:
        if AGENT_ID:
            RESOLVED['id'], RESOLVED['model'] = AGENT_ID, MODEL
        else:
            agents = await list_agents(client)
            match = next(
                (a for a in agents if a['name'].lower() == AGENT_NAME.lower()), None
            )
            if match is None:
                names = ', '.join(sorted(a['name'] for a in agents)) or 'none'
                raise RuntimeError(
                    f'No Osaurus agent named {AGENT_NAME!r}. Available: {names}'
                )
            RESOLVED['id'] = match['id']
            RESOLVED['model'] = MODEL or match.get('default_model')
    return RESOLVED['id'], RESOLVED['model']


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
        agent_id, model = await resolve_agent(client)
        body: dict[str, typing.Any] = {
            'messages': messages[-(HISTORY_TURNS * 2) :],
            'stream': True,
            'session_id': session_id,
        }
        if model:
            body['model'] = model
        chunks: list[str] = []
        async with client.stream(
            'POST',
            f'{BASE_URL}/agents/{agent_id}/run',
            headers=headers(),
            json=body,
        ) as response:
            if response.status_code >= 400:
                await response.aread()
                messages.pop()
                raise RuntimeError(
                    f'Osaurus returned {response.status_code}: {response.text[:500]}'
                )
            async for line in response.aiter_lines():
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
                        messages.pop()
                        raise RuntimeError(str(payload['error']))
                    chunks.append(delta(payload))
    reply = ''.join(chunks).strip()
    if not reply:
        messages.pop()
        raise RuntimeError('Osaurus agent returned an empty response.')
    messages.append({'role': 'assistant', 'content': reply})
    record(session_id, prompt, reply)
    return reply


mcp = MCPServer(name=f'osaurus-{slug(AGENT_NAME)}', version='0.1.0')


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
                    print(f'{agent["id"]}  {agent["name"]}  {agent.get("default_model")}')

        asyncio.run(show())
        return
    mcp.run('stdio')


if __name__ == '__main__':
    main()
