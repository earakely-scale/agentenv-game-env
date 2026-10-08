"""Tic-tac-toe as a game env: two players, x and o, each an agent or the game's AI, put their marks on a 3x3 board in
turn. A player slot's id is its mark.

    python examples/tictactoe.py      # serves it on port 18765 (MCP_PORT); each player plays at /players/<x|o>/mcp
"""

from __future__ import annotations

from typing import Literal

from agentenv_protocol import environment_card, tool
from pydantic import BaseModel, ConfigDict

from agentenv_game import (
    AgentEnvGameEnv,
    Lobby,
    LobbyError,
    PlayerKind,
    PlayerSlot,
    PlayerSlotLimits,
    check_player_slot,
    create_game,
    player_slot_limits,
)

MARKS = ("x", "o")
LINES = [(0, 1, 2), (3, 4, 5), (6, 7, 8), (0, 3, 6), (1, 4, 7), (2, 5, 8), (0, 4, 8), (2, 4, 6)]


@environment_card(name="tictactoe")
class TicTacToe(AgentEnvGameEnv):
    class GameSettings(BaseModel):
        model_config = ConfigDict(extra="forbid", use_attribute_docstrings=True)
        first: Literal["x", "o"] = "x"
        """Who moves first."""

    board: list[str] = []
    turn, winner = "x", None
    players: dict[str, PlayerSlot] = {}

    # ---- the lobby: what this game takes ----

    @player_slot_limits
    def two_players(self, game_settings: GameSettings, requested: PlayerSlotLimits) -> PlayerSlotLimits:
        if requested.max not in (None, 2):
            raise ValueError("tic-tac-toe has two players")
        return PlayerSlotLimits(min=2, max=2, player_kinds=[PlayerKind.AGENT, PlayerKind.AI])

    @check_player_slot
    def a_mark(self, slot: PlayerSlot, lobby: Lobby) -> None:
        if slot.player_id not in MARKS:
            raise LobbyError("bad_slot", f'tic-tac-toe\'s player slots are "x" and "o", not {slot.player_id!r}')

    @create_game
    async def new_game(self, lobby: Lobby) -> None:
        self.players = {s.player_id: s for s in lobby.player_slots}
        self.board, self.turn, self.winner = [" "] * 9, lobby.game_settings["first"], None
        self._ai_moves()

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
            raise ValueError("play at your player slot's address, /players/<x|o>/mcp")
        return slot.player_id

    def _ai_moves(self) -> None:
        while self.winner is None and self.players[self.turn].player_kind is PlayerKind.AI:
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
