from typing import Any, Literal

from pydantic import BaseModel, Field


class BotInfo(BaseModel):
    homeserver: str
    access_token: str
    refresh_token: str | None = None
    access_token_expires_at_ms: int | None = None
    refresh_before_expiry_ms: int = 60000
    user_id: str | None = None
    device_id: str | None = None
    sync_filter: str | dict[str, Any] | None = None
    set_presence: Literal["online", "offline", "unavailable"] | None = None

    # Traditional Matrix login credentials
    login_user: str | None = None
    login_password: str | None = None
    login_initial_device_display_name: str | None = None

    # OAuth2
    oauth_enabled: bool = False
    oauth_server_url: str | None = None  # e.g. https://account.matrix.org
    oauth_metadata_url: str | None = None
    oauth_client_id: str | None = None
    oauth_client_uri: str | None = None
    oauth_redirect_uri: str | None = None
    oauth_scope: str | None = None
    oauth_device_id: str | None = None
    oauth_open_browser: bool = False
    oauth_callback_timeout: float = 300.0

    # Auto-accept room invites
    auto_accept_invites: bool = False
    auto_accept_whitelist: list[str] | None = None  # None = all users allowed
    auto_accept_blacklist: list[str] = Field(default_factory=list)

    # E2EE configuration
    recovery_code: str | None = None
    # MATRIX_RECOVERY_CODE: base58-encoded Curve25519 private key
    # Used to restore Megolm sessions from server-side key backup
    e2ee_store_path: str | None = None
    # E2EE state persistence directory, derived from matrix_token_store_path when None

    # Runtime / persisted field
    session_type: str | None = None  # "legacy_login" | "oauth2" | None
    oauth_token_endpoint: str | None = None  # persisted for OAuth2 refresh


class Config(BaseModel):
    matrix_bots: list[BotInfo] = Field(default_factory=list)
    matrix_api_timeout: float = 30.0
    matrix_sync_timeout: int = 30000
    matrix_retry_interval: float = 3.0
    matrix_handle_self_message: bool = False
    matrix_handle_old_events: bool = False
    matrix_proxy: str | None = None
    matrix_token_store_path: str | None = None
