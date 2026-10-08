"""A broadcast's presentation, from the start_broadcast step: its title, the players' display names, a theme, sponsor
banners, and the overlay's widgets. The streamer's overlay gets it as JSON, with every banner logo inlined as a data:
URI, so a broadcast never waits on a third-party host; the env never sees it."""

from __future__ import annotations

import base64
import binascii
import re
import urllib.request
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

FRAME = (1920, 1080)
ANCHORS = ("top", "top-left", "top-right", "bottom", "bottom-left", "bottom-right", "left", "right", "center")
PAGE_SIZE = (480, 270)
LOGO = re.compile(r"\{([a-z][a-z0-9_-]{0,19})\}")
LOGO_NAME = r"^[a-z][a-z0-9_-]{0,19}$"
LOGO_BYTES = 512 * 1024
LOGO_SECONDS = 20
DATA_URI = re.compile(r"data:(image/(?:png|jpeg|webp|gif|svg\+xml));base64,([A-Za-z0-9+/]+={0,2})")


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Banner(_Model):
    """A sponsor banner: its text, each `{name}` in it one of its logos (an https:// URL, an image file or a data:
    URI), on a dark or a light plate."""

    text: str = Field(min_length=1, max_length=100)
    logos: dict[str, str] = {}
    theme: Literal["dark", "light"] = "dark"

    @model_validator(mode="after")
    def _placed(self) -> Banner:
        placed, named = set(LOGO.findall(self.text)), set(self.logos)
        if missing := sorted(placed - named):
            raise ValueError(f"banner {self.text!r} places logos it has no source for: {missing}")
        if unused := sorted(named - placed):
            raise ValueError(f"banner {self.text!r} has logos it never places ({{name}} in its text): {unused}")
        for name, source in self.logos.items():
            if not re.fullmatch(LOGO_NAME, name):
                raise ValueError(f"a logo's name is a-z, 0-9, _ and -, starting with a letter, not {name!r}")
            if not (source.startswith(("https://", "data:", "/", "~/"))):
                raise ValueError(f"logo {name!r} is an https:// URL, a data:image URI or an image file's path, not "
                                 f"{source[:80]!r}")
        return self


class Theme(_Model):
    accent: str = Field("#e8b04a", pattern=r"^#[0-9a-fA-F]{6}$")
    """The colour of the score bug's edge, a winner's name and the cards' rules."""
    font: str | None = Field(None, min_length=1, max_length=60)
    """A font family the streamer has (DejaVu Sans, Noto Sans); its default otherwise."""


class Widget(_Model):
    """One piece of the overlay. `score_bug`, `banners`, `title_card` and `end_card` are drawn from the lobby, the
    match and the presentation; a `page` widget is any page (`env:/path` for one the game serves), sent the state on
    every change. It sits at an anchor, or in `box`, `[x, y, width, height]` of the 1920x1080 frame."""

    widget: Literal["score_bug", "banners", "title_card", "end_card", "page"]
    at: Literal[ANCHORS] | None = None
    box: tuple[int, int, int, int] | None = None
    size: tuple[int, int] | None = None
    """A page widget's width and height at its anchor."""
    url: str | None = None

    @model_validator(mode="after")
    def _shape(self) -> Widget:
        if self.widget == "page":
            if not self.url or not self.url.startswith(("env:/", "https://", "http://")):
                raise ValueError("a page widget needs a url: env:/<path> for a page the game serves, or http(s)://")
            if self.at is None and self.box is None:
                raise ValueError("a page widget sits at an anchor (at) or in a box")
        elif self.url is not None or self.size is not None:
            raise ValueError(f"a {self.widget} widget takes no url or size")
        if self.box is not None:
            x, y, w, h = self.box
            if w <= 0 or h <= 0 or x < 0 or y < 0 or x + w > FRAME[0] or y + h > FRAME[1]:
                raise ValueError(f"a widget's box lies within the {FRAME[0]}x{FRAME[1]} frame, not {list(self.box)}")
        return self


class Presentation(_Model):
    """How a broadcast looks. `names` gives a player slot's display name by its player_id (default: its label, its
    player_name, or "AI"). Without `overlay`, the overlay is the score bug alone, with the banners when there are
    some."""

    title: str | None = Field(None, min_length=1, max_length=80)
    names: dict[str, str] = {}
    theme: Theme = Theme()
    banners: list[Banner] = Field([], max_length=8)
    overlay: list[Widget] | None = None

    @model_validator(mode="after")
    def _names(self) -> Presentation:
        if bad := [k for k, v in self.names.items() if not 1 <= len(v.strip()) <= 30]:
            raise ValueError(f"a display name is 1 to 30 characters: {bad}")
        return self

    def layout(self) -> list[Widget]:
        if self.overlay is not None:
            return self.overlay
        return [Widget(widget="score_bug"), *([Widget(widget="banners")] if self.banners else [])]

    def inlined(self) -> Presentation:
        """This presentation with every banner logo a data: URI: a URL fetched, a file read, each checked to be a PNG,
        JPEG, WebP, GIF or SVG image of at most LOGO_BYTES. Raises ValueError for one that is missing, too big or not
        an image."""
        return self.model_copy(update={"banners": [b.model_copy(update={"logos": {
            n: data_uri(n, s) for n, s in b.logos.items()}}) for b in self.banners]})


def data_uri(name: str, source: str) -> str:
    if source.startswith("data:"):
        m = DATA_URI.fullmatch(source)
        try:
            body = base64.b64decode(m[2], validate=True) if m else None
        except binascii.Error:
            body = None
        if body is None:
            raise ValueError(f"logo {name!r} is not a base64 data: URI of a PNG, JPEG, WebP, GIF or SVG image")
        if len(body) > LOGO_BYTES:
            raise ValueError(f"logo {name!r} is {len(body):,} bytes; the limit is {LOGO_BYTES:,}")
        return source
    try:
        if source.startswith("https://"):
            request = urllib.request.Request(source, headers={"User-Agent": "agentenv-game"})
            with urllib.request.urlopen(request, timeout=LOGO_SECONDS) as r:
                body = r.read(LOGO_BYTES + 1)
        else:
            with Path(source).expanduser().open("rb") as f:
                body = f.read(LOGO_BYTES + 1)
    except OSError as e:
        raise ValueError(f"logo {name!r} could not be read from {source}: {e}") from e
    if len(body) > LOGO_BYTES:
        raise ValueError(f"logo {name!r} at {source} is over {LOGO_BYTES:,} bytes")
    if (kind := image_type(body)) is None:
        raise ValueError(f"logo {name!r} at {source} is not a PNG, JPEG, WebP, GIF or SVG image")
    return f"data:{kind};base64,{base64.b64encode(body).decode()}"


def image_type(body: bytes) -> str | None:
    """The image's media type from its first bytes, as a browser sniffs it."""
    if body.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if body.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if body.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if body[:4] == b"RIFF" and body[8:12] == b"WEBP":
        return "image/webp"
    return "image/svg+xml" if b"<svg" in body[:4096].lower() else None
