import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Set

from utils.logger import get_logger

logger = get_logger("risk")


class HealthStatus(Enum):
    OK = "OK"
    DEGRADED = "DEGRADED"
    CRITICAL = "CRITICAL"


@dataclass
class HealthReport:
    status: HealthStatus = HealthStatus.OK
    degraded_components: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    critical_alerts: List[str] = field(default_factory=list)
    timestamp: str = field(default_factory=lambda: datetime.utcnow().replace(tzinfo=timezone.utc).isoformat())
    metrics: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status.value,
            "degraded_components": self.degraded_components,
            "warnings": self.warnings,
            "critical_alerts": self.critical_alerts,
            "timestamp": self.timestamp,
            "metrics": self.metrics,
        }


class HealthCheck:
    def __init__(self) -> None:
        self.last_websocket_update: Dict[str, datetime] = {}
        self.reconciliation_mismatch_count: int = 0
        self.persistence_failures: int = 0
        self.last_ping_latency_ms: Optional[float] = None
        self.last_portfolio_cache_refresh: Optional[datetime] = None
        self.missing_protection_symbols: Set[str] = set()
        self.api_failure_counts: Dict[str, int] = {}
        self.health_log = get_logger("execution")
        self._stale_websocket_seconds = 20
        self._latency_warning_ms = 250
        self._latency_critical_ms = 500
        self._mismatch_warning_threshold = 1
        self._persistence_warning_threshold = 1
        self._api_failure_warning_threshold = 3

    def record_websocket_update(self, symbol: str) -> None:
        self.last_websocket_update[symbol] = datetime.utcnow().replace(tzinfo=timezone.utc)
        self.health_log.info(
            "websocket_freshness",
            extra={
                "symbol": symbol,
                "metadata": {"event": "websocket_update"},
            },
        )

    def record_reconciliation_report(self, mismatch_count: int) -> None:
        self.reconciliation_mismatch_count = mismatch_count
        self.health_log.info(
            "reconciliation_report",
            extra={
                "metadata": {"mismatch_count": mismatch_count},
            },
        )

    def record_persistence_failure(self, error: str) -> None:
        self.persistence_failures += 1
        self.health_log.warning(
            "persistence_failure",
            extra={
                "metadata": {"error": error, "count": self.persistence_failures},
            },
        )

    def record_ping_latency(self, latency_ms: float) -> None:
        self.last_ping_latency_ms = latency_ms
        self.health_log.info(
            "binance_ping_latency",
            extra={
                "metadata": {"latency_ms": latency_ms},
            },
        )

    def record_portfolio_cache_refresh(self) -> None:
        self.last_portfolio_cache_refresh = datetime.utcnow().replace(tzinfo=timezone.utc)
        self.health_log.info(
            "portfolio_cache_refreshed",
            extra={
                "metadata": {},
            },
        )

    def record_missing_protection(self, symbol: str) -> None:
        self.missing_protection_symbols.add(symbol)
        self.health_log.warning(
            "missing_protection_detected",
            extra={
                "symbol": symbol,
                "metadata": {},
            },
        )

    def record_api_failure(self, source: str, error: str) -> None:
        self.api_failure_counts[source] = self.api_failure_counts.get(source, 0) + 1
        self.health_log.warning(
            "api_failure",
            extra={
                "metadata": {"source": source, "error": error, "count": self.api_failure_counts[source]},
            },
        )

    def evaluate(self) -> HealthReport:
        report = HealthReport()
        now = datetime.utcnow().replace(tzinfo=timezone.utc)
        report.metrics = {
            "websocket_symbols": list(self.last_websocket_update.keys()),
            "reconciliation_mismatch_count": self.reconciliation_mismatch_count,
            "persistence_failures": self.persistence_failures,
            "last_ping_latency_ms": self.last_ping_latency_ms,
            "last_portfolio_cache_refresh": self._format_time(self.last_portfolio_cache_refresh),
            "stale_portfolio_cache_seconds": self._stale_cache_seconds(now),
            "missing_protection_count": len(self.missing_protection_symbols),
            "api_failure_counts": dict(self.api_failure_counts),
        }

        stale_websockets = self._detect_stale_websockets(now)
        if stale_websockets:
            report.status = HealthStatus.DEGRADED
            report.degraded_components.append("websocket_freshness")
            report.warnings.append(
                f"Stale websocket for symbols: {', '.join(sorted(stale_websockets))}"
            )

        if self.reconciliation_mismatch_count >= self._mismatch_warning_threshold:
            report.status = HealthStatus.DEGRADED
            report.degraded_components.append("reconciliation")
            report.warnings.append(
                f"{self.reconciliation_mismatch_count} reconciliation mismatches detected"
            )

        if self.persistence_failures >= self._persistence_warning_threshold:
            report.status = HealthStatus.DEGRADED
            report.degraded_components.append("persistence")
            report.warnings.append(
                f"{self.persistence_failures} persistence failures recorded"
            )

        if self.last_ping_latency_ms is not None:
            if self.last_ping_latency_ms >= self._latency_critical_ms:
                report.status = HealthStatus.CRITICAL
                report.degraded_components.append("ping_latency")
                report.critical_alerts.append(
                    f"Binance ping latency too high: {self.last_ping_latency_ms}ms"
                )
            elif self.last_ping_latency_ms >= self._latency_warning_ms:
                report.status = HealthStatus.DEGRADED
                report.degraded_components.append("ping_latency")
                report.warnings.append(
                    f"Binance ping latency elevated: {self.last_ping_latency_ms}ms"
                )

        if self.missing_protection_symbols:
            report.status = HealthStatus.DEGRADED
            report.degraded_components.append("missing_protection")
            report.warnings.append(
                f"Missing protection on {len(self.missing_protection_symbols)} symbol(s): {', '.join(sorted(self.missing_protection_symbols))}"
            )

        repeated_api_failures = [source for source, count in self.api_failure_counts.items() if count >= self._api_failure_warning_threshold]
        if repeated_api_failures:
            report.status = HealthStatus.DEGRADED
            report.degraded_components.append("api_failures")
            report.warnings.append(
                f"Repeated API failures for: {', '.join(sorted(repeated_api_failures))}"
            )

        logger.info("health_check_evaluation", extra={"metadata": report.to_dict()})
        return report

    def _detect_stale_websockets(self, now: datetime) -> List[str]:
        stale = []
        for symbol, last_update in self.last_websocket_update.items():
            if (now - last_update).total_seconds() > self._stale_websocket_seconds:
                stale.append(symbol)
        return stale

    def _stale_cache_seconds(self, now: datetime) -> Optional[float]:
        if self.last_portfolio_cache_refresh is None:
            return None
        return (now - self.last_portfolio_cache_refresh).total_seconds()

    def _format_time(self, timestamp: Optional[datetime]) -> Optional[str]:
        return timestamp.isoformat() if timestamp else None
