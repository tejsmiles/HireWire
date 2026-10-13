.PHONY: install demo test serve mcp clean
install:
	pip install -e ".[all]"
demo:
	onboard-sync replay all
test:
	pytest
serve:
	onboard-sync serve --port 8080
mcp:
	onboard-sync mcp
clean:
	rm -rf var .pytest_cache build *.egg-info src/*.egg-info
