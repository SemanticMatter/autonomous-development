.PHONY: check test precommit doctor clean

check: test
	python3 scripts/validate_project.py

test:
	python3 tests/run_isolated.py discover -s tests -v

precommit:
	pre-commit run --all-files

doctor:
	python3 scripts/controller.py doctor

clean:
	rm -rf __pycache__ scripts/__pycache__ tests/__pycache__
