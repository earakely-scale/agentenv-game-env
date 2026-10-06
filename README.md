# agentenv-game-env

Game envs for [AgentEnv](https://github.com/scaleapi/agentenv-framework). A game has players, and this package gives
every game env one way to say who they are:

- **A lobby**, `urn:game:lobby/v1`, that the env serves. It holds the next game's player slots, which are filled one
  occupant at a time before the game is created. An occupant is an agent, a person, or the game's own AI.
- **`AgentEnvGameEnv`**, the base class that serves the lobby and tells each request which slot it plays. A game marks
  its own parts of the lobby with decorators.
- **Two task steps**, `add_player_slot` and `start_match`, that fill and close the lobby of any game env built on it.

The same task steps then seat players in any game: one agent against the game's AI, two models against each other, a
person beside an agent.

```
deploy_env ── your game's own step (opens the lobby with the game's settings)
                 ├─ add_player_slot {ai, faction: "orc", team: 2}
                 ├─ deploy_agent p1 ── add_player_slot {agent: "p1", faction: "human", team: 1} ──┐
                 └─ deploy_agent p2 ── add_player_slot {agent: "p2", faction: "undead", team: 1} ─┤
                                                                                                 start_match ── prompt_agent p1, p2
                                                                                  (the lobby closes: the game is created)
```

## Install

It is not on PyPI yet:

```bash
pip install "agentenv-game-env @ git+https://github.com/earakely-scale/agentenv-game-env"
```

The env side (`AgentEnvGameEnv`, the lobby, the routing) needs only `agentenv-framework-protocol`, `mcp` and
`pydantic`. In an env image, install the package with `--no-deps` beside them. The task steps need `agentenv-framework`
too.

## The example: tic-tac-toe

[`examples/tictactoe.py`](examples/tictactoe.py) is a complete game env in about 100 lines. It has two players, each an
agent or the game's AI. The lobby part:

```python
from agentenv_game import AgentEnvGameEnv, PlayerSlotSettings, check_slot, create_game, open_lobby
from agentenv_protocol import environment_card, tool


@environment_card(name="tictactoe")
class TicTacToe(AgentEnvGameEnv):

    @open_lobby                       # the lobby's settings: who moves first, and two slots
    def settings(self, additional_settings, player_slot_settings):
        ...                           # refuse anything but {"first": "x" or "o"}
        return {"first": additional_settings.get("first", "x")}, PlayerSlotSettings(
            min=2, max=2, additional_settings={"occupants": ["agent", "ai"], "factions": ["x", "o"]})

    @check_slot                       # each slot is {"faction": "x" or "o"}, one of each
    def one_mark_each(self, slot, lobby):
        ...

    @create_game                      # the lobby closed: set up the board, and let the AI move if it goes first
    async def new_game(self, lobby):
        self.players = {s.additional_settings["faction"]: s for s in lobby.slots}
        ...
        return {"first": lobby.additional_settings["first"]}

    @tool()
    async def mark(self, cell: int):
        """Put your mark in a free cell on your turn: 0 to 8, top left to bottom right."""
        mine = self.player().additional_settings["faction"]    # the slot this request plays
        ...
```

### Run it, and fill its lobby

```bash
MCP_PORT=18765 python examples/tictactoe.py
```

```bash
L=http://127.0.0.1:18765/agentenv/ext/lobby
curl -s -XPOST $L/open -d '{"additional_settings": {"first": "x"}}'
```
```json
{"state":"open","additional_settings":{"first":"x"},
 "player_slot_settings":{"min":2,"max":2,"additional_settings":{"occupants":["agent","ai"],"factions":["x","o"]}},
 "slots":[]}
```

An agent's slot comes back with its address. A second "x" is refused by the game's own check:

```bash
curl -s -XPOST $L/fill -d '{"occupant": {"kind": "agent", "name": "alice"}, "additional_settings": {"faction": "x"}}'
curl -s -XPOST $L/fill -d '{"occupant": {"kind": "agent", "name": "bob"}, "additional_settings": {"faction": "x"}}'
curl -s -XPOST $L/fill -d '{"occupant": {"kind": "ai"}, "additional_settings": {"faction": "o"}}'
curl -s -XPOST $L/close
```
```json
{"slot":0,"occupant":{"kind":"agent","name":"alice"},"additional_settings":{"faction":"x"},
 "connect":{"path":"/players/alice/mcp","headers":{}}}
{"ok":false,"error":{"code":"bad_settings","message":"x is taken"}}                        (HTTP 400)
{"slot":1,"occupant":{"kind":"ai"},"additional_settings":{"faction":"o"}}
{"state":"closed", ..., "slots":[...], "game":{"first":"x"}}
```

### Play it

Alice's MCP client connects to her slot's address, `/players/alice/mcp`. Every tool call there plays her slot:

```
> show_board                         > mark {"cell": 4}
You are x.                           o |   |
  |   |                                | x |
  |   |                                |   |
  |   |                              x to play.
x to play.
```

The game's AI answered at once, in the top left. At `/players/carol/mcp` the tools refuse: carol plays no slot in this
game. At the env's own `/mcp` they refuse too, because a request there plays no one.

### In an AgentEnv task

Register the example as an MCP server env, as with any env (its image runs `python tictactoe.py`). A task then seats an
agent against the game's AI:

```json
[
  {"id": "deploy", "type": "deploy_env", "env_id": "tictactoe"},
  {"id": "agent", "type": "deploy_agent", "agent_name": "alice", "a2a_agent_id": "your-agent", "env_ids": []},
  {"id": "seat-alice", "type": "add_player_slot", "env_id": "tictactoe", "depends_on": ["deploy", "agent"],
   "occupant": {"kind": "agent", "name": "alice"}, "additional_settings": {"faction": "x"}},
  {"id": "seat-ai", "type": "add_player_slot", "env_id": "tictactoe", "depends_on": ["deploy"],
   "occupant": {"kind": "ai"}, "additional_settings": {"faction": "o"}},
  {"id": "start", "type": "start_match", "env_id": "tictactoe", "depends_on": ["seat-alice", "seat-ai"]},
  {"id": "play", "type": "prompt_agent", "agent_name": "alice", "depends_on": ["start"],
   "prompt": "You play tic-tac-toe through the tools. Win."}
]
```

- **The agent deploys with `"env_ids": []`.** `add_player_slot` gives it its slot's address, so it plays alice's
  slot, not the env's own address.
- **A second agent in place of the AI** makes it model against model: deploy `bob` and seat him as `"o"`.

## The lobby protocol: `urn:game:lobby/v1`

### Lifecycle

```
            open(settings)              fill(...) ×N                  close()
 (none) ─────────────────► OPEN ──────────────────────► OPEN ─────────────────► CLOSED
                            ▲                                                     │
                            └──────────────── open(settings) again: drops the game ┘
```

- **`open`** starts an empty lobby for a game with these settings, dropping any current game.
- **`fill`** adds one occupant while the lobby is open.
- **`close`** creates the game from the filled slots. After that the lobby is read-only, and the game is the env's
  business (turns, start gates, results).

An env whose lobby was never opened has an open one with the game's default settings.

### Methods

They're advertised on the env's card, in the `urn:game:lobby/v1` extension's `params.methods`, and called with
agentenv-protocol's `client.invoke_extension(base, card, LOBBY, params, method=...)`.

| Method | Route | Request | Response |
|---|---|---|---|
| `open` | `POST /agentenv/ext/lobby/open` | `{"additional_settings": {...}, "player_slot_settings": {...}}`, both optional | the lobby |
| `get` | `GET /agentenv/ext/lobby` | | the lobby |
| `fill` | `POST /agentenv/ext/lobby/fill` | `{"occupant": {...}, "slot": n, "additional_settings": {...}}`; `slot` and `additional_settings` optional | the filled slot |
| `close` | `POST /agentenv/ext/lobby/close` | `{}` | the lobby, plus `"game"`: what the game reports of itself |

### Types

| Type | Fields |
|---|---|
| `Lobby` | `state`: `LobbyState`, `open` or `closed`. `additional_settings`: the game's and the task's own settings (a map, a seed, a time limit), opaque to the protocol. `player_slot_settings`: `PlayerSlotSettings` or absent (no limits). `slots`: the filled `PlayerSlot`s, by slot number. |
| `PlayerSlotSettings` | `min`: at close, at least this many slots filled. `max`: no more than this many, numbered from 0. `additional_settings`: the game's own description of its slots (which occupants, factions, teams and AI levels it takes), opaque. All optional. |
| `PlayerSlot` | `slot`: its number. `occupant`: an `Occupant`. `additional_settings`: the game's own settings for the slot (faction, team, AI level, a display label), opaque. `connect`: for an agent slot, set by the env. `play`: for a human slot, set by the env. |
| `Occupant` | `kind`: `OccupantKind`. `name`: required for agents and people, optional for the AI; letters, digits, `_`, `.` and `-`, at most 64. |
| `Connect` | `path`: under the env's address. `headers`: to send with every request. |

The three occupant kinds:

| `kind` | Who | Gets |
|---|---|---|
| `agent` | anything that plays through the env's API: a model agent, a scripted bot, a person with an MCP client | `connect`: the path and headers whose requests play this slot |
| `human` | a person, through the game's own UI | `play`: a link into the game (a game that takes people says how) |
| `ai` | the game's built-in AI | nothing |

**How an agent connects is the env's choice.** By default its slot's address is `/players/<name>/mcp`. A game that
routes by header returns `{"path": "/mcp", "headers": {"X-Game-Player": "<name>"}}`. `add_player_slot` registers the
env's address plus the path, and the headers, with the agent. The agent never needs to know which scheme its game
uses.

### Checks and errors

| Checked by | What |
|---|---|
| the lobby, for every game | the lobby is open; the slot number is in range and free; no more than `max` slots; an agent or a person has a valid, unique name; at close, at least `min` slots |
| the game (its decorated methods) | everything in the `additional_settings` at both levels, such as a known map, a valid faction, one slot per faction, or one AI level for the game; and whether it takes people at all |

A refusal is an HTTP 400 with the protocol's error body, `{"ok": false, "error": {"code": ..., "message": ...}}`:

| Code | When |
|---|---|
| `lobby_closed` | a fill after close, or while the game is being created |
| `lobby_full` | every slot up to `max` is taken |
| `bad_slot` | a slot number of `max` or more |
| `slot_taken` | the slot is filled |
| `name_taken` | the name already has a slot, with other settings |
| `bad_occupant` | an occupant that isn't valid, or a person in a game that takes none |
| `bad_settings` | the game refused the lobby's or the slot's settings; the message says why |
| `too_few_slots` | close with fewer than `min` slots |
| `bad_request` | a body that isn't a JSON object, or that has unknown fields |

If creating the game fails, `close` answers 500 with `lobby_failed`. The lobby stays open, so the close can be retried.

**Repeats are safe.** A fill with the same name, slot and settings as one the lobby has returns that slot, and closing
a closed lobby returns its game again. A task that resumes after a failure can rerun its lobby steps.

## Writing a game env

Subclass `AgentEnvGameEnv` and mark the game's parts of the lobby, the way agentenv-protocol's data plane marks
`@reset_data`:

| Decorator | Called | Takes (after `self`) | Returns | |
|---|---|---|---|---|
| `@create_game` | when the lobby closes | `lobby` | a dict: what callers learn of the game (start locations, say) | required, `async` |
| `@open_lobby` | when a lobby opens, and for the first, default lobby | `additional_settings`, `player_slot_settings` | the two, as the lobby keeps them: checked, defaults filled in, slot limits set | optional |
| `@check_slot` | before the lobby takes a slot | `slot`, `lobby` | nothing; raise `ValueError` to refuse | optional |
| `@connect_slot` | for an agent slot | `slot` | a `Connect` | optional; default `/players/<name>/mcp` |
| `@play_link` | for a human slot | `slot` | a link | optional; without it, the game takes no people |

- **One method per decorator, across the class and its bases.** The method name is yours.
- **A `ValueError` from your method is the caller's `bad_settings`,** with your message.
- **The marks are checked before the env serves.** That covers the count, `@create_game` being present and async, and
  the number of arguments. A mistake fails at `create_app()` or the first lobby call, not mid-game.

In the game's own tools and extensions, **`self.player()`** is the agent slot the current request plays. It's `None`
at the env's own address, and a request for a name without a slot is refused. To route by a header instead of a path,
set `player_header = "X-Game-Player"` on the class.

**In process,** as tests or a game's own default setup use it: `self.lobby`, `self.new_lobby(...)`,
`self.fill_slot(SlotRequest(...))` and `await self.close_lobby()` do what the four methods do.

**Serving:** `serve()` or `create_app()`, as for any AgentEnv environment. `create_app()` adds the lobby's routes and
the player routing (the SDK serves one handler per extension, so the lobby's other methods are added there).
`mount()` onto an app of your own isn't supported.

## The task steps

**`add_player_slot`** fills one slot:

| Field | |
|---|---|
| `env_id` | the game env |
| `occupant` | `{"kind": "agent" \| "human" \| "ai", "name": ...}` |
| `slot` | optional: the next free one by default |
| `additional_settings` | optional: the game's settings for the slot |
| `register` | agents only, default `true`: register the slot's address with the `deploy_agent` agent of that name. `false` only reserves the slot, for a player that connects on its own |
| `timeout_seconds` | default `60` |

- **The agent is checked before the slot is taken,** so a missing agent leaves no slot behind.
- **The agent must not already have the env's own address,** or it would play there instead. Deploy players with
  `"env_ids": []`.
- **A person's link is logged** as `PLAY slot <n> (<name>): <url>`.
- **Every slot is kept in the run's `metadata["game_slots"]`,** by name (or `slot-<n>` for an unnamed AI), with its
  full `url` or `play_url`.

**`start_match`** closes the lobby, which creates the game. It takes `env_id` and `timeout_seconds` (default `900`).
Put it after every `add_player_slot` of the game and before its players play. The closed lobby and the game's report
are kept in `metadata["game_lobby"]`.

## Development

```bash
uv venv && uv pip install -e ".[dev]"
.venv/bin/pytest          # the lobby in process, served over HTTP, players over MCP, and the steps with a fake agent
.venv/bin/ruff check .
```

## Not done yet

- **More of what games share,** as further parts of `AgentEnvGameEnv`: a start gate (the game begins when every player
  has made its first move), holds, finish rules, per-slot results in the end-of-game summary, and a spectator timeline.
  All exist for Warcraft III in [agentenv-wc3-plugin](https://github.com/earakely-scale/agentenv-wc3-plugin)'s
  `agentenv_rts`.
- **The lobby stops at close.** It doesn't show the game's start (who is ready). There's no `leave` either: a rerun
  opens a new lobby.
- **A slot's address isn't a secret.** Any client that can reach the env can use another player's path or header. A
  per-slot token in `connect.headers` would close that.
- **No generic step opens the lobby.** Each game's own step does it, with its typed settings.

## License

Apache-2.0
