"""Sentinel test for the rag-orchestrator service.

Goal: catch module-level breakage (bad imports, missing env-var reads,
Pydantic model validation errors at class-definition time, etc.) without
touching Postgres or the LiteLLM endpoint.

Query-path tests (which need a real DB + embedding backend) live in the
integration suite defined by task 07.
"""

from __future__ import annotations


def test_app_module_imports() -> None:
    import app

    assert app.app is not None, "FastAPI app instance must be exported"
    assert app.app.title == "gitdoc-rag-orchestrator"


def test_ask_route_registered() -> None:
    import app

    paths = {route.path for route in app.app.routes}
    assert "/ask" in paths
    assert "/healthz" in paths
    assert "/readyz" in paths


def test_request_response_models_are_pydantic() -> None:
    from pydantic import BaseModel

    import app

    assert issubclass(app.AskRequest, BaseModel)
    assert issubclass(app.AskResponse, BaseModel)
    assert issubclass(app.Citation, BaseModel)


def test_ask_request_validation_bounds() -> None:
    import pytest
    from pydantic import ValidationError

    import app

    # Empty query rejected (min_length=1).
    with pytest.raises(ValidationError):
        app.AskRequest(query="", repo="r")

    # top_k out of bounds rejected (le=20).
    with pytest.raises(ValidationError):
        app.AskRequest(query="q", repo="r", top_k=999)

    # Valid request accepted.
    req = app.AskRequest(query="hello", repo="my-repo")
    assert req.top_k == 6  # default
