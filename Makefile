.PHONY: test check clean install-rules

PYTHON ?= python3

test:
	$(PYTHON) tools/test_ep0_protocol.py

check:
	$(PYTHON) -m py_compile tools/*.py
	@echo "All python tools pass syntax check."

install-rules:
	@echo "Installing udev rules..."
	sudo cp udev/39-shnurok.rules /etc/udev/rules.d/
	sudo udevadm control --reload-rules && sudo udevadm trigger
	@echo "udev rules installed and reloaded."

clean:
	rm -rf build/ dist/ tools/__pycache__ compat/__pycache__
