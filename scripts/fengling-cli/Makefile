PYTHON ?= python3
PREFIX ?= $(HOME)/.local
BIN_DIR ?= $(PREFIX)/bin

.PHONY: install-local test
install-local:
	mkdir -p $(BIN_DIR)
	printf '%s\n' '#!/usr/bin/env bash' > $(BIN_DIR)/fengling
	printf '%s\n' 'PYTHONPATH="$(CURDIR)" exec $(PYTHON) -m fengling_cli.main "$$@"' >> $(BIN_DIR)/fengling
	chmod +x $(BIN_DIR)/fengling

test:
	$(PYTHON) -m unittest discover -s tests
