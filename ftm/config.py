"""Configuration loaded from environment variables and plain-text list files.

Secrets (the access token) are only ever read from the environment and are never
written to the database, logs or reports.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader: KEY=VALUE lines, no overriding of existing env vars."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def read_list_file(path: Path) -> list[str]:
    """Read one item per line, ignoring blank lines and comment lines.

    A comment line starts with "# " (hash then space) or is a lone "#", so that
    hashtag entries such as "#climate" are still read as items.
    """
    if not path.is_file():
        return []
    items = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line == "#" or line.startswith("# "):
            continue
        items.append(line)
    return items


@dataclass
class Settings:
    access_token: str | None = None
    ig_user_id: str | None = None
    graph_api_version: str = "v23.0"
    db_path: Path = Path("data/ftm.sqlite3")
    hashtags_file: Path = Path("config/hashtags.txt")
    keywords_file: Path = Path("config/ad_keywords.txt")
    ad_countries: list[str] = field(default_factory=lambda: ["US"])

    @property
    def hashtags(self) -> list[str]:
        return [h.lstrip("#").lower() for h in read_list_file(self.hashtags_file)]

    @property
    def ad_keywords(self) -> list[str]:
        return read_list_file(self.keywords_file)


def load_settings(env_file: Path | None = Path(".env")) -> Settings:
    if env_file is not None:
        _load_dotenv(env_file)
    countries = os.environ.get("FTM_AD_COUNTRIES", "US")
    return Settings(
        access_token=os.environ.get("META_ACCESS_TOKEN") or None,
        ig_user_id=os.environ.get("IG_USER_ID") or None,
        graph_api_version=os.environ.get("GRAPH_API_VERSION", "v23.0"),
        db_path=Path(os.environ.get("FTM_DB_PATH", "data/ftm.sqlite3")),
        hashtags_file=Path(os.environ.get("FTM_HASHTAGS_FILE", "config/hashtags.txt")),
        keywords_file=Path(os.environ.get("FTM_AD_KEYWORDS_FILE", "config/ad_keywords.txt")),
        ad_countries=[c.strip().upper() for c in countries.split(",") if c.strip()],
    )
