# agentenv-game-env

Game envs for [AgentEnv](https://github.com/scaleapi/agentenv-framework). A game has players, and this package gives
every game env one way to say who they are:

- **A lobby**, `urn:game:lobby/v1`, that the env serves. It holds the next game's player slots, which are filled one
  player at a time before the game is created. A player is an agent, a person, or the game's own AI.
- **An env card for each player slot** that an agent or a person plays: the env as that player sees it, with the
  interfaces it plays through.
- **`AgentEnvGameEnv`**, the base class that serves both and tells each request which player slot it plays. A game
  declares its settings as pydantic models and marks its own parts of the lobby with decorators.
- **A license**, `urn:game:license/v1`, for a game that needs something from its user to run (license files, keys,
  terms to accept), which must never be in its image, a task file or a reply.
- **Four task steps**: `add_license` gives a game its license from agent-env's secret store, and `create_match`,
  `add_player_slot` and `start_match` open, fill and close the lobby of any game env built on it.

The same task steps then put players in any game: one agent against the game's AI, two models against each other, a
person beside an agent. Warcraft III's env, in
[agentenv-wc3-plugin](https://github.com/earakely-scale/agentenv-wc3-plugin), is built on it. Its lobby takes a
match's settings (map, seed, time limit, clock), and its player slots take a race, a team, a label and, for the game's
AI, a level.

```
deploy_env ── create_match (opens the lobby with the game's settings)
                 ├─ add_player_slot {player_id: "2", ai, faction: "orc", team: 2}
                 ├─ deploy_agent p1 ── add_player_slot {player_id: "0", agent p1, faction: "human", team: 1} ──┐
                 └─ deploy_agent p2 ── add_player_slot {player_id: "1", agent p2, faction: "undead", team: 1} ─┤
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

[`examples/tictactoe.py`](examples/tictactoe.py) is a complete game env in about 100 lines. It has two players, `x` and
`o`, each an agent or the game's AI. A player slot's id is its mark. The lobby part:

```python
from agentenv_game import AgentEnvGameEnv, LobbyError, PlayerKind, PlayerSlotLimits
from agentenv_game import check_player_slot, create_game, player_slot_limits
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
            raise LobbyError("bad_slot", ...)

    @create_game                      # the lobby closed: set up the board, and let the AI move if it goes first
    async def new_game(self, lobby):
        self.players = {s.player_id: s for s in lobby.player_slots}
        self.board, self.turn = [" "] * 9, lobby.game_settings["first"]
        ...

    @tool()
    async def mark(self, cell: int):
        """Put your mark in a free cell on your turn: 0 to 8, top left to bottom right."""
        mine = self.player().player_id     # the player slot this request plays
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
{"lobby_id":"lb-444966b0","status":"open","game_settings":{"first":"x"},
 "player_slot_limits":{"min":2,"max":2,"player_kinds":["agent","ai"]},"player_slots":[]}
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
{"lobby_id":"lb-444966b0","status":"closed", ..., "player_slots":[...]}
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

### In an AgentEnv task

Register the example as an MCP server env, as with any env (its image runs `python tictactoe.py`). A task then puts an
agent against the game's AI:

```json
[
  {"id": "deploy", "type": "deploy_env", "env_id": "tictactoe"},
  {"id": "agent", "type": "deploy_agent", "agent_name": "alice", "a2a_agent_id": "your-agent", "env_ids": []},
  {"id": "match", "type": "create_match", "env_id": "tictactoe", "game_settings": {"first": "o"},
   "depends_on": ["deploy"]},
  {"id": "slot-x", "type": "add_player_slot", "env_id": "tictactoe", "depends_on": ["match", "agent"],
   "player_id": "x", "player_kind": "agent", "player_name": "alice"},
  {"id": "slot-o", "type": "add_player_slot", "env_id": "tictactoe", "depends_on": ["match"],
   "player_id": "o", "player_kind": "ai"},
  {"id": "start", "type": "start_match", "env_id": "tictactoe", "depends_on": ["slot-x", "slot-o"]},
  {"id": "play", "type": "prompt_agent", "agent_name": "alice", "depends_on": ["start"],
   "prompt": "You play tic-tac-toe through the tools. Win."}
]
```

- **The agent deploys with `"env_ids": []`.** `add_player_slot` gives it its player slot's MCP address, so it plays
  `x`, not the env's own address.
- **A second agent in place of the AI** makes it model against model: deploy `bob` and give him player slot `"o"`.

## The lobby protocol: `urn:game:lobby/v1`

### Lifecycle

```
              open                   close                        the match (the game's business)
 not_opened ───────► open ──────────────────────────► closed ───► not_started → started → ...
                      │  fill ×N      ├─ cancel ─────► cancelled   (no game)
                      │               └─ the game can't be created ─► failed   (no game; close answers 500)
                      └─ open again: a new lobby, dropping this one and its game
```

- **`open`** starts a new, empty lobby with these settings and a new `lobby_id`, dropping the last lobby and its game.
- **`fill`** adds one player while the lobby is open.
- **`close`** creates the game from the filled player slots. After that the lobby is read-only, and the game is the
  env's business (turns, start gates, results).
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
current lobby's (`lobby_replaced`). The task steps always send the one `create_match` opened.

### Types

| Type | Fields |
|---|---|
| `Lobby` | `lobby_id`: new on each open. `status`: `LobbyStatus`, `not_opened`, `open`, `closed`, `cancelled` or `failed`. `game_settings`: the game's own (a map, a seed, a time limit), checked against its `GameSettings`, every default filled in. `player_slot_limits`: a `PlayerSlotLimits`. `player_slots`: the filled `PlayerSlot`s, in the order they were filled, which means nothing. |
| `PlayerSlotLimits` | `min`: at close, at least this many player slots filled. `max`: no more than this many. `player_kinds`: the kinds of player the game takes, `["agent"]` by default. |
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
| `@license_needs` | for the license's status, and before the lobby closes | nothing | the `LicenseItem`s the game still lacks; empty when licensed | optional, with `@install_license` |
| `@install_license` | when parts arrive | `parts`: a `LicenseParts` | nothing; raise `ValueError` to refuse a part | with `@license_needs` |

- **One method per decorator, across the class and its bases.** The method name is yours.
- **A `ValueError` from your method is the caller's `bad_settings`,** with your message. Raise a `LobbyError` to give
  another code: `bad_slot` for a `player_id` the game doesn't have.
- **The marks are checked before the env serves.** That covers the count, `@create_game` being present and async, and
  the number of arguments. A mistake fails at `create_app()` or the first lobby call, not mid-game.

In the game's own tools and extensions, **`self.player()`** is the player slot the current request plays, by its
`/players/<player_id>` address. It's `None` at the env's own address, and a request for an id that plays no agent or
human slot is refused.

**In process,** as tests or a game's own default setup use it: `self.lobby`, `self.new_lobby(...)`,
`self.fill_slot(SlotRequest(...))`, `await self.close_lobby()`, `self.cancel_lobby()` and `self.slot_card(player_id)`
do what the methods do.

**Serving:** `serve()` or `create_app()`, as for any AgentEnv environment. `create_app()` adds the lobby's routes, the
player slots' cards and the routing (the SDK serves one handler per extension, so the lobby's other methods are added
there). `mount()` onto an app of your own isn't supported.

## The task steps

**`add_license`** gives a licensed game its license: [Licenses](#the-add_license-step). Put it before
`start_match`.

**`create_match`** opens the lobby for a match:

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

**`start_match`** closes the lobby, which creates the game. It takes `env_id` and `timeout_seconds` (default `900`).
Put it after every `add_player_slot` of the game and before its players play. The closed lobby is kept in
`metadata["game_lobby"]`.

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

A part the game refuses is `bad_license`, with the reason. A key is never part of a reply, not even in an error.

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
.venv/bin/pytest          # the lobby in process, served over HTTP, players over MCP, and the steps with a fake agent
.venv/bin/ruff check .
```

## Not done yet

- **The match,** `urn:game:match/v1`: what happens after the lobby closes, such as the start gate, holds, finishing,
  and each player's result and scores. It's being designed; Warcraft III has its own version in
  [agentenv-wc3-plugin](https://github.com/earakely-scale/agentenv-wc3-plugin)'s `agentenv_rts`.
- **Nothing calls `cancel` yet.** Its natural caller is cleanup for a run that ends before `start_match`.
- **A player slot's address isn't a secret.** Any client that can reach the env can use another player's path. A
  per-slot token in its `headers` would close that.

## License

Apache-2.0
