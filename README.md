# Self-Looping Output Producer

A tiny Claude Code-style agent: terminal chat + tool loop, using the Gemini API for now.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export GEMINI_API_KEY="your-key-here"
# optional: pick another model
# export GEMINI_MODEL="models/gemini-3.8-flash"
python main.py
```

Run it from the folder of the project you want it to look at; the tools are
restricted to that folder.

## Layout

- `main.py`  - chat loop and the agent loop (model -> tools -> model ...)
- `llm.py`   - provider adapter (Gemini). Add other providers here.
- `tools.py` - tools the model can call (read_file, list_files)

## Try

- `what files are in this project?`
- `read main.py and explain how the loop stops`
