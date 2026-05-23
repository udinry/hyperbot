"""
Parse bot.log and print a summary of all completed round-trip trades.

Usage:
    python analyze_trades.py [logfile]          # default: bot.log
    python analyze_trades.py bot.log --scale 10 # show projected P&L at scale

A completed round-trip is any FILL with closedPnl != 0.0 (i.e., a closing fill).
Opening fills (closedPnl == 0.0) are tracked for entry price context.
"""
from __future__ import annotations

import math
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional


def wilson_ci(wins: int, n: int, z: float = 1.645) -> tuple:
    """Wilson score confidence interval for a proportion. Default z=1.645 → 90% CI."""
    if n == 0:
        return (0.0, 1.0)
    p = wins / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


FILL_RE = re.compile(
    r"FILL \| oid=(\d+) side=(\w+) px=([\d.]+) sz=([\d.]+) closedPnl=([+-]?[\d.]+)"
)
SIGNAL_RE = re.compile(
    r"\|\s*(BUY|SELL)\s+signal\s*\|\s*OFI=([+-]?[\d.]+)"
    r"(?:\s+TFI=([+-]?[\d.]+))?"
    r"(?:\s+QI=([\d.]+))?"
    r"(?:\s+VWAP=([+-]?[\d.]+)\$)?"
)


@dataclass
class Trade:
    direction: str       # BUY or SELL (entry direction)
    entry_px: float
    exit_px: float
    size: float
    closed_pnl: float
    ofi: Optional[float] = None
    tfi: Optional[float] = None
    qi: Optional[float] = None
    vwap: Optional[float] = None


def parse_log(paths) -> List[Trade]:
    """Parse one or more log files into completed round-trips.
    State is shared across files so a trade that opens in one log and
    closes in the next is correctly attributed.
    Partial fills (same oid, same closing event) are aggregated into one trade."""
    if isinstance(paths, Path):
        paths = [paths]

    trades: List[Trade] = []
    last_signal: Optional[tuple] = None  # (direction, ofi, tfi, qi, vwap)
    pending_entry: Optional[dict] = None  # {side, px, sz, ofi, tfi, qi, vwap}
    current_close_pnl: float = 0.0
    current_close_px: float = 0.0
    current_close_sz: float = 0.0
    in_closing_event: bool = False

    def _flush_close():
        nonlocal current_close_pnl, current_close_px, current_close_sz, in_closing_event
        if in_closing_event and abs(current_close_pnl) > 1e-9:
            entry_px   = pending_entry["px"]   if pending_entry else current_close_px
            entry_side = pending_entry.get("side", "BUY") if pending_entry else "BUY"
            ofi_val    = pending_entry.get("ofi")  if pending_entry else None
            tfi_val    = pending_entry.get("tfi")  if pending_entry else None
            qi_val     = pending_entry.get("qi")   if pending_entry else None
            vwap_val   = pending_entry.get("vwap") if pending_entry else None
            trades.append(Trade(
                direction=entry_side,
                entry_px=entry_px,
                exit_px=current_close_px,
                size=current_close_sz,
                closed_pnl=current_close_pnl,
                ofi=ofi_val,
                tfi=tfi_val,
                qi=qi_val,
                vwap=vwap_val,
            ))
        current_close_pnl = 0.0
        current_close_px  = 0.0
        current_close_sz  = 0.0
        in_closing_event  = False

    for path in paths:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                m = SIGNAL_RE.search(line)
                if m:
                    direction = m.group(1).strip()
                    try:
                        ofi = float(m.group(2))
                    except ValueError:
                        ofi = None
                    try:
                        tfi = float(m.group(3)) if m.group(3) is not None else None
                    except (ValueError, TypeError):
                        tfi = None
                    try:
                        qi = float(m.group(4)) if m.group(4) is not None else None
                    except (ValueError, TypeError):
                        qi = None
                    try:
                        vwap = float(m.group(5)) if m.group(5) is not None else None
                    except (ValueError, TypeError):
                        vwap = None
                    last_signal = (direction, ofi, tfi, qi, vwap)
                    continue

                m = FILL_RE.search(line)
                if not m:
                    continue

                oid, side, px_s, sz_s, pnl_s = m.groups()
                px  = float(px_s)
                sz  = float(sz_s)
                pnl = float(pnl_s)

                if abs(pnl) < 1e-9:
                    # Opening fill — first flush any accumulated closing event
                    _flush_close()
                    pending_entry = {"side": side, "px": px, "sz": sz}
                    if last_signal:
                        pending_entry["ofi"]  = last_signal[1]
                        pending_entry["tfi"]  = last_signal[2]
                        pending_entry["qi"]   = last_signal[3] if len(last_signal) > 3 else None
                        pending_entry["vwap"] = last_signal[4] if len(last_signal) > 4 else None
                else:
                    # Closing partial fill — accumulate
                    in_closing_event = True
                    current_close_pnl += pnl
                    current_close_px   = px    # last fill price as exit price
                    current_close_sz  += sz

    _flush_close()  # flush any final open closing event
    return trades


