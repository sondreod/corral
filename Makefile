CONFIG ?= examples/dev-config.toml

.PHONY: help sync test run check deb clean

help:
	@echo "make sync    install the development environment (uv sync)"
	@echo "make test    run the tests"
	@echo "make run     run scriptrunner with CONFIG (default: $(CONFIG))"
	@echo "make check   validate CONFIG"
	@echo "make deb     build the Debian package into dist/"
	@echo "make clean   remove build output, caches and dev data"

sync:
	uv sync

test:
	uv run pytest

run:
	uv run scriptrunner --config $(CONFIG)

check:
	uv run scriptrunner --check --config $(CONFIG)

deb:
	./packaging/build-deb.sh

clean:
	rm -rf dist .dev-data .pytest_cache
	find . -name __pycache__ -not -path './.venv/*' -not -path './examples/demo-project/.venv/*' -prune -exec rm -rf {} +
