# Contributing

Thank you for helping improve LLM Transit Gateway.

## Before opening a change

- Search existing issues and pull requests first.
- Keep each change focused on one problem.
- Never include API keys, service-account JSON, tokens, local configuration, or
  production request/response contents.
- For a security issue, follow [SECURITY.md](SECURITY.md) instead of opening a
  public issue.

## Development setup

Use Python 3.11, then install the project dependencies:

```bash
python -m pip install -r requirements.txt
```

Run the regression suite and syntax checks before submitting a pull request:

```bash
python -m unittest discover -s tests -v
python -m py_compile proxy.py provider_manager.py smoketest.py
bash -n install.sh start.sh stop.sh restart.sh
```

Tests must use fake credentials and must not call paid or production services.

## Pull requests

Explain the root cause, why the chosen change fixes it, and how you verified the
result. Add or update a regression test for behavior changes. Keep unrelated
formatting and refactors out of the pull request.
