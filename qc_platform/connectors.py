from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol


@dataclass(frozen=True)
class QCInput:
    source_system: str
    external_reference: str | None
    data: Mapping[str, Any]
    evidence: tuple[Mapping[str, Any], ...] = ()
    observed_at: datetime | None = None


class ApplicationConnector(Protocol):
    """Translate a client system's approved input format into platform QC input."""

    connector_id: str

    def normalize_submission(self, payload: Mapping[str, Any]) -> QCInput:
        """Normalize one source record without discarding its provenance."""
        ...

    def healthcheck(self) -> bool:
        """Return whether the configured integration is available."""
        ...


class ConnectorRegistry:
    """Allow only connectors explicitly registered by the application deployment."""

    def __init__(self) -> None:
        self._connectors: dict[str, ApplicationConnector] = {}

    def register(self, connector: ApplicationConnector) -> None:
        if connector.connector_id in self._connectors:
            raise ValueError(f"Connector already registered: {connector.connector_id}")
        self._connectors[connector.connector_id] = connector

    def get(self, connector_id: str) -> ApplicationConnector:
        try:
            return self._connectors[connector_id]
        except KeyError as error:
            raise LookupError(f"Connector is not registered: {connector_id}") from error
