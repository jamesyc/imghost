from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest


class TelemetryMetrics:
    def __init__(self, registry: CollectorRegistry | None = None) -> None:
        self.registry = registry or CollectorRegistry()
        self.http_requests_total = Counter(
            "imghost_http_requests_total",
            "HTTP requests handled by imghost.",
            ("method", "route", "status_class"),
            registry=self.registry,
        )
        self.http_request_duration_seconds = Histogram(
            "imghost_http_request_duration_seconds",
            "HTTP request duration in seconds.",
            ("method", "route"),
            registry=self.registry,
        )
        self.uploads_total = Counter(
            "imghost_uploads_total",
            "Upload results by media and actor kind.",
            ("result", "media_type", "actor_kind", "source"),
            registry=self.registry,
        )
        self.upload_bytes_total = Counter(
            "imghost_upload_bytes_total",
            "Uploaded bytes for successful uploads.",
            ("media_type", "actor_kind", "source"),
            registry=self.registry,
        )
        self.thumbnail_jobs_total = Counter(
            "imghost_thumbnail_jobs_total",
            "Thumbnail job outcomes.",
            ("result", "media_type", "reason"),
            registry=self.registry,
        )
        self.thumbnail_duration_seconds = Histogram(
            "imghost_thumbnail_duration_seconds",
            "Thumbnail processing duration in seconds.",
            ("media_type", "result"),
            registry=self.registry,
        )
        self.auth_events_total = Counter(
            "imghost_auth_events_total",
            "Authentication and authorization events.",
            ("event", "method", "result"),
            registry=self.registry,
        )
        self.oauth_events_total = Counter(
            "imghost_oauth_events_total",
            "OAuth events by provider.",
            ("provider", "event", "result"),
            registry=self.registry,
        )
        self.subsystem_degraded = Gauge(
            "imghost_subsystem_degraded",
            "Whether a subsystem is currently degraded.",
            ("subsystem",),
            registry=self.registry,
        )
        self.subsystem_transitions_total = Counter(
            "imghost_subsystem_transitions_total",
            "Subsystem degrade and recover transitions.",
            ("subsystem", "state"),
            registry=self.registry,
        )
        self.worker_running = Gauge(
            "imghost_worker_running",
            "Whether the task worker is currently running in this process.",
            registry=self.registry,
        )
        self.tasks_enqueued_total = Counter(
            "imghost_tasks_enqueued_total",
            "Tasks enqueued by queue and task name.",
            ("queue", "task_name"),
            registry=self.registry,
        )

    def observe_http_request(self, *, method: str, route: str, status_code: int, duration_seconds: float) -> None:
        status_class = f"{max(0, status_code) // 100}xx"
        self.http_requests_total.labels(method=method.upper(), route=route, status_class=status_class).inc()
        self.http_request_duration_seconds.labels(method=method.upper(), route=route).observe(max(0.0, duration_seconds))

    def record_upload(
        self,
        *,
        result: str,
        media_type: str,
        actor_kind: str,
        source: str,
        byte_count: int | None = None,
        duration_seconds: float | None = None,
    ) -> None:
        self.uploads_total.labels(
            result=result,
            media_type=media_type,
            actor_kind=actor_kind,
            source=source,
        ).inc()
        if result == "success" and byte_count is not None:
            self.upload_bytes_total.labels(
                media_type=media_type,
                actor_kind=actor_kind,
                source=source,
            ).inc(max(0, byte_count))

    def record_thumbnail_job(
        self,
        *,
        result: str,
        media_type: str,
        reason: str | None = None,
        duration_seconds: float | None = None,
    ) -> None:
        normalized_reason = reason or "none"
        self.thumbnail_jobs_total.labels(
            result=result,
            media_type=media_type,
            reason=normalized_reason,
        ).inc()
        if duration_seconds is not None:
            self.thumbnail_duration_seconds.labels(media_type=media_type, result=result).observe(max(0.0, duration_seconds))

    def record_auth_event(self, *, event: str, method: str, result: str) -> None:
        self.auth_events_total.labels(event=event, method=method, result=result).inc()

    def record_oauth_event(self, *, provider: str, event: str, result: str) -> None:
        self.oauth_events_total.labels(provider=provider, event=event, result=result).inc()

    def mark_subsystem_degraded(self, *, subsystem: str) -> None:
        self.subsystem_degraded.labels(subsystem=subsystem).set(1)
        self.subsystem_transitions_total.labels(subsystem=subsystem, state="degraded").inc()

    def mark_subsystem_recovered(self, *, subsystem: str) -> None:
        self.subsystem_degraded.labels(subsystem=subsystem).set(0)
        self.subsystem_transitions_total.labels(subsystem=subsystem, state="recovered").inc()

    def mark_worker_started(self) -> None:
        self.worker_running.set(1)

    def mark_worker_stopped(self) -> None:
        self.worker_running.set(0)

    def record_task_enqueued(self, *, queue: str, task_name: str) -> None:
        self.tasks_enqueued_total.labels(queue=queue, task_name=task_name).inc()

    def render(self) -> bytes:
        return generate_latest(self.registry)
