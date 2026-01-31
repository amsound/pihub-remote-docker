import importlib

import pihub.dispatcher as dispatcher


def test_repeat_constants_ignore_env(monkeypatch) -> None:
    monkeypatch.setenv("REPEAT_INITIAL_MS", "fast")
    monkeypatch.setenv("REPEAT_RATE_MS", "fast")

    reloaded = importlib.reload(dispatcher)

    assert reloaded.REPEAT_INITIAL_MS == 400
    assert reloaded.REPEAT_RATE_MS == 400
