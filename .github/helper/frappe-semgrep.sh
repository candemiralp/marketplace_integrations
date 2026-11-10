#!/usr/bin/env bash
# pre-commit hook: run Frappe's semgrep rules (github.com/frappe/semgrep-rules) on the given files.
# The rules repo ships no pre-commit hook, so clone it once at a pinned commit into the user cache.
set -euo pipefail

RULES_SHA=81a6e3d47a328249e8ddf04c586e493dd5553002
RULES_DIR="${XDG_CACHE_HOME:-$HOME/.cache}/frappe-semgrep-rules/$RULES_SHA"

if [ ! -d "$RULES_DIR/rules" ]; then
	tmp="$(mktemp -d)"
	git clone --quiet https://github.com/frappe/semgrep-rules.git "$tmp"
	git -C "$tmp" checkout --quiet "$RULES_SHA"
	mkdir -p "$(dirname "$RULES_DIR")"
	mv "$tmp" "$RULES_DIR"
fi

exec semgrep scan --error --quiet --metrics=off --disable-version-check --config "$RULES_DIR/rules" "$@"
