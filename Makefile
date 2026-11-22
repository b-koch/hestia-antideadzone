PYTHON ?= python3
PREFIX ?= $(HOME)/.local
BINDIR ?= $(PREFIX)/bin
CONFIGDIR ?= $(HOME)/.config/hestia/anti-deadzone
SYSTEMDUSERDIR ?= $(HOME)/.config/systemd/user

.PHONY: build install-user uninstall-user validate clean

build:
	$(PYTHON) build.py -o dist/adz

install-user: build
	install -Dm755 dist/adz $(BINDIR)/adz
	@if [ ! -f "$(CONFIGDIR)/config.json" ]; then \
		install -Dm644 config.example.json "$(CONFIGDIR)/config.json"; \
		echo "Installed default config to $(CONFIGDIR)/config.json"; \
	fi
	install -Dm644 packaging/systemd/antideadzone.service $(SYSTEMDUSERDIR)/antideadzone.service
	systemctl --user daemon-reload
	@echo "Installed $(BINDIR)/adz and $(SYSTEMDUSERDIR)/antideadzone.service"

uninstall-user:
	-systemctl --user disable --now antideadzone.service
	rm -f $(BINDIR)/adz $(SYSTEMDUSERDIR)/antideadzone.service
	rm -rf $(SYSTEMDUSERDIR)/antideadzone.service.d
	systemctl --user daemon-reload

validate:
	$(PYTHON) -m hestia_adz validate

clean:
	rm -rf dist __pycache__ hestia_adz/__pycache__
