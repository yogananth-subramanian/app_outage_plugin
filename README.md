# Chaos Engineering App outage Plugin for Arcaflow

This plugin implements the App outage scenario used by [redhat-chaos/krkn](https://github.com/redhat-chaos/krkn)
for Chaos Engineering experiments on Kubernetes.

## Testing

The code requires Python >= 3.9 in order to work.

```console
python -m venv .venv
source .venv/bin/activate
pip install poetry
poetry install
poetry run python -m coverage run -a -m unittest discover -s tests -v
poetry run python -m coverage html
```
