.PHONY: install run eval logs update-model clean build-sandbox

install:
	pip install -e ".[dev]"

run:
	python -m autodev.main

eval:
	python eval/run_eval.py

logs:
	@echo "=== Latest session log ==="
	@ls -t logs/*.jsonl 2>/dev/null | head -1 | xargs -I{} sh -c 'echo "File: {}"; echo "---"; cat "{}" | python -m json.tool --no-ensure-ascii 2>/dev/null || cat "{}"' || echo "No logs found in logs/"

update-model:
	@if [ -z "$(MODEL)" ]; then \
		echo "Usage: make update-model MODEL=<model-name>"; \
		echo "Example: make update-model MODEL=qwen2.5-coder:14b"; \
		exit 1; \
	fi
	bash scripts/update_model.sh $(MODEL)

build-sandbox:
	docker build -t autodev-sandbox:latest ./docker/

clean:
	rm -rf workspace/* logs/* eval/results/*
	@echo "Cleaned workspace, logs, and eval results."
