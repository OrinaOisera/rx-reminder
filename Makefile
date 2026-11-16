.PHONY: build deploy test smoke logs clean

build:
	sam build

deploy: build
	sam deploy --guided

test:
	PYTHONPATH=src python -m pytest -q

smoke:
	bash scripts/smoke_test.sh

logs:
	sam logs --stack-name rx-reminder --tail

clean:
	rm -rf .aws-sam
