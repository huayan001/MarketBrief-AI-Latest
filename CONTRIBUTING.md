# Contributing

Thanks for your interest in Quant AI Workbench.

## Local Development

Install dependencies and configure `api_keys.env` (MySQL required):

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp api_keys.env.example api_keys.env
./run.sh
```

On Windows:

```bat
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
run.bat
```

## Pull Requests

- Keep changes focused and easy to review.
- Do not commit API keys, local databases, reports, or personal data.
- Run a syntax check before opening a PR:

```bash
python3 -m py_compile server.py db.py auth.py
```

## Financial Disclaimer

This project is for research and information analysis only. It does not provide investment advice, trading advice, or automated trading instructions.