def print_report(trades: List[Trade], scale: float = 1.0) -> None:
    if not trades:
        print("No completed round-trip trades found in log.")
        return

    wins   = [t for t in trades if t.closed_pnl > 0]
    losses = [t for t in trades if t.closed_pnl <= 0]
    total_pnl = sum(t.closed_pnl for t in trades)
    wr = len(wins) / len(trades) * 100 if trades else 0

    avg_win  = sum(t.closed_pnl for t in wins)  / len(wins)  if wins   else 0
    avg_loss = sum(t.closed_pnl for t in losses) / len(losses) if losses else 0

    edge = (wr / 100 * avg_win) + ((1 - wr / 100) * avg_loss) if trades else 0

    print("=" * 60)
    print(f"  TRADE ANALYSIS  ({len(trades)} completed round-trips)")
    print("=" * 60)
    be_wr = abs(avg_loss) / (abs(avg_win) + abs(avg_loss)) * 100 if (avg_win and avg_loss) else 0
    ci_lo, ci_hi = wilson_ci(len(wins), len(trades))

    print(f"  Win rate    : {wr:.1f}%  ({len(wins)}W / {len(losses)}L)")
    print(f"  90% CI WR   : [{ci_lo*100:.1f}%, {ci_hi*100:.1f}%]  (need >{be_wr:.1f}% to profit)")
    edge_verdict = "EDGE" if ci_lo * 100 > be_wr else ("no edge" if ci_hi * 100 < be_wr else "inconclusive")
    print(f"  Verdict     : {edge_verdict}  (n={len(trades)}, need ~25+ for significance)")
    print(f"  Total P&L   : {total_pnl:+.4f}$  (×{scale:.0f} → {total_pnl * scale:+.2f}$)")
    print(f"  Avg win     : {avg_win:+.4f}$")
    print(f"  Avg loss    : {avg_loss:+.4f}$")
    print(f"  Edge/trade  : {edge:+.4f}$  (×{scale:.0f} → {edge * scale:+.4f}$)")
    if avg_loss != 0:
        print(f"  Win/loss R  : {abs(avg_win / avg_loss):.2f}:1")
    print()
    print(f"  Break-even WR needed: {be_wr:.1f}%" if be_wr else "")
    print()
    print(f"  {'#':>3}  {'Dir':>4}  {'Entry':>10}  {'Exit':>10}  {'PnL':>9}  {'OFI':>7}  {'TFI':>7}  {'QI':>6}  {'VWAP':>7}")
    print(f"  {'-'*3}  {'-'*4}  {'-'*10}  {'-'*10}  {'-'*9}  {'-'*7}  {'-'*7}  {'-'*6}  {'-'*7}")
    for i, t in enumerate(trades, 1):
        ofi_s  = f"{t.ofi:+.3f}"  if t.ofi  is not None else "   N/A"
        tfi_s  = f"{t.tfi:+.3f}"  if t.tfi  is not None else "   N/A"
        qi_s   = f"{t.qi:.3f}"    if t.qi   is not None else "   N/A"
        vwap_s = f"{t.vwap:+.1f}" if t.vwap is not None else "   N/A"
        flag   = " W" if t.closed_pnl > 0 else " L"
        print(f"  {i:>3}  {t.direction:>4}  {t.entry_px:>10.2f}  {t.exit_px:>10.2f}  {t.closed_pnl:>+9.4f}{flag}  {ofi_s}  {tfi_s}  {qi_s}  {vwap_s}")
    print("=" * 60)


if __name__ == "__main__":
    args = sys.argv[1:]
    scale = 10.0
    from_trade = 1
    positional: list[str] = []
    i = 0
    while i < len(args):
        if args[i] == "--scale" and i + 1 < len(args):
            scale = float(args[i + 1]); i += 2
        elif args[i] == "--from-trade" and i + 1 < len(args):
            from_trade = int(args[i + 1]); i += 2
        else:
            positional.append(args[i]); i += 1

    if positional:
        log_paths = [Path(positional[0])]
    else:
        base = Path(__file__).parent / "bot.log"
        older = base.with_suffix(".log.1")
        log_paths = ([older] if older.exists() else []) + [base]

    for lp in log_paths:
        if not lp.exists():
            print(f"Log file not found: {lp}")
            sys.exit(1)

    trades = parse_log(log_paths)
    if from_trade > 1:
        trades = trades[from_trade - 1:]
    print_report(trades, scale=scale)
