"""TradingAgents-style roles (idea only, no dependency): Technical Analyst, Market Analyst, Risk Analyst
produce deterministic reports; Gemini is the final Trader / decision maker. Gemini NEVER invents levels."""
from __future__ import annotations


def technical_report(ctx) -> str:
    l = ctx.tf["5m"].last
    l15 = ctx.tf["15m"].last
    return (f"RSI5m={l['rsi']:.0f} RSI15m={l15['rsi']:.0f} MACDhist5m={l['macd_hist']:.2f} "
            f"ATR5m={ctx.tf['5m'].atr:.2f} ATR15m={ctx.atr:.2f} VWAP={'Above' if l['close'] > l['vwap'] else 'Below'} "
            f"EMA20/50(5m)={'bull' if l['ema_20'] > l['ema_50'] else 'bear'} ER15m={l15['er']:.2f}")


def market_report(ctx, res) -> str:
    s5 = ctx.tf["5m"]
    lb = s5.last_break
    sweeps = [f"{s.expected}-sweep({s.pool_kind}@{s.level:.2f},{'confirmed' if s.confirmed else 'unconfirmed'})"
              for s in (s5.sweeps + ctx.tf["15m"].sweeps)[:3]]
    return (f"Structure5m={s5.trend}/{lb.kind + ' ' + lb.direction if lb else '-'}; Liquidity={'; '.join(sweeps) or 'none'}; "
            f"Session={ctx.session['label']}; SessionBreakout={ctx.session.get('breakout') or '-'}; News={ctx.news.state}")


def risk_report(ctx, res, risk_level: str) -> str:
    p = res.plan
    return (f"Entry={p.entry:.2f} SL={p.sl:.2f} TP1={p.tp1:.2f} TP2={p.tp2:.2f} Risk={p.risk:.2f} "
            f"RR1={p.rr1:.2f} RR2={p.rr2:.2f} Spread={ctx.spread:.2f} RiskLevel={risk_level}")


def build_card(symbol, ctx, res, confluence_score, prob, risk_level) -> str:
    t = ctx.mtf["labels"]
    s5 = ctx.tf["5m"]
    lb = s5.last_break
    sweep = next((s for s in s5.sweeps + ctx.tf["15m"].sweeps if s.expected == res.setup_tags.get("direction")), None)
    return "\n".join([
        f"Symbol: {symbol}", f"Price: {ctx.mid:.2f}", "",
        f"Trend 4H: {t['4H']}", f"Trend 1H: {t['1H']}", f"Trend 15M: {t['15m']}", f"Trend 5M: {t['5m']}", f"Trend 1M: {t['1m']}", "",
        f"Structure: {lb.kind if lb else 'none'}", f"Liquidity: {(sweep.expected + '-side sweep (' + sweep.pool_kind + ')') if sweep else 'none'}",
        f"Session: {ctx.session['label']}", "",
        "[Technical Analyst] " + technical_report(ctx),
        "[Market Analyst] " + market_report(ctx, res),
        "[Risk Analyst] " + risk_report(ctx, res, risk_level), "",
        f"Strategy: {res.name} (strategy score {res.score:.0f}/100)",
        f"Candidate: {res.signal}", f"Confluence score: {confluence_score:.0f}/100 (NOT a probability)",
        f"Historical Similar Setups: {prob.n if prob else 0}",
        f"Historical Win Rate: {('%.1f%%' % (prob.win_rate * 100)) if (prob and prob.sufficient) else 'insufficient sample'}",
        f"Risk: {risk_level}",
    ])
