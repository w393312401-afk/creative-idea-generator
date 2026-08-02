from integrations.google_fx.services import google_fx_image as image_service


class _Page:
    def __init__(self, url="https://labs.google/fx/tools/flow"):
        self.url = url
        self.goto_calls = []

    def goto(self, url, timeout=None, **kwargs):
        self.goto_calls.append((url, timeout))
        self.url = url


def test_route_less_flow_workspace_is_reused_without_new_project(monkeypatch):
    page = _Page()
    events = []
    monkeypatch.setattr(image_service, "random_sleep", lambda *_: None)
    monkeypatch.setattr(
        image_service, "_find_fx_prompt_input", lambda *_args, **_kwargs: object()
    )
    monkeypatch.setattr(
        image_service,
        "_click_new_project_button",
        lambda _page: events.append("unexpected_create"),
    )

    project_url = image_service._open_image_flow_canvas(page)

    assert project_url is None
    assert events == []
    # A usable route-less workspace is already the active canvas; reconnecting
    # CDP must not reload it and discard task-local canvas state.
    assert page.goto_calls == []


def test_in_place_workspace_creation_is_accepted_without_project_route(monkeypatch):
    page = _Page()
    state = {"workspace": False}
    monkeypatch.setattr(image_service, "random_sleep", lambda *_: None)
    monkeypatch.setattr(
        image_service,
        "_find_fx_prompt_input",
        lambda *_args, **_kwargs: object() if state["workspace"] else None,
    )

    def create_in_place(_page):
        state["workspace"] = True
        return False  # URL-only confirmation cannot see this Flow variant.

    monkeypatch.setattr(image_service, "_click_new_project_button", create_in_place)

    assert image_service._open_image_flow_canvas(page) is None


def test_workspace_home_is_reused_without_reloading_before_new_project(monkeypatch):
    """A ready Flow home has New project but no prompt editor yet.

    Reloading this page destroys the usable DOM and was the direct cause of the
    three fast FLOW_CANVAS_UNAVAILABLE failures seen in production.
    """
    page = _Page()
    state = {"workspace": False, "clicks": 0}
    monkeypatch.setattr(image_service, "random_sleep", lambda *_: None)
    monkeypatch.setattr(
        image_service,
        "_find_fx_prompt_input",
        lambda *_args, **_kwargs: object() if state["workspace"] else None,
    )

    def create_from_loaded_home(_page):
        state["clicks"] += 1
        state["workspace"] = True
        _page.url = "https://labs.google/fx/tools/flow/project/reused-home"
        return True

    monkeypatch.setattr(
        image_service, "_click_new_project_button", create_from_loaded_home
    )

    assert image_service._open_image_flow_canvas(page).endswith("/reused-home")
    assert state["clicks"] == 1
    assert page.goto_calls == []


def test_missing_workspace_and_failed_creation_still_raise(monkeypatch):
    page = _Page()
    monkeypatch.setattr(image_service, "random_sleep", lambda *_: None)
    monkeypatch.setattr(
        image_service, "_find_fx_prompt_input", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        image_service, "_click_new_project_button", lambda _page: False
    )
    monkeypatch.setattr(
        image_service, "ensure_flow_workspace", lambda *_args, **_kwargs: False
    )

    try:
        image_service._open_image_flow_canvas(page)
    except RuntimeError as exc:
        assert "FLOW_CANVAS_UNAVAILABLE" in str(exc)
    else:
        raise AssertionError("expected an unusable Flow page to fail")


def test_stale_bound_project_falls_back_to_project_home(monkeypatch):
    stale_url = "https://labs.google/fx/tools/flow/project/dead-project"
    page = _Page(url=stale_url)
    state = {"ready": False, "created": False}
    monkeypatch.setattr(image_service, "random_sleep", lambda *_: None)
    monkeypatch.setattr(
        image_service,
        "_find_fx_prompt_input",
        lambda *_args, **_kwargs: object() if state["created"] else None,
    )

    def ensure_workspace(_page, **_kwargs):
        if _page.url == stale_url:
            return False
        state["ready"] = True
        return True

    def create_replacement(_page):
        assert state["ready"] is True
        state["created"] = True
        _page.url = "https://labs.google/fx/tools/flow/project/replacement"
        return True

    monkeypatch.setattr(image_service, "ensure_flow_workspace", ensure_workspace)
    monkeypatch.setattr(image_service, "_click_new_project_button", create_replacement)

    result = image_service._open_image_flow_canvas(page, stale_url)

    assert result.endswith("/replacement")
    assert page.goto_calls == [
        (stale_url, 60000),
        ("https://labs.google/fx/tools/flow", 60000),
    ]
