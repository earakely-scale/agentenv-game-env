"""Game envs for AgentEnv: `AgentEnvGameEnv` serves a lobby of player slots (`urn:game:lobby/v1`) that agents,
people and the game's own AI fill before the game is created, an env card for each agent and human player slot, and
the game's license (`urn:game:license/v1`). The env side needs only agentenv-protocol; the task steps
(`agentenv_game.steps`) need agent-env too."""

from .env import (
    AgentEnvGameEnv,
    check_player_slot,
    create_game,
    install_license,
    license_needs,
    player_slot_card,
    player_slot_limits,
)
from .license import LICENSE, LicenseItem, LicenseKind, LicenseParts
from .lobby import LOBBY, Lobby, LobbyError, LobbyStatus, PlayerKind, PlayerSlot, PlayerSlotLimits, SlotRequest
from .routing import PlayerPaths, current_player

__all__ = ["LICENSE", "LOBBY", "AgentEnvGameEnv", "LicenseItem", "LicenseKind", "LicenseParts", "Lobby", "LobbyError",
           "LobbyStatus", "PlayerKind", "PlayerPaths", "PlayerSlot", "PlayerSlotLimits", "SlotRequest",
           "check_player_slot", "create_game", "current_player", "install_license", "license_needs",
           "player_slot_card", "player_slot_limits"]
