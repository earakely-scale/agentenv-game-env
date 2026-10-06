"""Game envs for AgentEnv: `AgentEnvGameEnv` serves a lobby of player slots (`urn:game:lobby/v1`) that agents,
people and the game's own AI fill before the game is created. The env side needs only agentenv-protocol; the task
steps (`agentenv_game.steps`) need agent-env too."""

from .env import AgentEnvGameEnv, check_slot, connect_slot, create_game, open_lobby, play_link
from .lobby import (
    LOBBY,
    Connect,
    Lobby,
    LobbyError,
    LobbyState,
    Occupant,
    OccupantKind,
    PlayerSlot,
    PlayerSlotSettings,
    SlotRequest,
)
from .routing import PlayerPaths, current_player

__all__ = ["LOBBY", "AgentEnvGameEnv", "Connect", "Lobby", "LobbyError", "LobbyState", "Occupant", "OccupantKind",
           "PlayerPaths", "PlayerSlot", "PlayerSlotSettings", "SlotRequest", "check_slot", "connect_slot",
           "create_game", "current_player", "open_lobby", "play_link"]
