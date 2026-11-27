"""The list picker: filtering, moving, choosing, what it draws."""

from karox6.tui_picker import ROWS, PickState


def _text(fragments):
    return "".join(t for _, t in fragments)


def test_starts_on_the_current_item_and_filters_by_words():
    p = PickState("Model", ["mistral-large", "mistral-small", "codestral", "glm-5"], "codestral")
    assert p.chosen() == "codestral"
    p.set_query("stral sm")
    assert p.filtered() == ["mistral-small"]
    assert p.chosen() == "mistral-small"
    p.set_query("")
    assert p.chosen() == "codestral"  # back on the current one


def test_moving_stays_in_the_list_and_scrolls():
    items = [f"m{i:02}" for i in range(30)]
    p = PickState("Model", items)
    p.move(-1)
    assert p.chosen() == "m00"
    p.move(ROWS + 3)
    assert p.chosen() == f"m{ROWS + 3:02}"
    assert p.top == 4  # the cursor row is visible
    text = _text(p.lines())
    assert f"m{ROWS + 3:02}" in text
    assert "m03" not in text
    assert "↑ more" in text
    assert "more" in text.splitlines()[-1]
    p.move(100)
    assert p.chosen() == "m29"
    p.home()
    assert (p.chosen(), p.top) == ("m00", 0)


def test_nothing_matches():
    p = PickState("Model", ["a-model"])
    p.set_query("my-own-model")
    assert p.chosen() == "my-own-model"  # typed as it is
    assert "enter uses “my-own-model”" in _text(p.lines())
    strict = PickState("Provider", ["x"], allow_typed=False)
    strict.set_query("zzz")
    assert strict.chosen() is None


def test_draws_the_cursor_the_mark_and_the_match():
    p = PickState("Model", ["glm-5", "glm-5-pro"], "glm-5")
    p.set_query("pro")
    lines = p.lines(">", "*")
    assert _text(lines) == " > glm-5-pro"
    assert ("class:pick-current class:pick-match", "pro") in lines
    p.set_query("")
    assert _text(p.lines(">", "*")).splitlines() == [" > glm-5 *", "   glm-5-pro"]
    assert "Model (2)" in _text(p.header(" · "))


def test_notes_line_up_beside_the_names():
    p = PickState(
        "Model", ["a", "long-name"], "a", notes={"a": "1M · pictures", "long-name": "128k"}
    )
    rows = _text(p.lines(">", "*")).splitlines()
    assert rows == [" > a *        1M · pictures", "   long-name  128k"]
