# LLM-call observability

Redline records calls to the Anthropic and OpenAI SDKs as `llm_call` spans, next to the SQL
and HTTP spans of the same request, and flags patterns that waste time or money.

**Privacy.** Only metadata is stored: provider, model, token counts, status, whether the call
streamed, a 16-character hash of the prompt and its length. Prompt text, completions, tool
arguments and exception messages are never recorded.

## Turn it on

```python
from redline_core.contrib.anthropic import instrument_anthropic
from redline_core.contrib.openai import instrument_openai

instrument_anthropic()   # Messages.create and AsyncMessages.create
instrument_openai()      # chat.completions.create, sync and async
```

Both return `False` when the SDK is not installed, are idempotent, and leave the SDK's
behaviour and exceptions unchanged. Calls made outside a traced request cost one context-variable
read. `redline instrumentation` lists them under integrations.

**Streaming.** A streamed call is recorded with `stream: true` and no token counts, because
usage is only known when the stream ends. Totals therefore undercount when you stream.

## Findings

| Kind | Fires when | Default |
|---|---|---|
| `duplicate_llm_call` | The same model got an identical prompt (same hash) repeatedly in one request | 2 calls |
| `serial_llm_calls` | Calls ran back to back with no overlap | 3 calls |
| `llm_in_transaction` | A call started while a database transaction was open | always |
| `llm_call_loop` | One request made many calls (agent loops, per-item calls) | 8 calls |
| `uncached_large_prompt` | A large non-streamed Anthropic prompt used no prompt-cache tokens | 4000 characters |

Settings: `llm_duplicate_threshold`, `llm_serial_min`, `llm_loop_calls`,
`llm_prompt_cache_chars`. A value of `0` disables that detector.

Limits worth knowing:

- `serial_llm_calls` is a candidate, not a verdict. A trace shows timing, not whether a later
  prompt needs an earlier answer.
- `duplicate_llm_call` compares hashes of the prompt-bearing arguments (system, messages,
  tools, input). Model output may legitimately differ between identical prompts.
- `uncached_large_prompt` only looks at Anthropic, whose caching is opt-in. OpenAI caches
  automatically.
- Hashes are computed over at most the first two million characters of the arguments.

## Usage summary

```text
GET api/llm?window=24h
```

Returns sampled calls, errors, streamed calls, `input_tokens`, `output_tokens`,
`cache_read_tokens` and `cache_creation_tokens` per route and model, with totals. Agents get
the same data from the `llm_summary` MCP tool. Prices are not included because they change;
multiply the token counts by your own rates. Only the newest 300 traces in the window are
scanned and counts are labelled sampled.

```python
PRICE_PER_MTOK = {"claude-x": (3.0, 15.0)}          # your own numbers: (input, output)
cost = sum(PRICE_PER_MTOK[r["model"]][0] * r["input_tokens"] / 1e6
           + PRICE_PER_MTOK[r["model"]][1] * r["output_tokens"] / 1e6
           for r in data["items"] if r["model"] in PRICE_PER_MTOK)
```
