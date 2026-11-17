.PHONY: run test unit integration e2e doctor
run:         ; python -m app.main
doctor:      ; python -m app.doctor
test:        ; python -m pytest
unit:        ; python -m pytest tests/unit
integration: ; python -m pytest tests/integration
e2e:         ; python -m pytest tests/e2e
