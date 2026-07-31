from integrations.google_fx.services import google_fx_image as image_service


class _Page:
    def __init__(self, url="https://labs.google/fx/tools/flow"):
        self.url = url
        self.goto_calls = []

    def goto(self, url, timeout=None):
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
    assert page.goto_calls == [("https://labs.google/fx/tools/flow", 60000)]


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


def test_missing_workspace_and_failed_creation_still_raise(monkeypatch):
    page = _Page()
    monkeypatch.setattr(image_service, "random_sleep", lambda *_: None)
    monkeypatch.setattr(
        image_service, "_find_fx_prompt_input", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        image_service, "_click_new_project_button", lambda _page: False
    )

    try:
        image_service._open_image_flow_canvas(page)
    except RuntimeError as exc:
        assert "usable Flow canvas" in str(exc)
    else:
        raise AssertionError("expected an unusable Flow page to fail")
