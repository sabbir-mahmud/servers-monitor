# Contributing to Server Monitor

Thanks for helping! Bug reports, ideas, documentation and code are all welcome.

## Reporting bugs and requesting features

- **Bugs:** open an issue with what you did, what you expected, what happened, and versions
  (dashboard commit, agent version from `smctl status`, OS). Logs help: `docker compose logs`,
  or `sudo smctl logs` on a server. **Remove secrets first:** connection strings, tokens and
  `.env` values.
- **Features:** describe the problem you're trying to solve before the solution. The project
  deliberately stays small, so "how does this fit without adding a new service?" is a good
  question to answer up front.
- **Security issues:** never in a public issue. See [SECURITY.md](SECURITY.md).

## Development setup

```bash
git clone https://github.com/sabbir-mahmud/servers-monitor.git
cd servers-monitor
make install                     # Python 3.14 virtualenv with all dependencies
cp main/.env.example main/.env   # set DEBUG=True, SECRET_KEY and REDIS_URL
make migrate superuser
make dev                         # dashboard on :8000 + the relay process
```

A free Upstash database is the easiest Redis for development. To run an agent against your
local dashboard, add a server in the UI, put its connection string in `agent/dev.toml` under
`[main] redis_url = "…"`, and run `make agent-dev`.

## Before you open a pull request

```bash
make test    # dashboard + agent suites; no network or Redis needed
make lint    # ruff check + format check
make check   # Django checks, migrations up to date, production deployment audit
```

All three must pass. Also:

- **Add tests** for behaviour changes. Security fixes get a regression test that performs the
  attack and asserts it fails (see `main/tests/test_security.py`).
- **Agent code must run on Python 3.11.** Servers use their system Python. Ruff is configured
  to keep `agent/` on 3.11 syntax, and a test parses it with the 3.11 grammar.
- **The agent stays dependency-free** apart from `psutil`, and `smctl` uses only the
  standard library.
- **Changed `agent/`?** Its checksums change too. Installs only accept files matching the
  deployed dashboard, so mention it in the PR so releases can note "upgrade agents".
- **Migrations:** run `make check` to confirm they're committed.
- **UI:** match the existing design system in `main/static/css/app.css` (tokens, components,
  light and dark themes). Server-rendered templates and HTMX; no frontend build step.
- **Keep the relay cheap.** Every Redis command costs money on hosted plans; prefer blocking
  calls and batching over polling.

## Code style

- Python formatted and linted with ruff (`make format` fixes most things).
- Comments explain *why*, not what. Match the density of the surrounding code.
- Keep views thin: put multi-model writes in `services.py`, inside transactions, with audit
  records.

## Licensing of contributions

The project is licensed under the [Apache License 2.0](LICENSE). Under section 5 of that
license, any contribution you intentionally submit for inclusion is licensed under the same
terms, with no additional conditions. Please only submit work you have the right to license
this way.
