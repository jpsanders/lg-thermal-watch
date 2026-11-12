# Contributing

Issues and pull requests are welcome. Say which LG unit you have; the night watch was built against the PC18SK.

## Set up

```sh
git clone https://github.com/jpsanders/lg-thermal-watch && cd lg-thermal-watch
python3.15 -m venv .venv && . .venv/bin/activate
make init-dev
```

## Before a pull request

```sh
make format      # black and isort
make checks      # every check CI runs, in the same order
```

`make checks` runs isort, black, editorconfig-checker, darglint, flake8, mypy in strict mode, bandit, the man page
check and pytest with coverage. CI runs the tests on Python 3.14 and 3.15 on Linux and macOS, then installs the built
wheel, starts the service and checks that the API refuses a call without the token.
