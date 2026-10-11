# droidrt build, test and install helpers.
#
#   make build      compile the launcher, the icon compositor and the device agent (when present)
#   make test       run the unit tests
#   make install    symlink droidrt.py to $(BINDIR)/droidrt
#   make uninstall  remove that symlink
#   make clean      remove build outputs

PYTHON ?= python3
PREFIX ?= $(if $(wildcard /opt/homebrew/bin),/opt/homebrew,/usr/local)
BINDIR ?= $(PREFIX)/bin

AGENT_BUILD := $(wildcard agent-apk/build.sh)

.PHONY: all build build-launcher build-iconkit build-agent test install uninstall clean help

all: build

help:
	@echo "targets: build test install uninstall clean"
	@echo "PREFIX=$(PREFIX) BINDIR=$(BINDIR) PYTHON=$(PYTHON)"

build: build-launcher build-iconkit build-agent

build-launcher:
	/bin/sh launcher/build.sh

build-iconkit:
	/bin/bash iconkit/build.sh

build-agent:
ifneq ($(AGENT_BUILD),)
	/bin/sh $(AGENT_BUILD)
else
	@echo "agent-apk/build.sh not found, skipping the device agent"
endif

test:
	$(PYTHON) -m unittest -v test_droidrt
	$(PYTHON) -m unittest discover -v -s iconkit -p "test_*.py"
	@if [ -f semantic/test_semantic.py ]; then \
		echo "$(PYTHON) -m unittest -v semantic.test_semantic"; \
		$(PYTHON) -m unittest -v semantic.test_semantic; \
	else echo "semantic/test_semantic.py not found, skipping"; fi
	@if [ -f test_mcp.py ]; then \
		echo "$(PYTHON) -m unittest -v test_mcp"; \
		$(PYTHON) -m unittest -v test_mcp; \
	elif [ -f droidrt_mcp/test_mcp.py ]; then \
		echo "$(PYTHON) -m unittest -v droidrt_mcp.test_mcp"; \
		$(PYTHON) -m unittest -v droidrt_mcp.test_mcp; \
	else echo "test_mcp.py not found, skipping"; fi

install: build-launcher build-iconkit
	@mkdir -p "$(BINDIR)"
	ln -sfn "$(CURDIR)/droidrt.py" "$(BINDIR)/droidrt"
	@echo "installed $(BINDIR)/droidrt -> $(CURDIR)/droidrt.py"
	@echo "keep this checkout in place: generated launchers call droidrt.py from here"

uninstall:
	@link="$(BINDIR)/droidrt"; \
	if [ -L "$$link" ] && [ "$$(readlink "$$link")" = "$(CURDIR)/droidrt.py" ]; then \
		rm -f "$$link"; echo "removed $$link"; \
	elif [ -e "$$link" ] || [ -L "$$link" ]; then \
		echo "$$link does not point to this checkout, leaving it alone"; \
	else echo "$$link is not installed"; fi
	@echo "launchers in ~/Applications/Android and the AVD in ~/.android/avd are kept; remove them by hand if you want"

clean:
	rm -rf launcher/build iconkit/build
	find . -path ./.git -prune -o -type d -name __pycache__ -prune -exec rm -rf {} +
