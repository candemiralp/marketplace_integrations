### Marketplace Integrations

Connects ERPNext directly to the bol.com Retailer API (Fulfilment by Retailer): offers, stock,
orders, shipments, labels, cancellations, returns and invoices. ERPNext is the hub. Shopify keeps
running on `ecommerce_integrations`, whose `Ecommerce Item` and `Ecommerce Integration Log` this
app reuses.

Design and engineering standards: [`docs/stories/00-overview.md`](docs/stories/00-overview.md).
Decisions and spec differences: [`docs/decisions.md`](docs/decisions.md).

### Requirements

Frappe / ERPNext v15 and `ecommerce_integrations` (branch `version-15`).

### Installation

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app --branch version-15 https://github.com/frappe/erpnext
bench get-app --branch version-15 https://github.com/frappe/ecommerce_integrations
bench get-app $URL_OF_THIS_REPO --branch develop
bench --site $SITE install-app marketplace_integrations
```

`required_apps` makes `install-app` install ERPNext and `ecommerce_integrations` first. If either
isn't in the bench, the install stops with `Could not find app "<name>"`.

Installing (and every `bench migrate`) creates the **Bol Manager** role and applies the app's custom
fields. Both steps are idempotent. Bol Manager has no permissions of its own: each feature's DocTypes
grant them.

### Development

In the dev environment, bench runs inside a container. Use the `make` targets from the
environment repo:

```bash
make migrate                     # migrate dev.localhost and test.localhost
make test                        # all tests on test.localhost
make test ARGS="--module marketplace_integrations.tests.test_install"
make lint                        # pre-commit run --all-files
```

On a plain bench: `bench --site <test-site> run-tests --app marketplace_integrations`
(the site needs `allow_tests`).

- Custom fields on other apps' DocTypes go in `marketplace_integrations/setup/custom_fields.py`
  only. Field names start with `bol_`; bol identifiers are `read_only` and `no_copy`.
- bol API specs downloaded for a story go in `docs/bol-openapi/`.

### Linting

`pre-commit` runs ruff (lint + format), Frappe's semgrep rules, prettier and eslint:

```bash
cd apps/marketplace_integrations
pre-commit install
```

### CI

- **CI** (`.github/workflows/ci.yml`): sets up a v15 bench with ERPNext and
  `ecommerce_integrations`, installs the app and runs the tests on every push to `develop` and on PRs.
- **Linters** (`.github/workflows/linter.yml`): pre-commit, Frappe semgrep rules and pip-audit on PRs.

### License

MIT
