.PHONY: install run eval logs update-model clean build-sandbox memory-stats update-packages

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

memory-stats:
	@python -c "from autodev.memory import Memory, MemoryConfig; from autodev.config import load_config; c = load_config(); m = Memory(MemoryConfig(**c.memory.model_dump())); s = m.get_stats(); print(f'Memory: {\"ON\" if s[\"available\"] else \"OFF\"}'); print(f'Solutions: {s[\"total_solutions\"]}'); print(f'Lessons: {s[\"total_lessons\"]}'); print(f'Success rate: {s[\"success_rate\"]}%'); print(f'Avg iterations: {s[\"avg_iterations\"]}')"

update-packages:
	@python -c "import sys; sys.path.insert(0, 'src'); from autodev.package_manager import AutonomousPackageManager; from autodev.config import load_config; c = load_config(); m = AutonomousPackageManager(c); print('Package manager initialized')"

clean:
	rm -rf workspace/* logs/* eval/results/*
	@echo "Cleaned workspace, logs, and eval results."
