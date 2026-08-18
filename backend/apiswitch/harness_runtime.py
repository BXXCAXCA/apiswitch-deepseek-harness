"""DeepSeek Harness-specific runtime credentials.

The plugin owns one local gateway token so users do not need a client-token
management screen.  Only the token hash is stored in SQLite; the plaintext is
kept in the plugin data directory for the local Harness connector.
"""

from __future__ import annotations

import hmac
import os
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from apiswitch.config import settings
from apiswitch.db.models import ApiToken
from apiswitch.security.tokens import generate_api_token, hash_api_token, token_prefix

HARNESS_TOKEN_NAME = "DeepSeek Harness"
HARNESS_SCOPES = ["gateway:invoke"]


def harness_token_path() -> Path:
    return Path(settings.harness_token_file).expanduser().resolve()


def _read_token(path: Path) -> str | None:
    if not path.is_file():
        return None
    value = path.read_text(encoding="utf-8").strip()
    return value if value.startswith("ask_") and len(value) > 20 else None


def _write_token(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(value + "\n", encoding="utf-8")
    try:
        os.chmod(temporary, 0o600)
    except OSError:
        pass
    temporary.replace(path)


def ensure_harness_token(db: Session) -> tuple[ApiToken, str]:
    """Create or repair the single plugin-managed Harness token."""
    path = harness_token_path()
    plain = _read_token(path)
    if plain is None:
        plain = generate_api_token()
        _write_token(path, plain)

    digest = hash_api_token(plain)
    row = db.scalar(select(ApiToken).where(ApiToken.name == HARNESS_TOKEN_NAME).limit(1))
    if row is None:
        row = ApiToken(
            name=HARNESS_TOKEN_NAME,
            token_prefix=token_prefix(plain),
            token_hash=digest,
            scopes_json=HARNESS_SCOPES,
            enabled=True,
        )
        db.add(row)
    else:
        row.token_prefix = token_prefix(plain)
        row.token_hash = digest
        row.scopes_json = HARNESS_SCOPES
        row.enabled = True
        row.expires_at = None
    db.commit()
    db.refresh(row)
    return row, plain


def is_harness_token(token: ApiToken) -> bool:
    """Return true only for the token matching the plugin-owned plaintext file."""
    plain = _read_token(harness_token_path())
    return bool(
        plain
        and token.name == HARNESS_TOKEN_NAME
        and hmac.compare_digest(token.token_hash, hash_api_token(plain))
    )
