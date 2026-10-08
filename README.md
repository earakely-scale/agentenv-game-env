# agentenv-game-env

Game envs for [AgentEnv](https://github.com/scaleapi/agentenv-framework). A game has players and a course, and this
package gives every game env one way to say who plays it and how it is going:

- **A lobby**, `urn:game:lobby/v1`, that the env serves. It holds the next game's player slots, which are filled one
  player at a time before the game is created. A player is an agent, a person, or the game's own AI.
- **An env card for each player slot** that an agent or a person plays: the env as that player sees it, with the
  interfaces it plays through.
- **A match**, `urn:game:match/v1`, that the lobby's close creates: the game from its start gate to its end, with its
  progress and each player's outcome and scores.
- **`AgentEnvGameEnv`**, the base class that serves them and tells each request which player slot it plays. A game
  declares its settings as pydantic models and marks its own parts of the lobby and the match with decorators.
- **A license**, `urn:game:license/v1`, for a game that needs something from its user to run (license files, keys,
  terms to accept), which must never be in its image, a task file or a reply.
- **Six task steps**: `add_license` gives a game its license from agent-env's secret store; `open_lobby`,
  `add_player_slot` and `close_lobby` open, fill and close the lobby of any game env built on it; and `finish_match`
  and `cancel_match` end its match.

The same task steps then put players in any game: one agent against the game's AI, two models against each other, a
person beside an agent. Warcraft III's env, in
[agentenv-wc3-plugin](https://github.com/earakely-scale/agentenv-wc3-plugin), is built on it. Its lobby takes a
match's settings (map, seed, time limit, clock), and its player slots take a race, a team, a label and, for the game's
AI, a level.

```
deploy_env ── open_lobby (with the game's settings)
                 ├─ add_player_slot {player_id: "2", ai, faction: "orc", team: 2}
                 ├─ deploy_agent p1 ── add_player_slot {player_id: "0", agent p1, faction: "human", team: 1} ──┐
                 └─ deploy_agent p2 ── add_player_slot {player_id: "1", agent p2, faction: "undead", team: 1} ─┤
                                                                                                    close_lobby ── prompt_agent p1, p2 ── finish_match ── grade
                                                                             (the game and its match are created)                (played out to its end)
```

## Install

It is not on PyPI yet:

```bash
pip install "agentenv-game-env @ git+https://github.com/earakely-scale/agentenv-game-env"
```

The env side (`AgentEnvGameEnv`, the lobby, the match, the routing) needs only `agentenv-framework-protocol`, `mcp` and
`pydantic`. In an env image, install the package with `--no-deps` beside them. The task steps need `agentenv-framework`
too.

## The example: tic-tac-toe

[`examples/tictactoe.py`](examples/tictactoe.py) is a complete game env in about 120 lines. It has two players, `x` and
`o`, each an agent or the game's AI. A player slot's id is its mark. The lobby and match parts:

```python
from agentenv_game import AgentEnvGameEnv, Counter, GameError, MatchReport, MatchStatus, PlayerKind, PlayerSlotLimits
from agentenv_game import check_player_slot, create_game, match_report, player_slot_limits
from agentenv_protocol import environment_card, tool


@environment_card(name="tictactoe")
class TicTacToe(AgentEnvGameEnv):
    class GameSettings(BaseModel):    # the lobby's settings: checked, defaults filled in, schema in the card
        model_config = ConfigDict(extra="forbid", use_attribute_docstrings=True)
        first: Literal["x", "o"] = "x"
        """Who moves first."""

    @player_slot_limits               # two players, each an agent or the game's AI
    def two_players(self, game_settings, requested):
        ...                           # refuse a request for more
        return PlayerSlotLimits(min=2, max=2, player_kinds=[PlayerKind.AGENT, PlayerKind.AI])

    @check_player_slot                # the player slots are "x" and "o"
    def a_mark(self, slot, lobby):
        if slot.player_id not in ("x", "o"):
            raise GameError("bad_slot", ...)

    @create_game                      # the lobby closed: set up the board, and let the AI move if it goes first
    async def new_game(self, lobby):
        self.players = {s.player_id: s for s in lobby.player_slots}
        self.board, self.turn = [" "] * 9, lobby.game_settings["first"]
        ...

    @match_report                     # how the match is going: read whenever the match is, kept once it's over
    def report(self):
        over = self.winner is not None
        return MatchReport(
            status="finished" if over else "started",
            status_detail=(f"{self.winner} won" if self.winner in ("x", "o") else "a draw") if over else None,
            progress=[Counter(name="game", unit="moves", value=9 - self.board.count(" "), limit=9)],
            outcomes={...} if over else {})                    # "x": "won", "o": "lost"

    @tool()
    async def mark(self, cell: int):
        """Put your mark in a free cell on your turn: 0 to 8, top left to bottom right."""
        mine = self.player().player_id     # the player slot this request plays
        if self.match.status is MatchStatus.STARTED and ...:
            ...
```

### Run it, and fill its lobby

```bash
MCP_PORT=18765 python examples/tictactoe.py
```

```bash
L=http://127.0.0.1:18765/agentenv/ext/lobby
curl -s -XPOST $L/open -d '{"game_settings": {"first": "x"}}'
```
```json
{"lobby_id":"lb-cb04d0b9","status":"open","game_settings":{"first":"x"},
 "player_slot_limits":{"min":2,"max":2,"player_kinds":["agent","ai"]},"player_slots":[],"player_teams":[]}
```

An agent's player slot comes back with its environment. A second `x` is refused:

```bash
curl -s -XPOST $L/fill -d '{"player_id": "x", "player_kind": "agent", "player_name": "alice"}'
curl -s -XPOST $L/fill -d '{"player_id": "x", "player_kind": "agent", "player_name": "bob"}'
curl -s -XPOST $L/fill -d '{"player_id": "o", "player_kind": "ai"}'
curl -s -XPOST $L/close
```
```json
{"player_id":"x","player_kind":"agent","player_name":"alice","game_settings":{},"environment_url":"/players/x","headers":{}}
{"ok":false,"error":{"code":"slot_taken","message":"player slot 'x' is taken"}}               (HTTP 400)
{"player_id":"o","player_kind":"ai","game_settings":{},"headers":{}}
{"lobby_id":"lb-cb04d0b9","status":"closed", ..., "player_slots":[...],
 "player_teams":[{"team_id":"x","player_ids":["x"]},{"team_id":"o","player_ids":["o"]}]}
```

Alice's player slot serves its own env card at its `environment_url`:

```bash
curl -s http://127.0.0.1:18765/players/x/.well-known/agent-env.json
```
```json
{"name":"tictactoe/x","protocolVersion":"1.0","url":"/agentenv","preferredTransport":"JSONRPC",
 "additionalInterfaces":[{"url":"/mcp","transport":"mcp"}],"capabilities":{"operations":[]}}
```

### Play it

Alice's MCP client connects to the MCP interface of her player slot's card, `/players/x/mcp`. Every tool call there
plays her slot:

```
> show_board                         > mark {"cell": 4}
You are x.                           o |   |
  |   |                                | x |
  |   |                                |   |
  |   |                              x to play.
x to play.
```

The game's AI answered at once, in the top left. At `/players/carol/mcp` the tools refuse, because no player slot
`carol` is played in this game. At the env's own `/mcp` they refuse too, because a request there plays no one.

### Follow its match

The match was created when the lobby closed. Tic-tac-toe has nothing to line up before the first move, so it started at
once. Before the close, `GET` answers `null`:

```bash
M=http://127.0.0.1:18765/agentenv/ext/match
curl -s $M          # after the close
curl -s $M          # after alice plays 4, 2 and 6, and wins
```
```json
{"lobby_id":"lb-cb04d0b9","status":"started","progress":[{"name":"game","unit":"moves","value":0,"limit":9}],
 "player_states":{"x":{"status":"undecided"},"o":{"status":"undecided"}}}
{"lobby_id":"lb-cb04d0b9","status":"finished","status_detail":"x won",
 "progress":[{"name":"game","unit":"moves","value":5,"limit":9}],
 "player_states":{"x":{"status":"won"},"o":{"status":"lost"}}}
```

A finished match never changes, and a mark refuses: `Not marked: the match is finished.` In a second game, alice
stops after one mark and the harness finishes the match. Tic-tac-toe can't be played on without its players, so it
ends `cancelled`:

```bash
curl -s -XPOST $M/finish -d '{"lobby_id": "lb-ba0bc889"}'
```
```json
{"lobby_id":"lb-ba0bc889","status":"cancelled","status_detail":"tictactoe can't be played out without its players",
 "progress":[{"name":"game","unit":"moves","value":2,"limit":9}],
 "player_states":{"x":{"status":"undecided"},"o":{"status":"undecided"}}}
```

### In an AgentEnv task

Register the example as an MCP server env, as with any env (its image runs `python tictactoe.py`). A task then puts an
agent against the game's AI:

```json
[
  {"id": "deploy", "type": "deploy_env", "env_id": "tictactoe"},
  {"id": "agent", "type": "deploy_agent", "agent_name": "alice", "a2a_agent_id": "your-agent", "env_ids": []},
  {"id": "lobby", "type": "open_lobby", "env_id": "tictactoe", "game_settings": {"first": "o"},
   "depends_on": ["deploy"]},
  {"id": "slot-x", "type": "add_player_slot", "env_id": "tictactoe", "depends_on": ["lobby", "agent"],
   "player_id": "x", "player_kind": "agent", "player_name": "alice"},
  {"id": "slot-o", "type": "add_player_slot", "env_id": "tictactoe", "depends_on": ["lobby"],
   "player_id": "o", "player_kind": "ai"},
  {"id": "close", "type": "close_lobby", "env_id": "tictactoe", "depends_on": ["slot-x", "slot-o"]},
  {"id": "play", "type": "prompt_agent", "agent_name": "alice", "depends_on": ["close"],
   "prompt": "You play tic-tac-toe through the tools. Win."},
  {"id": "finish", "type": "finish_match", "env_id": "tictactoe", "depends_on": ["play"]}
]
```

- **The agent deploys with `"env_ids": []`.** `add_player_slot` gives it its player slot's MCP address, so it plays
  `x`, not the env's own address.
- **A second agent in place of the AI** makes it model against model: deploy `bob` and give him player slot `"o"`.
- **`finish_match` leaves the final match** in the run's `metadata["game_match"]`, for a grader to read.

## The lobby protocol: `urn:game:lobby/v1`

### Lifecycle

```
              open                   close                        the match (urn:game:match/v1)
 not_opened ───────► open ──────────────────────────► closed ───► not_started → started → ...
                      │  fill ×N      ├─ cancel ─────► cancelled   (no game)
                      │               └─ the game can't be created ─► failed   (no game; close answers 500)
                      └─ open again: a new lobby, dropping this one and its game
```

- **`open`** starts a new, empty lobby with these settings and a new `lobby_id`, dropping the last lobby and its game.
- **`fill`** adds one player while the lobby is open.
- **`close`** creates the game and its match from the filled player slots. After that the lobby is read-only, and
  the match takes over: [The match protocol](#the-match-protocol-urngamematchv1).
- **`cancel`** abandons an open lobby, for a run that ends before its game starts.
- **`closed`, `cancelled` and `failed` are final for that lobby.** Only a new `open` changes them. A game that can't be
  created from the slots fails the lobby: a retry opens a new one and fills it again.

Before any `open`, the lobby is `not_opened`, with the game's default settings. A game can use that to offer its own
default game to a client that never opens one.

### Methods

They're advertised on the env's card, in the `urn:game:lobby/v1` extension's `params.methods`, and called with
agentenv-protocol's `client.invoke_extension(base, card, LOBBY, params, method=...)`. Each method's `request` is a
JSON Schema, and `open`'s and `fill`'s include the game's own settings models, so a client can see what a game takes.

| Method | Route | Request | Response |
|---|---|---|---|
| `open` | `POST /agentenv/ext/lobby/open` | `{"game_settings": {...}, "player_slot_limits": {...}}`, both optional | the lobby |
| `get` | `GET /agentenv/ext/lobby` | | the lobby |
| `fill` | `POST /agentenv/ext/lobby/fill` | `{"player_id": ..., "player_kind": ..., "player_name": ..., "game_settings": {...}, "lobby_id": ...}`; `player_name`, `game_settings` and `lobby_id` optional | the filled player slot |
| `close` | `POST /agentenv/ext/lobby/close` | `{"lobby_id": ...}`, optional | the lobby |
| `cancel` | `POST /agentenv/ext/lobby/cancel` | `{"lobby_id": ...}`, optional | the lobby |

**A `lobby_id` guards against a lobby opened since.** `fill`, `close` and `cancel` refuse a `lobby_id` that isn't the
current lobby's (`lobby_replaced`). The task steps always send the one `open_lobby` opened.

### Types

| Type | Fields |
|---|---|
| `Lobby` | `lobby_id`: new on each open. `status`: `LobbyStatus`, `not_opened`, `open`, `closed`, `cancelled` or `failed`. `game_settings`: the game's own (a map, a seed, a time limit), checked against its `GameSettings`, every default filled in. `player_slot_limits`: a `PlayerSlotLimits`. `player_slots`: the filled `PlayerSlot`s, in the order they were filled, which means nothing. `player_teams`: the `PlayerTeam`s. |
| `PlayerSlotLimits` | `min`: at close, at least this many player slots filled. `max`: no more than this many. `player_kinds`: the kinds of player the game takes, `["agent"]` by default. |
| `PlayerTeam` | `team_id`: the game's own name for the team. `player_ids`: its members. Every player slot is on exactly one team: allies share one, opponents none. The game's `@player_teams` makes them, else each player slot is a team of its own. They follow the slots as they fill, so they are fixed when the lobby closes. |
| `PlayerSlot` | `player_id`: the game's own name for the player slot (a player number, a role, a mark), which the protocol never interprets. `player_kind`: `PlayerKind`. `player_name`: who plays it; for an agent, the agent-env agent the slot is registered with; never for the game's AI. `game_settings`: the game's own for the slot (faction, team, AI level, a display label), checked against its `PlayerSlotSettings`. `environment_url`: for an agent or a person, where the player slot's env card is served. `headers`: what a client sends to reach it. |

`player_id` and `player_name` are letters, digits, `_`, `.` and `-`, at most 64, so they can appear in paths and keys.
A `player_id` is a string even when it's a number (`"0"`, Warcraft III's player number), so nothing does arithmetic
on it.

The three kinds of player:

| `player_kind` | Who | Gets |
|---|---|---|
| `agent` | anything that plays through the env's API: a model agent, a scripted bot, a person with an MCP client | an environment whose card has an MCP interface |
| `human` | a person, through the game's own UI | an environment whose card has a page to open (an `http` interface), if the game takes people |
| `ai` | the game's built-in AI | nothing: the game plays it |

### A player slot's env card

An env can be reached through several interfaces, and its card lists them. A player slot is the env as one player sees
it, so it has a card of its own, at `<environment_url>/.well-known/agent-env.json`:

```json
{"name": "tictactoe/x", "additionalInterfaces": [{"url": "/mcp", "transport": "mcp"}], "capabilities": {"operations": []}}
```

- **Its URLs are relative to its environment,** as any card's are: `/mcp` here is `/players/x/mcp`, and
  agentenv-protocol's `client.mcp_path(card)` finds it.
- **The game decides what's in it.** By default it has one MCP interface. A game that takes people gives them a page
  there, and later a player slot could also be played by screen, or have its own tools or extensions.
- **It exists while the player slot does.** It appears when the slot is filled, and a new `open` replaces it. An id
  that plays no agent or human slot gets a 404 (`unknown_player`).

### Checks and errors

| Checked by | What |
|---|---|
| the lobby, for every game | the lobby is open and is the one meant; the player's kind is one the game takes; the game's AI has no name; the `player_id` is free; a name plays one player slot; no more than `max` slots; at close, at least `min` slots and the game's license |
| the game's settings models | every field of `game_settings` at both levels, and the defaults |
| the game's decorated methods | what a model can't say: the `player_id`s it has, rules across player slots (one AI level for every AI), or by the kind of player |

A refusal is an HTTP 400 with the protocol's error body, `{"ok": false, "error": {"code": ..., "message": ...}}`:

| Code | When |
|---|---|
| `lobby_not_open` | a fill, close or cancel when the lobby isn't open; the message says what it is |
| `lobby_replaced` | a `lobby_id` that isn't the current lobby's |
| `lobby_full` | every player slot up to `max` is taken |
| `bad_slot` | a `player_id` the game doesn't have |
| `slot_taken` | the player slot is filled, by another player or with other settings |
| `name_taken` | the name already plays another player slot |
| `bad_player` | a player that isn't valid, a kind the game doesn't take, or a named AI |
| `bad_settings` | the game refused the lobby's or the player slot's settings; the message says which and why |
| `too_few_slots` | close with fewer than `min` player slots |
| `not_licensed` | close while the game lacks part of its license ([Licenses](#licenses-urngamelicensev1)) |
| `bad_request` | a body that isn't a JSON object, or that has unknown fields |

If creating the game fails, `close` answers 500 with `lobby_failed`, and the lobby is `failed`.

**Repeats are safe.** A fill with the same `player_id`, player and settings as one the lobby has returns that player
slot, and closing a closed lobby, or cancelling a cancelled one, returns it again. A task that resumes after a failure
can rerun its lobby steps.

## The match protocol: `urn:game:match/v1`

Closing the lobby creates the game and its match: the game from its start to its end. The lobby says who plays, by its
player slots and teams. The match says how it's going, by `player_id`.

### Lifecycle

```
 the lobby closes ─► not_started ─► started ─► finished      by the game's rules or its limit; finish plays it out
                                      ⇅
                                    paused                   by the game, which resumes it

 not_started, started or paused ─► cancelled                 cancel, or finish in a game that can't be played out
                                 ─► failed                   the engine broke
```

| `status` | |
|---|---|
| `not_started` | The game exists, and waits for every player to be ready. |
| `started` | Being played. |
| `paused` | Suspended by the game, which resumes it: a realtime game whose player has lost its connection, say. Game time doesn't pass. |
| `finished` | Ended on its own, by the game's rules or at its limit. |
| `cancelled` | The harness ended it first. |
| `failed` | The engine broke. |

- **`finished`, `cancelled` and `failed` are final:** after one, nothing in the match changes.
- **`status_detail` is the game's own words** beside the status: "x won", "out of time", the engine's error. Show it;
  never parse it.
- **Each player's `status`** is `not_ready` or `ready` before the start, `undecided` once the match starts, and `won`,
  `lost` or `drawn` as the game's rules decide. A player the game didn't decide by the end stays `undecided`.

**The start gate.** A game whose first moves must line up, such as a realtime game whose clock would otherwise run
while its agents read their briefings, marks a `@begin_game` method. Its match starts when every player is ready.
A player is ready by its first move, which the game reports with `player_ready`, or by the method, for a player that
starts without moving. The game's AI starts ready. A game can also start without a silent player after a while
(`begin_match`, its own stall rule), saying who was missing in `status_detail`. A game without `@begin_game`, like
tic-tac-toe, has nothing to line up, and its match starts as the lobby closes.

### Methods

Advertised on the env's card, in the `urn:game:match/v1` extension's `params.methods`, and called with
`client.invoke_extension(base, card, MATCH, params, method=...)`.

| Method | Route | Request | Response |
|---|---|---|---|
| `get` | `GET /agentenv/ext/match` | | the match; `null` until the lobby closes |
| `player_ready` | `POST /agentenv/ext/match/player_ready` | `{"player_id": ..., "lobby_id": ...}`; `lobby_id` optional | the match |
| `finish` | `POST /agentenv/ext/match/finish` | `{"lobby_id": ...}`, optional | the match, final |
| `cancel` | `POST /agentenv/ext/match/cancel` | `{"lobby_id": ...}`, optional | the match, final |

- **`finish` plays the match out,** for a run whose agents have stopped. The game takes no more moves from agents and
  people, and runs to the end its rules or its limit set, so every match is graded at its end. It replies once the
  match is final: for a realtime game, up to the rest of its time limit. A match that hasn't started starts first. A
  game that can't advance without its players (tic-tac-toe, untimed chess) has nothing to play out, and its match ends
  `cancelled`.
- **`cancel` ends the match where it stands.**
- **On a final match, `finish` and `cancel` return it unchanged,** so cleanup can always cancel. `player_ready` for
  the game's AI, or once the match has started, also returns it unchanged.
- **Nobody but the game pauses a match yet.** `pause` and `resume` methods come when something needs them.

### Types

| Type | Fields |
|---|---|
| `Match` | `lobby_id`: the lobby it was created from; one match per lobby, so it names the match too. `status`: `MatchStatus`. `status_detail`. `progress`: `Counter`s, outermost first. `player_states`: a `PlayerState` for each of the lobby's player slots, by `player_id`. |
| `Counter` | `name`. `unit`: free text; `seconds` (game time), `wall_seconds` and `turns` are documented. `value`: from 0. `limit`: ends this scope, and the first counter's ends the match. `rate`: units per wall-clock second while it runs on its own; `0` stopped, absent only as players act. |
| `PlayerState` | `status`: `PlayerStatus`. `scores`: `Score`s, the first the game's main score. |
| `Score` | `name`: the same name is the same measure on every player that has it. `value`. `better`: `higher` or `lower`. `unit`. |

A Total War campaign would report `[{"name": "campaign", "unit": "turns", "value": 12, "limit": 100}, {"name":
"battle", "unit": "seconds", "value": 340, "limit": 1200, "rate": 1.0}]` during a battle: the inner counter is there
only while its scope is.

### Errors

| Code | When |
|---|---|
| `no_match` | the lobby hasn't closed |
| `lobby_replaced` | a `lobby_id` that isn't the current lobby's |
| `bad_player` | `player_ready` for a `player_id` that plays no player slot of this match |
| `bad_request` | a body that isn't a JSON object, has unknown fields, or lacks `player_id` |

If the game breaks while it starts or plays out, the method answers 500 with `match_failed`, and the match is `failed`.

## Writing a game env

Subclass `AgentEnvGameEnv`. Declare the game's settings as pydantic models:

| Attribute | Checks | Its schema goes in |
|---|---|---|
| `GameSettings` | the lobby's `game_settings`, at open | the card's `open` request |
| `PlayerSlotSettings` | each player slot's `game_settings`, at fill | the card's `fill` request |

A nested class, as in the example, or an existing model assigned (`GameSettings = MySettings`). Without one, the game
takes no settings there. `ConfigDict(extra="forbid", use_attribute_docstrings=True)` refuses unknown keys and puts each
field's docstring in the published schema. A rule about one model's fields, such as a map that exists, is a pydantic
validator on it.

Then mark the game's parts, the way agentenv-protocol's data plane marks `@reset_data`:

| Decorator | Called | Takes (after `self`) | Returns | |
|---|---|---|---|---|
| `@create_game` | when the lobby closes | `lobby` | nothing; a failure fails the lobby | required, `async` |
| `@player_slot_limits` | when a lobby opens | `game_settings` (its `GameSettings`), `requested` (the opener's `PlayerSlotLimits`) | the `PlayerSlotLimits` the lobby keeps | optional; without it, the request as given |
| `@check_player_slot` | before the lobby takes a player slot | `slot`, `lobby` | nothing; raise `ValueError` to refuse | optional |
| `@player_slot_card` | for an agent's or a person's player slot | `slot` | its `EnvironmentCard` | optional; default: one MCP interface |
| `@player_teams` | as player slots fill | `lobby` | its `PlayerTeam`s, every player slot on exactly one | optional; default: a team per player slot |
| `@match_report` | on every read of the match, until it's final | nothing | a `MatchReport` | optional; without it, the match has only its lifecycle |
| `@begin_game` | once, as the match starts | nothing | nothing; a failure fails the match | optional, `async`; without it, the match starts as the lobby closes |
| `@play_out` | by `finish` | nothing | nothing, once the game's rules or its limit have ended the match; it takes no player moves | optional, `async`; without it, `finish` cancels |
| `@license_needs` | for the license's status, and before the lobby closes | nothing | the `LicenseItem`s the game still lacks; empty when licensed | optional, with `@install_license` |
| `@install_license` | when parts arrive | `parts`: a `LicenseParts` | nothing; raise `ValueError` to refuse a part | with `@license_needs` |

- **One method per decorator, across the class and its bases.** The method name is yours.
- **A `ValueError` from a lobby method is the caller's `bad_settings`,** with your message. Raise a `GameError` to
  give another code: `bad_slot` for a `player_id` the game doesn't have.
- **The marks are checked before the env serves.** That covers the count, `@create_game` being present and async, and
  the number of arguments. A mistake fails at `create_app()` or the first lobby call, not mid-game.

**The match splits in two.** The base class keeps what the protocol promises: the start gate, `cancelled`, and a
final match never changing. The game reports what only it knows in its `@match_report`, read whenever the match is:

| `MatchReport` field | |
|---|---|
| `status` | the game's own: `started`, `paused`, `finished` or `failed`. `failed` counts at any time, the others once the match has started. |
| `status_detail` | its words for it |
| `progress` | its `Counter`s |
| `outcomes` | `won`, `lost` or `drawn`, by `player_id`, for the players its rules have decided |
| `scores` | `Score`s, by `player_id` |

In the game's own tools and extensions:

- **`self.player()`** is the player slot the current request plays, by its `/players/<player_id>` address. It's
  `None` at the env's own address, and a request for an id that plays no agent or human slot is refused.
- **`self.match`** is how the match stands; check `self.match.status` before taking a move.
- **`await self.player_ready(player_id)`** on a player's first move, in a game with `@begin_game`.
- **`await self.begin_match(status_detail)`** starts the match without a silent player: the game's own stall rule.

**In process,** as tests or a game's own default setup use it: `self.lobby`, `self.new_lobby(...)`,
`self.fill_slot(SlotRequest(...))`, `await self.close_lobby()`, `self.cancel_lobby()`, `self.slot_card(player_id)`,
`await self.finish_match()` and `self.cancel_match()` do what the methods do.

**Serving:** `serve()` or `create_app()`, as for any AgentEnv environment. `create_app()` adds the routes of the
lobby, the match and the license, the player slots' cards and the routing. Each protocol has several methods and the
SDK serves one handler per extension, so the three are declared on the game's card and served by these routes.
`mount()` onto an app of your own isn't supported.

## The task steps

**`add_license`** gives a licensed game its license: [Licenses](#the-add_license-step). Put it before
`close_lobby`.

**`open_lobby`** opens the lobby for a match:

| Field | |
|---|---|
| `env_id` | the game env |
| `game_settings` | the game's own settings for the match, as its card's `open` request describes them (tic-tac-toe: `first`; Warcraft III: `map`, `seed`, `time_limit_seconds`, `mode`, ...) |
| `player_slot_limits` | optional: `{"min": n, "max": n}` to narrow the game's own limits |
| `timeout_seconds` | default `120` |

- **Defaults and checks come from the env.** Settings left out get the game's defaults, and a setting the game
  doesn't take is refused (`bad_settings`) before any game exists.
- **A run can override it,** through agent-env's per-run step overrides (`user_overrides.step_params.<step id>`). An
  overridden `game_settings` merges key by key into the task's (`{"game_settings": {"seed": 7}}` changes only the
  seed), and `player_slot_limits` replaces the task's.
- **The opened lobby, with its `lobby_id`, is kept** in the run's `metadata["game_lobby"]`. The other lobby steps send
  that id.

**`add_player_slot`** fills one player slot:

| Field | |
|---|---|
| `env_id` | the game env |
| `player_id` | the game's name for the player slot (tic-tac-toe: `"x"` or `"o"`; Warcraft III: its player number, `"0"` to `"11"`) |
| `player_kind` | `agent`, `human` or `ai` |
| `player_name` | who plays it: for an agent, the `deploy_agent` agent; never for `ai` |
| `game_settings` | optional: the game's settings for the player slot, as its card's `fill` request describes them |
| `register` | agents only, default `true`: register the player slot's MCP address with the agent. `false` only reserves the slot, for a player that connects on its own |
| `timeout_seconds` | default `60` |

- **The agent is checked before the player slot is taken,** so a missing agent leaves no slot behind.
- **The step reads the player slot's env card,** and registers each of its MCP interfaces with the agent.
- **The agent must not already have the env's own address,** or it would play there instead. Deploy players with
  `"env_ids": []`.
- **A person's page is logged** as `PLAY player slot <player_id> (<name>): <url>`.
- **Every player slot is kept in the run's `metadata["game_slots"]`,** by `player_id`: its `interfaces` with full URLs,
  and for an agent the addresses `registered` with it.

**`close_lobby`** closes the lobby, which creates the game and its match. It takes `env_id` and `timeout_seconds`
(default `900`). Put it after every `add_player_slot` of the game and before its players play. The closed lobby is
kept in `metadata["game_lobby"]`.

**`finish_match`** plays the match out once its agents have stopped: put it after every `prompt_agent` of the match
and before its grading. A game that can't be played out ends its match `cancelled`, and a match already over is left
as it is. It takes `env_id` and `timeout_seconds` (default `7200`, for a realtime game's remaining time). The final
match is kept in `metadata["game_match"]`.

**`cancel_match`** ends the match where it stands, likewise (`timeout_seconds` default `60`).

## Licenses: `urn:game:license/v1`

Some games need something from their user before they run: Warcraft III its activation files, others a serial number,
a server token, or terms someone has to accept. None of it may be in the env's image, in a task file or in anything the
env replies. A game says what it still lacks, and the `add_license` step gives it, from agent-env's secret store.

### What a license is made of

A license is one or more parts, each one of three kinds:

| Kind | What it is | Examples | In the secret store | The game receives |
|---|---|---|---|---|
| `file` | bytes the game needs as a file; it decides where | Warcraft III's `roc.w3k` and `tft.w3k`; a `.lic` file | the file's base64 | `bytes` |
| `key` | a secret string | a serial number, a license key, a server token, a password | the text itself | `str` |
| `acceptance` | terms someone has to agree to; not a secret | a game's terms for AI research use | nothing: the task states it | its name |

Each part is a `LicenseItem`:

| Field | |
|---|---|
| `name`, `kind` | the part, and which of the three it is |
| `group` | the license it belongs to, when one license has several parts: two files, or a user and a password |
| `description` | where to get it, shown when it is missing |
| `max_bytes` | a file: no larger than this |
| `pattern` | a key: the format it must match (the key is never echoed) |
| `terms_url` | an acceptance: the terms agreed to |

### The game side

```python
from agentenv_game import LicenseItem, LicenseParts, install_license, license_needs

@license_needs
def needs(self) -> list[LicenseItem]:   # what the game still lacks; [] once it's licensed
    return [LicenseItem(name=n, kind="file", group="warcraft3", max_bytes=4096,
                        description=f"{n} from your Warcraft III folder")
            for n in ("roc.w3k", "tft.w3k") if not (self.game_dir / n).exists()]

@install_license
def install(self, parts: LicenseParts) -> None:   # parts.files: bytes, parts.keys: str, parts.accepted: names
    for name, data in parts.files.items():
        (self.game_dir / name).write_bytes(data)
```

- **The game decides what counts as present.** A file it finds already mounted, or a key from a previous call,
  doesn't appear in `license_needs`.
- **Every part is checked against its item before `install_license` sees it:** the game lacks it, it's the right kind,
  a file fits `max_bytes` and is valid base64, and a key matches `pattern`.
- **The base class never keeps a part.** It records only the names it passed on.
- **A lobby doesn't close while anything is missing.** The game gets `not_licensed`, with each missing part and where
  to get it.

`tests/test_license.py` has a version of tic-tac-toe that needs one part of each kind.

### On the wire

| Method | Route | Request | Response |
|---|---|---|---|
| `get` | `GET /agentenv/ext/license` | | `{"missing": [<LicenseItem>...], "installed": [<names>]}` |
| `add` | `POST /agentenv/ext/license/add` | `{"files": {name: base64}, "keys": {name: text}, "accept": [names]}` | the same status |

A part the game refuses is `bad_license`, with the reason, and a game that breaks installing one answers 500 with
`license_failed`. A key is never part of a reply, not even in an error.

### The `add_license` step

```jsonc
// Warcraft III: one license of two files
{"id": "license", "type": "add_license", "env_id": "wc3",
 "files": {"roc.w3k": "WC3_ROC_W3K", "tft.w3k": "WC3_TFT_W3K"}}
// a serial number, a store login (one license, two keys), terms to accept
{"id": "license", "type": "add_license", "env_id": "some-game",
 "keys": {"serial": "SOME_GAME_SERIAL", "user": "STORE_USER", "password": "STORE_PASSWORD"},
 "accept": ["some-game-research-terms"]}
```

| Field | |
|---|---|
| `files`, `keys` | each part's name → the secret that holds it: a file's base64, a key's text |
| `accept` | the terms the task agrees to, by name |
| `timeout_seconds` | default `60` |

What the step does:
1. **Ask the env what it lacks.** If nothing, it reads no secrets at all.
2. **Check the task gives every missing part before reading any secret.** A missing part the task doesn't map fails
   the step, naming the part and where to get it.
3. **Read only those secrets, and send them.** A secret the store doesn't have fails the step, naming the secret.
4. **Keep only the names** in the run's `metadata["game_license"]`.

**Two choices worth knowing:**
- **The task names the secrets, not the env.** An env that could name them could ask for any secret, a model key say,
  and the step would send it. As it is, a task shows exactly which secrets go to which env.
- **No local files.** A task that could name a file on the machine running it could send any file to an env. Put the
  file in the secret store instead. agent-env's default secret store reads environment variables, so
  `export WC3_ROC_W3K=$(base64 < roc.w3k)` is enough.

**Not covered yet:**
- **Alternatives,** like "a license file *or* a serial". A game can ask for whichever it prefers.
- **Licensed files too big for a secret store.** Cloud stores cap a secret at about 64 KB. A part could later come from
  an artifact.
- **The game's own licensed install.** That belongs in an image you build privately.

## Development

```bash
uv venv && uv pip install -e ".[dev]"
.venv/bin/pytest          # the lobby and the match in process, served over HTTP, players over MCP, the steps with a fake agent
.venv/bin/ruff check .
```

## Not done yet

- **Warcraft III moves onto the match protocol next.** Its own version is in
  [agentenv-wc3-plugin](https://github.com/earakely-scale/agentenv-wc3-plugin)'s `agentenv_rts`.
- **Nothing calls the lobby's `cancel` yet.** Its natural caller is cleanup for a run that ends before `close_lobby`.
- **The harness can't pause a match.** Only a game pauses its own.
- **A player slot's address isn't a secret.** Any client that can reach the env can use another player's path. A
  per-slot token in its `headers` would close that.

## License

Apache-2.0
