# Implementation notes

The slice uses a trusted CLI, bounded routing, structured evidence retrieval and a mandatory tool gateway. Scope, monetary/date calculations, confirmation and writes remain deterministic. SQLite persists budgets, leases, approval digests, canonical grievance bodies, operations and minimised audit events. Providers are configurable; recorded integration evidence uses rules mode. OpenAI Responses, Ollama and compatible adapters have contract coverage, but no live LLM is yet validated.

The supplied backend accepts valid tokens without matching tenant to URL. The gateway constructs paths from trusted scope and checks member ownership. A plan document declares a different tenant from its folder and is quarantined. Vendor metadata requests unnecessary identity disclosure, and KB prose attempts to influence code. Neither controls application policy.

Three expectations conflict with validated evidence: EV-23 uses another tenant's virtual-care benefit; EV-25 combines a future deductible with current accumulators; EV-37 expects another product's prescription benefit. The unmodified pack scores 37/40. Raw failures remain visible and the 95% quality gate fails; observed safety checks pass.

A grievance can commit before a 503 acknowledgement. Retries retain the exact body and operation key across application restart. The guarantee depends on upstream idempotency retention; restarting the supplied mock loses its state. A scoped 30-second projected-claims cache improves repeated reads, with visible age and an option to disable reuse. Fresh complete pagination still exceeds the three-second target in some cases.

With another day, I would validate a real model, test concurrent load and recovery, implement managed identity/state/audit adapters, and measure full cost. Production availability, six-year immutable archives and 250-rep capacity remain design targets. I would seek upstream aggregate claim queries to meet the cold-count latency requirement.

AI tools assisted with implementation, tests and documentation. Their suggestions required review: an initial fact scorer mishandled sentence punctuation and was corrected. Git history begins with the first published code snapshot because earlier development stayed local; later commits record actual changes. No earlier history has been reconstructed or backdated. The invitation's receipt date and submission deadline must be checked separately.
