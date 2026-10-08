"""Broadcasting a game env's match: the start_broadcast and save_broadcast task steps, the presentation they take,
and the streamer image (streamer/) that frames the game's spectator view with the overlay and streams or records it.
This runs beside agent-env, never in the env."""

from .settings import Banner, Presentation, Theme, Widget
from .steps import SaveBroadcastTaskStep, StartBroadcastTaskStep

__all__ = ["Banner", "Presentation", "SaveBroadcastTaskStep", "StartBroadcastTaskStep", "Theme", "Widget"]
