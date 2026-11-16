"""Public runtime state shared by the homepage and service diagnostics."""

from typing import Literal

from pydantic import Field

from backend.configuration.models import Schema


class RuntimeReport(Schema):
    status: str
    service: Literal["opencodex-lite"]
    runtime: Literal["python"]
    version: str
    pid: int
    port: int
    startedAt: str
    uptimeSeconds: float
    consoleReady: bool
    logPath: str | None = None
    stateDirectory: str | None = None


class ServiceOperation(Schema):
    id: str
    action: str
    state: str
    updatedAt: float
    error: str | None = None


class StartupHealth(Schema):
    status: Literal["protected", "at-risk"]
    platform: Literal["win32"]
    mode: Literal["user-logon"]
    taskName: str
    stateDirectory: str
    port: int
    runtimePort: int | None
    runtimeState: str
    logPath: str
    supervisorLogPath: str
    autostartEnabled: bool
    rebootSafe: bool
    protection: Literal["service", "none"]
    serviceInstalled: bool
    serviceEnabled: bool
    serviceViable: bool
    serviceStale: bool
    serviceConflict: bool
    serviceSupported: bool
    serviceRunning: bool
    managerRunning: bool
    diagnosticStale: bool
    error: str | None = None
    serviceOperation: ServiceOperation | None = None
    commands: dict[str, str] = Field(default_factory=dict)


class ChannelCounts(Schema):
    total: int
    enabled: int


class SetupMilestones(Schema):
    first_success_at: str | None


class Overview(Schema):
    runtime: RuntimeReport
    startup: StartupHealth
    channels: ChannelCounts
    milestones: SetupMilestones
