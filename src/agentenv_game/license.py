"""Licenses, `urn:game:license/v1`: what a game needs from its user at run time and must never find in its image, a
task file or a reply. A license is one or more parts, each a file (bytes), a key (a secret string) or an acceptance
(terms the task agrees to). The game says what it still lacks (`@license_needs`) and puts the parts it is given where
it wants them (`@install_license`); `AgentEnvGameEnv` (env.py) serves the extension, and the add_license step
(steps.py) resolves the parts from agent-env's secret store."""

from __future__ import annotations

import base64
import binascii
import re
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from .lobby import NAME, LobbyError

LICENSE = "urn:game:license/v1"


class LicenseKind(StrEnum):
    FILE = "file"
    """Bytes the game needs as a file; it decides where. Stored as a secret holding the file's base64."""
    KEY = "key"
    """A secret string: a serial number, a license key, a token, a password. Stored as the text itself."""
    ACCEPTANCE = "acceptance"
    """Terms someone has to agree to. Not a secret: the task states it."""


class LicenseItem(BaseModel):
    """One part a game needs."""

    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=NAME)
    kind: LicenseKind
    group: str | None = None
    """The license it is part of, when one license has several parts: WC3's two files, a user and a password."""
    description: str | None = None
    """Where to get it, shown when it is missing."""
    max_bytes: int | None = Field(None, ge=1)
    pattern: str | None = None
    """For a key: the format it must match (the key itself is never echoed)."""
    terms_url: str | None = None
    """For an acceptance: the terms being agreed to."""

    def label(self) -> str:
        where = f" ({self.description})" if self.description else ""
        terms = f" ({self.terms_url})" if self.terms_url else ""
        return f"{self.kind.value} {self.name}{where or terms}"


class LicenseParts(BaseModel):
    """What `@install_license` receives, typed by kind."""

    files: dict[str, bytes] = {}
    keys: dict[str, str] = {}
    accepted: list[str] = []


def parts_for(missing: list[LicenseItem], files: dict, keys: dict, accept: list) -> LicenseParts:
    """The parts an `add` call gives, each checked against the item it fills: the game asked for it, as that kind,
    and it fits the item's limits. A part for an item the game doesn't lack now is refused too, so nothing is sent
    by mistake. Raises LobbyError("bad_license")."""
    wanted = {i.name: i for i in missing}
    given = [*(("file", n) for n in files), *(("key", n) for n in keys), *(("acceptance", n) for n in accept)]
    for kind, name in given:
        item = wanted.get(name)
        if item is None:
            raise LobbyError("bad_license", f"the game doesn't need {kind} {name} now; it lacks "
                                            f"{', '.join(i.label() for i in missing) or 'nothing'}")
        if item.kind.value != kind:
            raise LobbyError("bad_license", f"{name} is a {item.kind.value}, not a {kind}")
    decoded = {}
    for name, value in files.items():
        try:
            data = base64.b64decode(value, validate=True) if isinstance(value, str) else b""
        except binascii.Error as e:
            raise LobbyError("bad_license", f"file {name} is not base64 ({e})") from e
        if not data:
            raise LobbyError("bad_license", f"file {name} is empty")
        if wanted[name].max_bytes is not None and len(data) > wanted[name].max_bytes:
            raise LobbyError("bad_license", f"file {name} is {len(data)} bytes, more than the "
                                            f"{wanted[name].max_bytes} it can be")
        decoded[name] = data
    for name, value in keys.items():
        if not isinstance(value, str) or not value:
            raise LobbyError("bad_license", f"key {name} is empty")
        if wanted[name].pattern is not None and not re.fullmatch(wanted[name].pattern, value):
            raise LobbyError("bad_license", f"key {name} is not in its format ({wanted[name].pattern})")
    return LicenseParts(files=decoded, keys=dict(keys), accepted=list(accept))
