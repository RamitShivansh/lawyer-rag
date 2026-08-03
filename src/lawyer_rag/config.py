from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="LEGAL_RAG_",
        case_sensitive=False,
        extra="ignore",
    )

    environment: str = "development"
    database_url: str = "sqlite:///./data/lawyer_rag.db"
    qdrant_url: str = "http://qdrant:6333"
    qdrant_collection: str = "legal_chunks_v1"
    qdrant_alias: str = "legal_chunks_current"

    data_dir: Path = Path("data")
    model_cache_dir: Path = Path("models")
    dense_model: str = "BAAI/bge-small-en-v1.5"
    sparse_model: str = "Qdrant/bm25"
    reranker_model: str = "Xenova/ms-marco-MiniLM-L-6-v2"
    model_local_files_only: bool = False

    admin_token: str = "development-admin-token"  # noqa: S105
    mcp_token: str = "development-mcp-token"  # noqa: S105
    mcp_auth_mode: str = "bearer"
    session_secret: str = "replace-this-session-secret-before-deployment"  # noqa: S105
    base_url: str = "http://localhost:8000"
    allowed_origins: str = "http://localhost:8000,http://127.0.0.1:8000"
    oauth_issuer: str = ""
    oauth_audience: str = ""
    oauth_required_scopes: str = (
        "matters:read documents:read evidence:search citations:read"
    )
    oauth_jwks_url: str = ""

    max_upload_bytes: int = 250 * 1024 * 1024
    max_pdf_pages: int = 1000
    read_page_limit: int = 5
    retrieval_candidate_limit: int = 40
    rerank_limit: int = 30
    max_result_limit: int = 20
    worker_poll_seconds: float = 2.0
    ocr_timeout_seconds: int = 7200
    parser_version: str = "legal-layout-v1"

    log_level: str = "INFO"
    secure_cookies: bool = False
    trusted_hosts: str = "localhost,127.0.0.1,testserver"
    cors_origins: list[str] = Field(default_factory=list)

    @property
    def allowed_origin_set(self) -> set[str]:
        return {origin.strip() for origin in self.allowed_origins.split(",") if origin.strip()}

    @property
    def trusted_host_list(self) -> list[str]:
        return [host.strip() for host in self.trusted_hosts.split(",") if host.strip()]

    @property
    def oauth_scope_list(self) -> list[str]:
        scopes = self.oauth_required_scopes.replace(",", " ").split()
        return [scope.strip() for scope in scopes if scope.strip()]

    @property
    def oauth_resource(self) -> str:
        return self.oauth_audience or f"{self.base_url.rstrip('/')}/mcp"

    @property
    def oauth_resource_metadata_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/.well-known/oauth-protected-resource/mcp"

    @property
    def oauth_authorization_server(self) -> str:
        return self.oauth_issuer.rstrip("/")

    @property
    def resolved_oauth_jwks_url(self) -> str:
        if self.oauth_jwks_url:
            return self.oauth_jwks_url
        return f"{self.oauth_authorization_server}/.well-known/jwks.json"

    def ensure_directories(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.model_cache_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "matters").mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_directories()
    return settings
