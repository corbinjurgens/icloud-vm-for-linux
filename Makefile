# Makefile — developer and operator entry points for the iCloud bridge.
#
# `make` on its own lists the targets. The short version:
#
#   make venv        create .venv with the test dependencies
#   make check       everything verifiable on a machine with no VM (lint + tests)
#   make deb         build dist/icloud-bridge_<version>_all.deb
#   make install     install that .deb, then run `make configure`
#
# Almost nothing in this repo is compiled; "build" means staging files into a
# package tree. The targets that need a real KVM host, a Windows guest or a live
# CIFS mount (deps, install, configure, acceptance) are marked as such below and
# cannot be validated in a checkout alone.
SHELL := /usr/bin/env bash
.DEFAULT_GOAL := help

VERSION := $(shell sed -n 's/^__version__ = "\(.*\)"/\1/p' gui/icloud_bridge_gui/__init__.py)
DEB     := dist/icloud-bridge_$(VERSION)_all.deb

VENV       := .venv
VENV_STAMP := $(VENV)/.stamp-dev
VENV_QT    := .venv-qt
QT_STAMP   := $(VENV_QT)/.stamp-qt
PYTHON     ?= python3

# Every shell script in the repo, including the two extensionless root helpers.
SHELL_SCRIPTS := $(wildcard host/*.sh gui/*.sh packaging/*.sh tools/*.sh) \
                 host/icloud-bridge-power host/icloud-bridge-configure
PS_SCRIPTS    := $(wildcard provision/*.ps1 guest-agent/*.ps1 tools/*.ps1)

PWSH_VERSION := 7.4.6
PWSH_DIR     := build/pwsh
PWSH         := $(PWSH_DIR)/pwsh

.PHONY: help version venv venv-qt test test-qt test-all lint lint-ps check \
        deb install uninstall purge configure install-gui run deps acceptance \
        clean distclean

# ------------------------------------------------------------------- meta ----

help: ## List the available targets
	@echo "icloud-bridge $(VERSION)"
	@echo
	@awk 'BEGIN {FS = ":.*## "} /^[a-zA-Z_-]+:.*## / \
		{printf "  \033[1m%-14s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)
	@echo
	@echo "  Targets needing a real host (KVM, guest, mounts): deps, install,"
	@echo "  uninstall, purge, configure, acceptance."

version: ## Print the version the package will carry
	@echo $(VERSION)

# ------------------------------------------------------- dependencies --------

$(VENV_STAMP):
	@echo "==> Creating $(VENV) (test dependencies)"
	$(PYTHON) -m venv $(VENV)
	$(VENV)/bin/pip install --quiet --upgrade pip
	$(VENV)/bin/pip install --quiet pytest
	@touch $@

venv: $(VENV_STAMP) ## Create .venv with pytest (PEP 668-safe; never --user)

$(QT_STAMP):
	@echo "==> Creating $(VENV_QT) (test dependencies + PySide6, ~200 MB)"
	$(PYTHON) -m venv $(VENV_QT)
	$(VENV_QT)/bin/pip install --quiet --upgrade pip
	$(VENV_QT)/bin/pip install --quiet pytest PySide6
	@touch $@

venv-qt: $(QT_STAMP) ## Create .venv-qt with pytest + PySide6 (large download)

deps: ## HOST: install runtime prerequisites (docker, cifs-utils, KVM check)
	sudo ./host/setup-prereqs.sh

# -------------------------------------------------------------- testing ------

test: $(VENV_STAMP) ## Run the pytest suite without Qt installed
	$(VENV)/bin/pytest gui/tests

test-qt: $(QT_STAMP) ## Run the pytest suite with PySide6 present
	$(VENV_QT)/bin/pytest gui/tests

test-all: test test-qt ## Run the suite both with and without PySide6
	@echo "PASS: suite passes with and without PySide6"

# ------------------------------------------------------------- linting -------

lint: ## Syntax-check shell, compose and Python; verify the agent.ps1 copies
	@fail=0; \
	echo "==> bash -n"; \
	for f in $(SHELL_SCRIPTS); do \
	  bash -n "$$f" || { echo "FAIL: $$f"; fail=1; }; \
	done; \
	echo "PASS: $(words $(SHELL_SCRIPTS)) shell scripts parse"; \
	echo "==> shell maintainer scripts (sh -n)"; \
	for f in packaging/deb/postinst packaging/deb/prerm packaging/deb/postrm; do \
	  sh -n "$$f" || { echo "FAIL: $$f"; fail=1; }; \
	done; \
	echo "PASS: maintainer scripts parse"; \
	echo "==> python"; \
	$(PYTHON) -m compileall -q gui/icloud_bridge_gui gui/tests >/dev/null \
	  || { echo "FAIL: python syntax"; fail=1; }; \
	echo "PASS: python compiles"; \
	echo "==> agent.ps1 copies"; \
	if cmp -s guest-agent/agent.ps1 provision/agent.ps1; then \
	  echo "PASS: guest-agent/agent.ps1 == provision/agent.ps1"; \
	else \
	  echo "FAIL: agent.ps1 copies have diverged"; fail=1; \
	fi; \
	echo "==> docker compose config"; \
	if command -v docker >/dev/null 2>&1; then \
	  docker compose config >/dev/null && echo "PASS: compose config valid" \
	    || { echo "FAIL: docker compose config"; fail=1; }; \
	else \
	  echo "SKIP: docker is not installed"; \
	fi; \
	echo "==> optional linters"; \
	if command -v shellcheck >/dev/null 2>&1; then \
	  shellcheck $(SHELL_SCRIPTS) && echo "PASS: shellcheck" || { echo "FAIL: shellcheck"; fail=1; }; \
	else \
	  echo "SKIP: shellcheck is not installed (bash -n only checks syntax, not quoting)"; \
	fi; \
	if command -v desktop-file-validate >/dev/null 2>&1; then \
	  echo "SKIP: .desktop files carry __LAUNCHER__ placeholders until installed"; \
	else \
	  echo "SKIP: desktop-file-validate is not installed"; \
	fi; \
	echo "NOTE: PowerShell is not checked here — run 'make lint-ps' for that."; \
	exit $$fail

$(PWSH):
	@echo "==> Fetching PowerShell $(PWSH_VERSION) into $(PWSH_DIR)"
	mkdir -p $(PWSH_DIR)
	curl -fsSL -o $(PWSH_DIR)/pwsh.tar.gz \
	  https://github.com/PowerShell/PowerShell/releases/download/v$(PWSH_VERSION)/powershell-$(PWSH_VERSION)-linux-x64.tar.gz
	tar -xzf $(PWSH_DIR)/pwsh.tar.gz -C $(PWSH_DIR)
	chmod +x $@
	rm -f $(PWSH_DIR)/pwsh.tar.gz

lint-ps: $(PWSH) ## Parse the .ps1 files with PowerShell 7 (downloads ~70 MB)
	@echo "==> Parsing PowerShell scripts"
	@$(PWSH) -NoProfile -File packaging/lint-ps1.ps1 $(PS_SCRIPTS)
	@echo "NOTE: PS 7 parses a superset of PS 5.1 and cannot execute the guest-only"
	@echo "      parts (cfapi interop, Get-LocalUser, SMB cmdlets, scheduled tasks)."

check: lint test ## Everything verifiable without a VM: lint + tests

# ------------------------------------------------------------- packaging -----

deb: ## Build dist/icloud-bridge_<version>_all.deb
	./packaging/build-deb.sh

install: $(DEB) ## HOST: install the built .deb (then run 'make configure')
	sudo apt install -y ./$(DEB)

$(DEB):
	$(MAKE) deb

uninstall: ## HOST: remove the package, keeping credentials and config
	sudo apt remove -y icloud-bridge

purge: ## HOST: remove the package AND its credentials, sudoers grant and marker
	sudo apt purge -y icloud-bridge

configure: ## HOST: apply machine-specific config (credentials, uid/gid, sudoers)
	sudo icloud-bridge-configure --env-file "$(CURDIR)/.env"

install-gui: ## Per-user GUI install into your home dir (the non-package path)
	./gui/install-gui.sh

# ------------------------------------------------------------- running -------

run: ## Run the GUI from the source tree (uses .venv-qt if you built it)
	@if [ -x $(VENV_QT)/bin/python ]; then \
	  echo "==> Running from $(VENV_QT)"; \
	  cd gui && ../$(VENV_QT)/bin/python -m icloud_bridge_gui; \
	else \
	  echo "==> Running with $(PYTHON) (run 'make venv-qt' if PySide6 is missing)"; \
	  cd gui && $(PYTHON) -m icloud_bridge_gui; \
	fi

acceptance: ## HOST: run the host-side acceptance checks
	./host/acceptance-tests.sh

# -------------------------------------------------------------- cleaning -----

clean: ## Remove build products and bytecode (incl. the fetched pwsh)
	rm -rf build dist
	find . -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
	find . -name '.pytest_cache' -type d -prune -exec rm -rf {} + 2>/dev/null || true

distclean: clean ## Also remove the virtualenvs
	rm -rf $(VENV) $(VENV_QT)
