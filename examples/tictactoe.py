"""Tic-tac-toe as a game env: two players, each an agent or the game's AI, put their marks on a 3x3 board in turn.

    python examples/tictactoe.py      # serves it on port 18765 (MCP_PORT); each player plays at /players/<name>/mcp
"""

from __future__ import annotations

from agentenv_protocol import environment_card, tool

from agentenv_game import (
    AgentEnvGameEnv,
    Lobby,
    OccupantKind,
    PlayerSlot,
    PlayerSlotSettings,
    check_slot,
    create_game,
    open_lobby,
)

MARKS = ("x", "o")
LINES = [(0, 1, 2), (3, 4, 5), (6, 7, 8), (0, 3, 6), (1, 4, 7), (2, 5, 8), (0, 4, 8), (2, 4, 6)]


@environment_card(name="tictactoe")
class TicTacToe(AgentEnvGameEnv):
    board: list[str] = []
    turn, winner = "x", None
    players: dict[str, PlayerSlot] = {}

    # ---- the lobby: what this game takes ----

    @open_lobby
    def settings(self, additional_settings: dict, player_slot_settings: PlayerSlotSettings | None):
        if set(additional_settings) - {"first"} or additional_settings.get("first", "x") not in MARKS:
            raise ValueError('the settings are {"first": "x" or "o"}, who moves first')
        if player_slot_settings is not None and player_slot_settings.max not in (None, 2):
            raise ValueError("tic-tac-toe has two players")
        return {"first": additional_settings.get("first", "x")}, PlayerSlotSettings(
            min=2, max=2, additional_settings={"occupants": ["agent", "ai"], "factions": list(MARKS)})

    @check_slot
    def one_mark_each(self, slot: PlayerSlot, lobby: Lobby) -> None:
        mark = slot.additional_settings.get("faction")
        if set(slot.additional_settings) != {"faction"} or mark not in MARKS:
            raise ValueError('a slot\'s settings are {"faction": "x" or "o"}')
        if any(s.additional_settings["faction"] == mark for s in lobby.slots):
            raise ValueError(f"{mark} is taken")

    @create_game
    async def new_game(self, lobby: Lobby) -> dict:
        self.players = {s.additional_settings["faction"]: s for s in lobby.slots}
        self.board, self.turn, self.winner = [" "] * 9, lobby.additional_settings["first"], None
        self._ai_moves()
        return {"first": lobby.additional_settings["first"]}

    # ---- the game: what each player can do ----

    @tool()
    async def show_board(self):
        """The board, your mark, and whose turn it is."""
        return f"You are {self._mine()}.\n{self._show()}"

    @tool()
    async def mark(self, cell: int):
        """Put your mark in a free cell on your turn: 0 to 8, top left to bottom right."""
        mine = self._mine()
        if self.winner is None and mine == self.turn and 0 <= cell <= 8 and self.board[cell] == " ":
            self._put(cell)
            self._ai_moves()
            return self._show()
        why = ("the game is over" if self.winner else f"it is {self.turn}'s turn" if mine != self.turn
               else f"cell {cell} is not free")
        return f"Not marked: {why}.\n{self._show()}"

    def _mine(self) -> str:
        if not self.board:
            raise ValueError("the game has not started: its lobby is still open")
        slot = self.player()
        if slot is None:
            raise ValueError("play at your slot's address, /players/<name>/mcp")
        return slot.additional_settings["faction"]

    def _ai_moves(self) -> None:
        while self.winner is None and self.players[self.turn].occupant.kind is OccupantKind.AI:
            self._put(self.board.index(" "))

    def _put(self, cell: int) -> None:
        self.board[cell] = self.turn
        if any(self.board[a] == self.board[b] == self.board[c] == self.turn for a, b, c in LINES):
            self.winner = self.turn
        elif " " not in self.board:
            self.winner = "draw"
        self.turn = "o" if self.turn == "x" else "x"

    def _show(self) -> str:
        rows = "\n".join(" | ".join(self.board[r * 3:r * 3 + 3]) for r in range(3))
        status = (f"{self.winner} won" if self.winner in MARKS else "A draw" if self.winner
                  else f"{self.turn} to play")
        return f"{rows}\n{status}."


if __name__ == "__main__":
    TicTacToe().serve()
