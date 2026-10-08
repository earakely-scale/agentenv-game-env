"""Game envs for AgentEnv: `AgentEnvGameEnv` serves a lobby of player slots (`urn:game:lobby/v1`) that agents,
people and the game's own AI fill before the game is created, an env card for each agent and human player slot, the
match the lobby's close creates (`urn:game:match/v1`), and the game's license (`urn:game:license/v1`). The env side
needs only agentenv-protocol; the task steps (`agentenv_game.steps`) need agent-env too."""

from .env import (
    AgentEnvGameEnv,
    begin_game,
    check_player_slot,
    create_game,
    install_license,
    license_needs,
    match_report,
    play_out,
    player_slot_card,
    player_slot_limits,
    player_teams,
)
from .license import LICENSE, LicenseItem, LicenseKind, LicenseParts
from .lobby import (
    LOBBY,
    GameError,
    Lobby,
    LobbyStatus,
    PlayerKind,
    PlayerSlot,
    PlayerSlotLimits,
    PlayerTeam,
    SlotRequest,
)
from .match import MATCH, Counter, Match, MatchReport, MatchStatus, PlayerState, PlayerStatus, Score
from .routing import PlayerPaths, current_player

__all__ = ["LICENSE", "LOBBY", "MATCH", "AgentEnvGameEnv", "Counter", "GameError", "LicenseItem", "LicenseKind",
           "LicenseParts", "Lobby", "LobbyStatus", "Match", "MatchReport", "MatchStatus", "PlayerKind", "PlayerPaths",
           "PlayerSlot", "PlayerSlotLimits", "PlayerState", "PlayerStatus", "PlayerTeam", "Score", "SlotRequest",
           "begin_game", "check_player_slot", "create_game", "current_player", "install_license", "license_needs",
           "match_report", "play_out", "player_slot_card", "player_slot_limits", "player_teams"]
