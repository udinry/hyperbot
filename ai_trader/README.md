# ai_trader — Claude-operated systematic trading

An AI **operator** on top of a **validated strategy**, bounded by a
**deterministic risk engine**. You talk to it (`status` / `trade` / `loop`);
it reads the proven trend signal, reasons about execution in plain English,
and places orders only within hard limits it cannot override.

```
You ──"status" / "trade" / "stop"──► agent.py (Claude, the operator)
                                         │
        get_strategy_signal ◄────────────┤  the EDGE (walk-forward validated;
        get_account / get_market ─────────┤  the AI does not predict markets)
        place_order ─────────────────────►│  risk_engine.py  ── HARD LIMITS
        halt_trading ────────────────────►│  (size/exposure/leverage/daily-loss
                                           ▼   /order-count — AI cannot bypass)
                                     Hyperliquid (dry-run by default)
```

## Why it's built this way (read this)

An LLM cannot backtest its own market opinions, so letting it predict prices is
how you lose money with confidence. Here the **edge is the validated trend
ensemble** (`hft_bot/STRATEGY_V2.md`: +21.2% CAGR / 22.2% MaxDD OOS 2022–2026 vs +5.9%/67%
buy-and-hold, regime-filtered, frozen-parameter-confirmed on ETH/SOL). The AI's job is the part
it's actually good at: reading state, applying judgment about *when/whether* to
rebalance, explaining decisions auditably, and stopping when something looks
wrong, and reading market news for SAFETY and CONTEXT (an exchange hack or
depeg triggers the kill switch — but news never sets the position; the signal
does). Every order passes through `risk_engine.py` — plain Python limits that
run after the model and before the exchange. No prompt or hallucination gets
past them.

## Usage

```bash
cd ai_trader
python -m pytest tests/                  # 21 tests (risk engine + full loop)

python agent.py status                   # signals + positions, never trades
python agent.py trade                    # one cycle, DRY-RUN (no real orders)
python agent.py trade --live             # one cycle, REAL orders (keys + opt-in)
python agent.py loop --interval 86400    # recurring daily cycles
```

Without `ANTHROPIC_API_KEY`, `status` still works (deterministic signal/account
readout) so you can run the strategy brain with zero AI spend. With the key, the
same command is narrated and reasoned by Claude.

## Configuration (env, conservative defaults for ~$160)

| Var | Default | Meaning |
|---|---|---|
| `ANTHROPIC_API_KEY` | — | enables the Claude operator |
| `AI_TRADER_MODEL` | `claude-fable-5` | decision model |
| `AI_TRADER_COINS` | `BTC` | comma list, e.g. `BTC,ETH,SOL` |
| `AI_MAX_POSITION_USD` | 200 | per-coin notional cap |
| `AI_MAX_TOTAL_EXPOSURE_USD` | 200 | gross notional cap |
| `AI_MAX_ORDER_USD` | 120 | single-order cap |
| `AI_MAX_DAILY_LOSS_USD` | 10 | halt for the UTC day past this realized loss |
| `AI_MAX_LEVERAGE` | 1.5 | equity-multiple cap |
| `AI_MAX_ORDERS_PER_DAY` | 6 | runaway-loop throttle |
| `PRIVATE_KEY` / `ACCOUNT_ADDRESS` / `HYPERLIQUID_API_URL` | — | Hyperliquid |

## Safety model

1. **Dry-run by default.** Real orders need a key *and* `--live`.
2. **Hard limits in code**, not prompts — size, exposure, leverage, daily loss,
   order count, allowed-coins, reduce-only correctness. Unit-tested.
3. **The AI cannot invert the strategy sign** (flat in downtrends stays flat).
4. **Kill switch** (`halt_trading`) the model can pull, plus an operator halt.
5. **Full audit trail** to `audit_log.jsonl`: every proposed order, the risk
   decision, rationale, and outcome.
6. Start at minimum size on a small account; scale only after the live audit
   trail matches expectations.

## Honest expectations

This does not change the edge — it operates the same validated strategy with a
conversational interface and a safety cage. Expect ~15–21%/yr with 20%+ drawdowns
and losing months (see `hft_bot/STRATEGY_V2.md` bootstrap). The AI adds
usability, auditability, and judgment — not extra alpha. Run **one** bot per
account (this or `trend_bot.py`, not both).
