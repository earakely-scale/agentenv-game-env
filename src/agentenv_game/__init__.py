"""Game envs for AgentEnv: `AgentEnvGameEnv` serves a lobby of player slots (`urn:game:lobby/v1`) that agents,
people and the game's own AI fill before the game is created, and the game's license (`urn:game:license/v1`). The env
side needs only agentenv-protocol; the task steps (`agentenv_game.steps`) need agent-env too."""

from .env import (
    AgentEnvGameEnv,
    check_slot,
    connect_slot,
    create_game,
    install_license,
    license_needs,
    open_lobby,
    play_link,
)
from .license import LICENSE, LicenseItem, LicenseKind, LicenseParts
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

__all__ = ["LICENSE", "LOBBY", "AgentEnvGameEnv", "Connect", "LicenseItem", "LicenseKind", "LicenseParts", "Lobby",
           "LobbyError", "LobbyState", "Occupant", "OccupantKind", "PlayerPaths", "PlayerSlot", "PlayerSlotSettings",
           "SlotRequest", "check_slot", "connect_slot", "create_game", "current_player", "install_license",
           "license_needs", "open_lobby", "play_link"]
