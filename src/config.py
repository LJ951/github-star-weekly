"""Application configuration loaded from environment variables."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - useful before dependencies are installed.
    load_dotenv = None


DEFAULT_DATABASE_PATH = Path("data/rankings.sqlite")
DEFAULT_AI_BASE_URL = "https://api.deepseek.com"
DEFAULT_AI_MODEL = "deepseek-v4-flash"
DEFAULT_GITHUB_API_TIMEOUT_SECONDS = 20
DEFAULT_OPENAI_TIMEOUT_SECONDS = 45
DEFAULT_RESEND_TIMEOUT_SECONDS = 30
DEFAULT_README_MAX_CHARS = 6000
DEFAULT_TOP_N = 10


class ConfigError(ValueError):
    """Raised when required configuration is missing or invalid."""


@dataclass(frozen=True)
class AppConfig:
    app_env: str
    database_path: Path
    google_credentials_json: str | None
    google_credentials_path: Path | None
    ai_api_key: str
    ai_base_url: str | None
    ai_model: str
    github_token: str
    openai_api_key: str
    resend_api_key: str
    email_from: str
    email_to: tuple[str, ...]
    openai_model: str
    github_api_timeout_seconds: float
    openai_timeout_seconds: float
    resend_timeout_seconds: float
    readme_max_chars: int
    top_n: int
    dry_run: bool
    log_level: str

    @property
    def primary_recipient(self) -> str:
        return self.email_to[0]

    def masked_summary(self) -> dict[str, str | int | float | bool | list[str]]:
        """Return non-sensitive configuration details for logs."""

        return {
            "app_env": self.app_env,
            "database_path": str(self.database_path),
            "google_credentials": (
                "json"
                if self.google_credentials_json
                else "path"
                if self.google_credentials_path
                else "missing"
            ),
            "email_from": self.email_from,
            "email_to": [_mask_email(address) for address in self.email_to],
            "ai_base_url": self.ai_base_url or "default",
            "ai_model": self.ai_model,
            "github_api_timeout_seconds": self.github_api_timeout_seconds,
            "openai_timeout_seconds": self.openai_timeout_seconds,
            "resend_timeout_seconds": self.resend_timeout_seconds,
            "readme_max_chars": self.readme_max_chars,
            "top_n": self.top_n,
            "dry_run": self.dry_run,
            "log_level": self.log_level,
        }

    def prepare_google_credentials_file(self) -> Path | None:
        """Make Google credentials available as a file for BigQuery clients.

        GitHub Actions stores the service account JSON as a secret string. The
        Google client library expects a file path, so this writes the JSON to a
        private temp file and sets GOOGLE_APPLICATION_CREDENTIALS for the
        current process. If a path is already configured, it is returned.
        """

        if self.google_credentials_path:
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(
                self.google_credentials_path
            )
            return self.google_credentials_path

        if not self.google_credentials_json:
            return None

        _validate_json_object(
            self.google_credentials_json,
            "GOOGLE_APPLICATION_CREDENTIALS_JSON",
        )
        handle = tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix="github-star-weekly-google-",
            suffix=".json",
            delete=False,
        )
        with handle:
            handle.write(self.google_credentials_json)
            handle.write("\n")

        credentials_path = Path(handle.name)
        try:
            credentials_path.chmod(0o600)
        except OSError:
            pass
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(credentials_path)
        return credentials_path


def load_config(
    env: Mapping[str, str] | None = None,
    *,
    load_env_file: bool = True,
) -> AppConfig:
    """Load, validate, and return application configuration."""

    if load_env_file and load_dotenv is not None:
        load_dotenv()

    source = env if env is not None else os.environ
    missing: list[str] = []

    google_credentials_json = _optional_str(
        source, "GOOGLE_APPLICATION_CREDENTIALS_JSON"
    )
    google_credentials_path_value = _optional_str(source, "GOOGLE_APPLICATION_CREDENTIALS")
    google_credentials_path = (
        Path(google_credentials_path_value).expanduser()
        if google_credentials_path_value
        else None
    )
    if not google_credentials_json and not google_credentials_path:
        missing.append(
            "GOOGLE_APPLICATION_CREDENTIALS_JSON or GOOGLE_APPLICATION_CREDENTIALS"
        )

    github_token = _required_str(source, "GITHUB_TOKEN", missing)
    ai_api_key = (
        _optional_str(source, "AI_API_KEY")
        or _optional_str(source, "DEEPSEEK_API_KEY")
        or _optional_str(source, "OPENAI_API_KEY")
    )
    if ai_api_key is None:
        missing.append("AI_API_KEY or DEEPSEEK_API_KEY or OPENAI_API_KEY")
    resend_api_key = _required_str(source, "RESEND_API_KEY", missing)
    email_from = _required_str(source, "EMAIL_FROM", missing)
    email_to_raw = _required_str(source, "EMAIL_TO", missing)

    if missing:
        raise ConfigError("Missing required environment variables: " + ", ".join(missing))

    if google_credentials_json:
        _validate_json_object(
            google_credentials_json,
            "GOOGLE_APPLICATION_CREDENTIALS_JSON",
        )
    if google_credentials_path and not google_credentials_path.exists():
        raise ConfigError(
            "GOOGLE_APPLICATION_CREDENTIALS points to a file that does not exist"
        )

    email_to = _parse_email_list(email_to_raw, "EMAIL_TO")

    ai_base_url = _optional_str(source, "AI_BASE_URL") or DEFAULT_AI_BASE_URL
    ai_model = (
        _optional_str(source, "AI_MODEL")
        or _optional_str(source, "DEEPSEEK_MODEL")
        or _optional_str(source, "OPENAI_MODEL")
        or DEFAULT_AI_MODEL
    )

    return AppConfig(
        app_env=_optional_str(source, "APP_ENV") or "local",
        database_path=Path(
            _optional_str(source, "DATABASE_PATH") or str(DEFAULT_DATABASE_PATH)
        ),
        google_credentials_json=google_credentials_json,
        google_credentials_path=google_credentials_path,
        ai_api_key=ai_api_key or "",
        ai_base_url=ai_base_url,
        ai_model=ai_model,
        github_token=github_token,
        openai_api_key=ai_api_key or "",
        resend_api_key=resend_api_key,
        email_from=email_from,
        email_to=email_to,
        openai_model=ai_model,
        github_api_timeout_seconds=_positive_float(
            source,
            "GITHUB_API_TIMEOUT_SECONDS",
            DEFAULT_GITHUB_API_TIMEOUT_SECONDS,
        ),
        openai_timeout_seconds=_positive_float(
            source,
            "OPENAI_TIMEOUT_SECONDS",
            DEFAULT_OPENAI_TIMEOUT_SECONDS,
        ),
        resend_timeout_seconds=_positive_float(
            source,
            "RESEND_TIMEOUT_SECONDS",
            DEFAULT_RESEND_TIMEOUT_SECONDS,
        ),
        readme_max_chars=_positive_int(
            source,
            "README_MAX_CHARS",
            DEFAULT_README_MAX_CHARS,
        ),
        top_n=_positive_int(source, "TOP_N", DEFAULT_TOP_N),
        dry_run=_bool(source, "DRY_RUN", default=False),
        log_level=(_optional_str(source, "LOG_LEVEL") or "INFO").upper(),
    )


def _optional_str(source: Mapping[str, str], name: str) -> str | None:
    value = source.get(name)
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _required_str(
    source: Mapping[str, str],
    name: str,
    missing: list[str],
) -> str:
    value = _optional_str(source, name)
    if value is None:
        missing.append(name)
        return ""
    return value


def _positive_int(source: Mapping[str, str], name: str, default: int) -> int:
    raw = _optional_str(source, name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer") from exc
    if value <= 0:
        raise ConfigError(f"{name} must be greater than 0")
    return value


def _positive_float(source: Mapping[str, str], name: str, default: float) -> float:
    raw = _optional_str(source, name)
    if raw is None:
        return float(default)
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number") from exc
    if value <= 0:
        raise ConfigError(f"{name} must be greater than 0")
    return value


def _bool(source: Mapping[str, str], name: str, *, default: bool) -> bool:
    raw = _optional_str(source, name)
    if raw is None:
        return default
    normalized = raw.casefold()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    raise ConfigError(f"{name} must be a boolean value")


def _parse_email_list(raw: str, name: str) -> tuple[str, ...]:
    addresses = tuple(address.strip() for address in raw.split(",") if address.strip())
    if not addresses:
        raise ConfigError(f"{name} must contain at least one email address")
    invalid = [address for address in addresses if "@" not in address]
    if invalid:
        raise ConfigError(f"{name} contains invalid email address values")
    return addresses


def _validate_json_object(raw: str, name: str) -> None:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{name} must be valid JSON") from exc
    if not isinstance(value, dict):
        raise ConfigError(f"{name} must be a JSON object")


def _mask_email(address: str) -> str:
    local, separator, domain = address.partition("@")
    if not separator:
        return "***"
    if len(local) <= 2:
        masked_local = local[:1] + "***"
    else:
        masked_local = local[:2] + "***"
    return f"{masked_local}@{domain}"


if __name__ == "__main__":
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"Configuration error: {exc}")
        raise SystemExit(2) from exc

    print("Configuration loaded successfully.")
    print(json.dumps(config.masked_summary(), ensure_ascii=False, indent=2))
