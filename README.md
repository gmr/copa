# Copa

A Claude Code plugin that exposes a local [Osaurus](https://docs.osaurus.ai/api)
server-side agent as a single MCP tool over stdio.

## Layout

```
copa/
├── .claude-plugin/
│   ├── plugin.json              plugin manifest
│   └── marketplace.json         single-plugin local marketplace
├── .mcp.json                    stdio MCP server declaration
├── server/osaurus_agent.py      uv-shebang single-file MCP server
├── transcripts/                 Markdown transcripts, one file per session per day
└── README.md
```

## How it works

1. `.mcp.json` runs `server/osaurus_agent.py` directly. The
   `#!/usr/bin/env -S uv run --script` shebang makes `uv` build and cache an
   isolated environment from the PEP 723 block in the file, so there is no
   virtualenv to manage.
2. On the first tool call the server calls `GET /agents` and matches
   `OSAURUS_AGENT` by name to get the agent UUID and its `default_model`. Set
   `OSAURUS_AGENT_ID` to skip the lookup.
3. The tool is named after the agent, so `OSAURUS_AGENT=Rhona` exposes
   `ask_rhona(prompt, session_id)`.
4. Conversation history is kept in memory per `session_id` and resent on each
   call, trimmed to the last `OSAURUS_HISTORY_TURNS` turns.
5. The history is posted to `POST /agents/{id}/run` with `stream: true`. Osaurus
   runs its own tool loop; the server consumes the SSE stream, keeps the
   `choices[].delta.content` text, and ignores execution hints.
6. The final text is returned to Claude Code and appended to the session
   transcript.

## Configuration

Edit the `env` block in `.mcp.json`, or set these in the shell that starts
Claude Code.

| Variable                 | Default                             | Purpose                                     |
| ------------------------ | ----------------------------------- | ------------------------------------------- |
| `OSAURUS_BASE_URL`       | `http://127.0.0.1:1337`             | Osaurus server                              |
| `OSAURUS_AGENT`          | `agent`                             | Agent name; also names the tool             |
| `OSAURUS_AGENT_ID`       | unset                               | Agent UUID; skips name resolution           |
| `OSAURUS_MODEL`          | agent `default_model`               | Model override                              |
| `OSAURUS_API_KEY`        | unset                               | `osk-v1` key; only for non-loopback servers |
| `OSAURUS_HISTORY_TURNS`  | `20`                                | Turns of history resent per call            |
| `OSAURUS_TRANSCRIPT_DIR` | `${CLAUDE_PLUGIN_DATA}/transcripts` | Transcript location                         |

## Transcripts

One Markdown file per session per UTC day:
`<transcript dir>/YYYY-MM-DD-<agent>-<session_id>.md`

```markdown
# Rhona — session `refactor-auth`

- Started: 2026-08-19T14:02:11+00:00
- Osaurus: http://127.0.0.1:1337

## 2026-08-19T14:02:11+00:00

### Claude Code

Summarize the auth flow.

### Rhona

The flow starts at ...
```

Transcripts are only written for exchanges that succeed. A failed call is also
rolled back out of the in-memory history, so a retry does not resend a turn the
agent never answered.

## Install

`uv` must be on `PATH`. Confirm the agent is reachable first:

```bash
./server/osaurus_agent.py --list      # prints agent UUIDs, names, and models
```

Set `OSAURUS_AGENT` in `.mcp.json` to the name that command prints. Then pick one
of three ways to load it.

### 1. Test without installing anything

```bash
claude --plugin-dir /Volumes/Users/gmr/Source/gmr/copa
```

The tool registers as `mcp__plugin_copa_rhona__ask_rhona`. Run `/reload-plugins`
after editing the server.

### 2. Install from the bundled marketplace

This directory is its own marketplace, so it installs by path:

```
/plugin marketplace add /Volumes/Users/gmr/Source/gmr/copa
/plugin install copa@copa
```

Installing copies the directory into the plugin cache, so re-run
`/plugin marketplace update copa` after changes.

### 3. Skip the plugin, add the MCP server directly

The plugin wrapper only exists for distribution. The server is a plain stdio MCP
server, so it can be registered on its own:

```bash
claude mcp add rhona -s user -e OSAURUS_AGENT=Rhona -e OSAURUS_TRANSCRIPT_DIR=/Volumes/Users/gmr/.osaurus/transcripts -- /Volumes/Users/gmr/Source/gmr/copa/server/osaurus_agent.py
```

Or write it into a project `.mcp.json`. Note the wrapper key, which a plugin
`.mcp.json` does not use:

```json
{
  "mcpServers": {
    "rhona": {
      "command": "/Volumes/Users/gmr/Source/gmr/copa/server/osaurus_agent.py",
      "env": {
        "OSAURUS_BASE_URL": "http://127.0.0.1:1337",
        "OSAURUS_AGENT": "Rhona",
        "OSAURUS_TRANSCRIPT_DIR": "/Volumes/Users/gmr/.osaurus/transcripts"
      }
    }
  }
}
```

Registered this way the tool is `mcp__rhona__ask_rhona`, and
`${CLAUDE_PLUGIN_DATA}` is not available, so set `OSAURUS_TRANSCRIPT_DIR` to a
real path.
