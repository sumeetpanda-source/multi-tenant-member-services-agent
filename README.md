# Multi tenant member services agent

A Python CLI for representatives answering benefit and claim questions for one health plan and member per conversation. Every tool passes through a gateway enforcing tenant scope, minimum necessary data, confirmation before writes, and conversation budgets. Grievances use a durable operation ledger and stable idempotency keys.

## Implementation and model status

The recorded development and integration runs use the deterministic `rules` router. **No live LLM has been evaluated yet.** OpenAI Responses, Ollama and compatible Chat Completions adapters have synthetic contract coverage. The OpenAI and Ollama configurations below are reproducible setup examples, not claims of measured model quality.

The model classifies minimised routing features. Application code controls evidence lookup, arithmetic, authorisation, approval and writes. Model requests contain a fixed service vocabulary and reference flags; raw questions, identifiers, member records, clinical notes and transcripts do not cross the model boundary.

## Requirements

- Python 3.10 or later and Git. Use `python3` if that is your Python command.
- The original assignment pack, provided separately by the interviewer. It is deliberately absent from this repository.
- For a live model: an accessible provider endpoint and model; paid providers also need credentials and configured pricing.

The application and tests use the Python standard library. No API SDK or application dependency installation is required. Run all following application commands from the cloned repository root.

## Clone and configure the external pack

```sh
git clone https://github.com/sumeetpanda-source/multi-tenant-member-services-agent.git
cd multi-tenant-member-services-agent
python --version
```

Create a private configuration file, preserving any existing file.

PowerShell:

```powershell
if (-not (Test-Path -LiteralPath .env)) { Copy-Item .env.example .env }
```

macOS or Linux:

```sh
test -f .env || cp .env.example .env
```

Edit `.env` and set an absolute `PACK_DIR`, for example:

```dotenv
PACK_DIR=C:/path/to/assignment-pack
MEMBER_API_URL=http://127.0.0.1:8090
MODEL_PROVIDER=rules
```

On macOS or Linux, use a path such as `/path/to/assignment-pack`. The pack root must contain `mock_services/server.py`, `mock_services/data/tenants.json`, `kb/`, and `eval/conversations.json`. Keep that folder separate from the clone. Existing local installations with the pack in the project root can retain `PACK_DIR=.`.

`--pack-dir` overrides `PACK_DIR`. `--env-file` selects a configuration file; otherwise `.env` is read when present. Process environment variables override file values. Values are parsed as data, without executing shell expressions.

## Start the mock and run the agent

In terminal 1, run the mock from your external pack. Replace the path with your actual pack location:

```sh
python "C:/path/to/assignment-pack/mock_services/server.py" --port 8090
```

On macOS or Linux, substitute the equivalent absolute path. The mock listens at `http://127.0.0.1:8090`; adjust `MEMBER_API_URL` if you choose another port.

In terminal 2, from this repository:

```sh
python -m member_agent --demo-credentials chat --tenant plan_a --member MEMBER_ID
```

Replace `MEMBER_ID` with an identifier from your private pack. Ask a benefit or claim question, then continue in the same console. `/exit` closes it. The console prints the session ID, model mode and citations.

`--demo-credentials` reads only mock configuration credentials from the external pack. Member and operational records are read through HTTP. For another backend, omit that flag and set `TENANT_TOKENS_JSON` and `DIRECTORY_API_KEY` privately. The CLI is a trusted local operator interface; production authentication and member authorisation belong ahead of the session boundary.

Single turns and durable continuation:

```sh
python -m member_agent --demo-credentials ask --tenant plan_a --member MEMBER_ID --message "What does a specialist visit cost?" --json
python -m member_agent --demo-credentials ask --tenant plan_a --member MEMBER_ID --session SESSION_ID --message "File a billing grievance about an incorrect bill" --json
python -m member_agent --demo-credentials ask --tenant plan_a --member MEMBER_ID --session SESSION_ID --message "confirm" --json
```

Use the session ID returned by the first command. A filing request creates a draft and displays its summary. Read it back to the member; use `confirm` after agreement or `cancel` to discard the draft. Retain the same scope, session and state database. Repeated confirmation reconciles the original operation. For sensitive questions, omit `--message` and supply standard input instead of placing text in shell history.

