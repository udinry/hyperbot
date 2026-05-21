"""
Parse bot.log and print a summary of all completed round-trip trades.

Usage:
    python analyze_trades.py [logfile]          # default: bot.log
    python analyze_trades.py bot.log --scale 10 # show projected P&L at scale

A completed round-trip is any FILL with closedPnl != 0.0 (i.e., a closing fill).
Opening fills (closedPnl == 0.0) are tracked for entry price context.
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional


FILL_RE = re.compile(
    r"FILL \| oid=(\d+) side=(\w+) px=([\d.]+) sz=([\d.]+) closedPnl=([+-]?[\d.]+)"
)
SIGNAL_RE = re.compile(r"\|\s*(BUY|SELL)\s+signal\s*\|\s*OFI=([+-]?[\d.]+)(?:\s+TFI=([+-]?[\d.]+))?")


@dataclass
class Trade:
    direction: str       # BUY or SELL (entry direction)
    entry_px: float
    exit_px: float
    size: float
    closed_pnl: float
    ofi: Optional[float] = None
    tfi: Optional[float] = None


def parse_log(path: Path) -> List[Trade]:
    """Parse bot.log into completed round-trips.
    Partial fills (same oid, same closing event) are aggregated into one trade.
    A trade closes when the next opening fill (closedPnl==0) appears after
    at least one closing fill has been accumulated."""
    trades: List[Trade] = []
    last_signal: Optional[tuple] = None  # (direction, ofi, tfi)
    pending_entry: Optional[dict] = None  # {side, px, sz, ofi, tfi}
    current_close_pnl: float = 0.0
    current_close_px: float = 0.0
    current_close_sz: float = 0.0
    in_closing_event: bool = False

    def _flush_close():
        nonlocal current_close_pnl, current_close_px, current_close_sz, in_closing_event
        if in_closing_event and abs(current_close_pnl) > 1e-9:
            entry_px   = pending_entry["px"]   if pending_entry else current_close_px
            entry_side = pending_entry.get("side", "BUY") if pending_entry else "BUY"
            ofi_val    = pending_entry.get("ofi") if pending_entry else None
            tfi_val    = pending_entry.get("tfi") if pending_entry else None
            trades.append(Trade(
                direction=entry_side,
                entry_px=entry_px,
                exit_px=current_close_px,
                size=current_close_sz,
                closed_pnl=current_close_pnl,
                ofi=ofi_val,
                tfi=tfi_val,
            ))
        current_close_pnl = 0.0
        current_close_px  = 0.0
        current_close_sz  = 0.0
        in_closing_event  = False

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
                last_signal = (direction, ofi, tfi)
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
                    pending_entry["ofi"] = last_signal[1]
                    pending_entry["tfi"] = last_signal[2]
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
    print(f"  Win rate    : {wr:.1f}%  ({len(wins)}W / {len(losses)}L)")
    print(f"  Total P&L   : {total_pnl:+.4f}$  (×{scale:.0f} → {total_pnl * scale:+.2f}$)")
    print(f"  Avg win     : {avg_win:+.4f}$")
    print(f"  Avg loss    : {avg_loss:+.4f}$")
    print(f"  Edge/trade  : {edge:+.4f}$  (×{scale:.0f} → {edge * scale:+.4f}$)")
    if avg_loss != 0:
        print(f"  Win/loss R  : {abs(avg_win / avg_loss):.2f}:1")
    print()
    print(f"  Break-even WR needed: {abs(avg_loss) / (abs(avg_win) + abs(avg_loss)) * 100:.1f}%" if (avg_win and avg_loss) else "")
    print()
    print(f"  {'#':>3}  {'Dir':>4}  {'Entry':>10}  {'Exit':>10}  {'PnL':>9}  {'OFI':>7}  {'TFI':>7}")
    print(f"  {'-'*3}  {'-'*4}  {'-'*10}  {'-'*10}  {'-'*9}  {'-'*7}  {'-'*7}")
    for i, t in enumerate(trades, 1):
        ofi_s = f"{t.ofi:+.3f}" if t.ofi is not None else "   N/A"
        tfi_s = f"{t.tfi:+.3f}" if t.tfi is not None else "   N/A"
        flag  = " W" if t.closed_pnl > 0 else " L"
        print(f"  {i:>3}  {t.direction:>4}  {t.entry_px:>10.2f}  {t.exit_px:>10.2f}  {t.closed_pnl:>+9.4f}{flag}  {ofi_s}  {tfi_s}")
    print("=" * 60)


if __name__ == "__main__":
    log_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent / "bot.log"
    scale = 10.0
    from_trade = 1
    for i, arg in enumerate(sys.argv):
        if arg == "--scale" and i + 1 < len(sys.argv):
            scale = float(sys.argv[i + 1])
        if arg == "--from-trade" and i + 1 < len(sys.argv):
            from_trade = int(sys.argv[i + 1])

    if not log_path.exists():
        print(f"Log file not found: {log_path}")
        sys.exit(1)

    trades = parse_log(log_path)
    if from_trade > 1:
        trades = trades[from_trade - 1:]
    print_report(trades, scale=scale)
