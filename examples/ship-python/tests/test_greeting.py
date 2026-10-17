from ship_python_canary import greeting


def test_greeting():
    assert greeting("canary") == "hello, canary"