Inspect scoped evidence and operations:

```sh
python -m member_agent --demo-credentials trace --tenant plan_a --member MEMBER_ID --session SESSION_ID
python -m member_agent --demo-credentials operation --tenant plan_a --member MEMBER_ID --session SESSION_ID
python -m member_agent --demo-credentials check-kb --tenant plan_a
```

## Choose a model

Provider selection changes routing, never gateway permissions. A replacement model must support the adapter's JSON-schema output contract; arbitrary models and endpoints are not automatically compatible. Start a new conversation after switching providers to obtain separate budget and trace evidence.

### OpenAI Responses API

1. Sign in to the [OpenAI API platform](https://platform.openai.com/).
2. Select a project and create your own development key on the [API keys page](https://platform.openai.com/api-keys). See the [official key guidance](https://developers.openai.com/api/docs/guides/api-keys).
3. Review [API billing and credits](https://platform.openai.com/account/billing/overview) and ensure the project can access your chosen model.
4. Put the key in your local `.env`, never in source control, logs, screenshots or a shared chat.

Example `.env` values for a small routing workload:

```dotenv
MODEL_PROVIDER=openai
MODEL_NAME=gpt-6-luna
MODEL_ENDPOINT=https://api.openai.com/v1/responses
OPENAI_API_KEY=replace-with-your-local-secret
MODEL_API_KEY=
MODEL_REASONING_EFFORT=none
MODEL_INPUT_USD_PER_MILLION=0.125
MODEL_OUTPUT_USD_PER_MILLION=0.50
```

Keep your real `PACK_DIR` and other limits from `.env.example`. Do not replace an existing key with the placeholder above. `MODEL_API_KEY`, if set, takes precedence over `OPENAI_API_KEY`.

The [GPT-6 Luna model page](https://developers.openai.com/api/docs/models/gpt-6-luna) lists Responses and structured-output support. At the documentation check on 11 November 2026, standard short-context input/output rates were USD 0.10/0.50 per million tokens, with a 1.25x cache-write rate. The example uses a conservative USD 0.125 input rate. Application cost figures are estimates, not billing statements; update rates for the model, processing mode and region you actually use. Account access and pricing can change.

`MODEL_REASONING_EFFORT=none` avoids spending this router's 512-token output allowance on reasoning. This optional setting applies only to the native OpenAI adapter. Leave it blank for models that do not accept reasoning controls. The provider must support the chosen effort value. Incomplete or invalid model responses fail closed. Requests use `store=false`; contractual retention and healthcare-data requirements still need separate review.

Run the same console command after configuration. Once the mock is running, a single read smoke check is:

```sh
python -m member_agent --demo-credentials ask --tenant plan_a --member MEMBER_ID --message "What does a specialist visit cost?" --json
```

This makes a billable request when OpenAI is selected. A subsequent live evaluation must record its real model name, quality, cost and latency before being represented as submission evidence.

### Local Ollama

Install [Ollama](https://ollama.com/) separately. One available small-model example is [qwen2.5:3b](https://ollama.com/library/qwen2.5:3b); review its licence and hardware requirements for your use.

```sh
ollama pull qwen2.5:3b
ollama serve
```

If Ollama is already running, use that instance. Set:

```dotenv
MODEL_PROVIDER=ollama
MODEL_NAME=qwen2.5:3b
MODEL_ENDPOINT=http://127.0.0.1:11434/api/chat
MODEL_REASONING_EFFORT=
MODEL_API_KEY=
OPENAI_API_KEY=
MODEL_INPUT_USD_PER_MILLION=0
MODEL_OUTPUT_USD_PER_MILLION=0
```

The adapter sends the JSON schema through Ollama's [chat API](https://docs.ollama.com/api/chat) and validates output locally. Zero token pricing represents local inference, not zero electricity or infrastructure cost. Re-run evaluation for the selected model; this example has not been live-tested here.

### Compatible Chat Completions endpoint

Set `MODEL_PROVIDER=chat-completions`, `MODEL_NAME`, a full `MODEL_ENDPOINT` ending in the provider's chat-completions route, `MODEL_API_KEY`, and its positive input/output pricing rates. The endpoint must accept strict `response_format.json_schema`, `max_tokens`, `temperature=0`, and `store=false`, and return `usage.prompt_tokens` and `usage.completion_tokens`. Endpoints with different contracts need an adapter change. Remote endpoints require HTTPS; plain HTTP is allowed only for loopback.

### Deterministic local mode

Set `MODEL_PROVIDER=rules` to run without an LLM or model API charge. This is useful for reproducible checks. It does not satisfy the missing live-model evidence by itself.

## Tests and evaluation

Unit, privacy, isolation, budget and retry checks require no assignment pack or live model:

```sh
python -m evaluation.checks
```

Run the external conversations with the configured provider. The runner starts an isolated mock, uses temporary state, and stops its own mock afterward:

```sh
python -m evaluation --demo-credentials --start-mock --output reports/local.json --gate
```

Explicit alternate pack/configuration:

```sh
python -m evaluation --pack-dir "C:/path/to/assignment-pack" --env-file .env --demo-credentials --start-mock --output reports/local.json --gate
```

Live provider selection makes billable model calls. Reports exclude raw conversations, answers, member identifiers and secrets. Review the report before committing it. The agent cannot access the scorer's expected facts.

The regression gate requires at least 95% case quality and every observed safety invariant to pass. `--gate` returns nonzero on failure. Independent backend observations verify write effects between turns. GitHub CI runs independent fixtures on Python 3.10 and 3.12; the external pack must be provisioned privately for an integration CI job.

Recorded evidence:

| Check | Result |
| --- | --- |
| [Automated suite](reports/checks.json) | 86 tests pass |
| [Unmodified external pack](reports/latest.json) | 37/40 cases in rules mode; observed safety checks pass; quality gate fails |
| Pack turn latency | p95 4.625 seconds; below-three-second target not met |
| [Direct-claim cache benchmark](reports/performance.json) | 5 samples per configuration; follow-up median 1.782 to 0.281 seconds |
| Live LLM, cloud cost and 250-rep load | Not evaluated |

Three supplied expectations conflict with validated tenant/product/year evidence: EV-23 imports another tenant's virtual-care terms, EV-25 combines a renewal-year deductible with current accumulators, and EV-37 expects another product's prescription benefit. Original files and raw failures are retained. No case is exempted to turn the gate green.

To repeat the small latency benchmark, select rules mode in the local configuration and run:

```sh
python -m evaluation.performance --demo-credentials --samples 5
```

This measures one sequential direct-claim scenario; it does not certify cold counts or peak scale.

## Budgets and operational limits

`.env.example` lists step, token, model-cost, active-time, turn and approval limits. Budgets persist across turns; human waiting time is excluded from active latency. Model cost is reserved before a call, and an ambiguous failure retains its conservative reservation. An approval binds tenant, member, session, operation and canonical body digest.

`CLAIMS_CACHE_TTL_SECONDS` defaults to 30 and accepts 0-60 seconds; 0 forces fresh reads. Cache keys are isolated by conversation and verified scope, contain only projected claim fields, and disclose data age. Partial pagination is never cached as a complete count. Date queries read the complete target day; full counts follow every page.

Cold complete counts can exceed three seconds because the supplied API takes 1.5 seconds per sequential page and throttles requests. Exactly-once effect requires retained downstream idempotency state; restarting the supplied in-memory mock discards that state. The cloud design covers managed identity, immutable six-year audit storage and deployment targets; those services are not provisioned by this CLI.

## Submission documents and privacy

- [Design brief](design/DESIGN.md): production deployment, second-cloud mapping, threats and five ADRs.
- [Four-page design PDF plus one diagram page](design/Design_Brief.pdf), with its [editable source](design/Design_Brief.docx) and [architecture SVG](design/architecture.svg).
- [NOTES.md](NOTES.md): assumptions, source conflicts, remaining work and assistance disclosure.

The original brief, supplied data, mock implementation, plan documents, vendor manifest and evaluation corpus stay outside Git. `.env`, member state, transcripts, detailed working documents and private analysis are ignored. Keep operational state on an access-controlled encrypted volume. The sanitized reports and independently authored test fixtures are committed.

Git history starts with the first published code snapshot. Earlier local development was not committed; no history has been backdated or reconstructed. Later commits record actual changes. The submission requires the public repository URL and final commit hash; check the deadline in the original invitation before sending.
