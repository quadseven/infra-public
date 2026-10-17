"""Fixture module for canary.ship-python.yml."""


def greeting(name: str) -> str:
    """Return a greeting for `name`."""
    return f"hello, {name}"


if __name__ == "__main__":
    print(greeting("canary"))
