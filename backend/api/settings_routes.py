"""HTTP endpoint registration for the retained settings use cases."""

from typing import Annotated, Any

from fastapi import APIRouter, Body

from backend.application.agents import AgentManagement
from backend.application.helper_settings import HelperPolicyManagement
from backend.application.model_selection import ModelSelectionManagement
from backend.application.request_settings import RequestPolicyManagement


def settings_router(
    service,
    catalog,
    integration,
    workflow,
    fallback,
    *,
    auth_status=None,
    pacer=None,
    sizing=None,
    apply_changes=None,
):
    router = APIRouter(prefix="/api")
    dependencies = dict(
        service=service,
        catalog=catalog,
        apply_changes=apply_changes,
    )
    request_settings = RequestPolicyManagement(
        **dependencies, workflow=workflow, fallback=fallback, pacer=pacer
    )
    router.add_api_route("/settings", request_settings.get_settings, methods=["GET"])
    router.add_api_route("/settings", request_settings.put_settings, methods=["PUT"])
    router.add_api_route("/simple-fallback", request_settings.get_fallback, methods=["GET"])
    router.add_api_route("/simple-fallback", request_settings.put_fallback, methods=["PUT"])
    router.add_api_route(
        "/workflow-budget/settings", request_settings.workflow_settings, methods=["GET"]
    )

    @router.put("/workflow-budget/settings")
    def set_workflow(body: Annotated[Any, Body()] = None):
        return request_settings.set_workflow(body)

    router.add_api_route("/workflow-budget", request_settings.workflow_view, methods=["GET"])
    router.add_api_route(
        "/workflow-budget/clear", request_settings.workflow_clear, methods=["POST"]
    )
    router.add_api_route("/spend-settings", request_settings.spend_settings, methods=["GET"])

    @router.put("/spend-settings")
    def put_spend(body: Annotated[Any, Body()] = None):
        return request_settings.put_spend(body)

    router.add_api_route(
        "/provider-request-pacing", request_settings.pacing_status, methods=["GET"]
    )
    agents = AgentManagement(**dependencies, integration=integration, sizing=sizing)
    router.add_api_route("/effort-caps", agents.effort_caps, methods=["GET"])
    router.add_api_route("/effort-caps", agents.put_effort, methods=["PUT"])
    router.add_api_route("/subagent-models", agents.roster, methods=["GET"])
    router.add_api_route("/subagent-models", agents.put_roster, methods=["PUT"])
    router.add_api_route("/subagent-model-fallback", agents.subagent_fallback, methods=["GET"])
    router.add_api_route("/subagent-model-fallback", agents.put_subagent_fallback, methods=["PUT"])
    router.add_api_route("/subagent-defaults", agents.defaults, methods=["GET"])
    router.add_api_route("/subagent-defaults", agents.put_defaults, methods=["PUT"])
    router.add_api_route("/codex-agent-settings", agents.native_settings, methods=["GET"])
    router.add_api_route("/codex-agent-settings", agents.put_native, methods=["PUT"])
    router.add_api_route("/subagent-defaults/suggest", agents.suggest_defaults, methods=["POST"])
    model_selection = ModelSelectionManagement(**dependencies)
    router.add_api_route("/selected-models", model_selection.select_models, methods=["PUT"])
    router.add_api_route("/model-visibility", model_selection.visibility, methods=["PUT"])
    router.add_api_route("/disabled-models", model_selection.disabled_models, methods=["PUT"])
    router.add_api_route("/provider-context-caps", model_selection.caps, methods=["GET"])
    router.add_api_route("/provider-context-caps", model_selection.set_caps, methods=["PUT"])
    router.add_api_route("/model-discovery", model_selection.discovery, methods=["GET"])
    router.add_api_route("/model-discovery", model_selection.set_discovery, methods=["PUT"])
    router.add_api_route(
        "/model-discovery/acknowledge", model_selection.acknowledge, methods=["POST"]
    )
    router.add_api_route("/model-presets", model_selection.model_presets, methods=["GET"])
    router.add_api_route("/model-presets", model_selection.set_model_preset, methods=["PUT"])
    helper_settings = HelperPolicyManagement(**dependencies, auth_status=auth_status)
    router.add_api_route("/sidecar-settings", helper_settings.sidecars, methods=["GET"])
    router.add_api_route("/sidecar-settings", helper_settings.put_sidecars, methods=["PUT"])
    return router
