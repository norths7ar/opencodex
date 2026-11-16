"""Service definitions and endpoint resolution for configured upstream channels."""

from .endpoints import GenerationEndpoint, discovery_url, generation_endpoint, wire_protocol
from .policy import request_policy
from .registry import (
    SERVICES,
    Destination,
    MatchedService,
    ServiceDefinition,
    ServiceId,
    match_destination,
)

__all__ = [
    "SERVICES",
    "Destination",
    "GenerationEndpoint",
    "MatchedService",
    "ServiceDefinition",
    "ServiceId",
    "discovery_url",
    "generation_endpoint",
    "match_destination",
    "request_policy",
    "wire_protocol",
]
