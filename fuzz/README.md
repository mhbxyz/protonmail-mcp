# Fuzzing

Coverage-guided fuzz targets built with [Atheris](https://github.com/google/atheris).
They exercise the code that handles attacker-controlled input: raw RFC 822 messages,
HTML bodies, recipient headers and attachment filenames.

| Target | Input | Properties checked |
|---|---|---|
| `fuzz_message.py` | Raw message bytes | Parsing, thread headers, attachments and send preparation never crash |
| `fuzz_html.py` | HTML / text bodies | Rendering, link extraction and quote stripping never crash |
| `fuzz_headers.py` | Recipient and filename strings | No CR/LF in validated headers; sandbox filenames never contain path separators, `..`, or control characters |

Run locally (Linux x86_64, Python 3.13+):

```bash
uv sync --group fuzz
uv run python fuzz/fuzz_message.py -max_total_time=60
```

CI runs every target for a short time on pull requests and for longer every week
(`.github/workflows/fuzz.yml`); crashing inputs are uploaded as workflow artifacts.
Hypothesis property tests in `tests/test_fuzz.py` cover the same surfaces in the regular
test suite.
