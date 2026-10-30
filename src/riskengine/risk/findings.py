import numpy as np
import pandas as pd

from riskengine.db.portfolios import get_portfolio_weights
from riskengine.risk.metrics import portfolio_hhi
from riskengine.risk.portfolio_report import get_portfolio_risk_report


def generate_risk_findings(
    connection,
    portfolio_id: int,
    start_date: str,
    end_date: str,
    benchmark_ticker: str = "SPY",
    confidence: float = 0.95,
    trading_days: int = 252,
    max_age_days: int = 0,
) -> dict:
    """
    Generate interpretable portfolio risk findings.

    Concentration uses invested holdings only, excluding cash.
    Historical risk statistics use cash-flow-aware portfolio returns.
    """

    risk = get_portfolio_risk_report(
        connection=connection,
        portfolio_id=portfolio_id,
        start_date=start_date,
        end_date=end_date,
        benchmark_ticker=benchmark_ticker,
        confidence=confidence,
        trading_days=trading_days,
        max_age_days=max_age_days,
    )

    holdings = get_portfolio_weights(
        connection,
        portfolio_id,
        end_date,
        max_age_days=max_age_days,
    )

    findings = []

    concentration = {
        "holdings_count": 0,
        "hhi": None,
        "effective_holdings": None,
        "largest_holding_weight": None,
    }

    if not holdings.empty:
        weights = holdings["weight"].astype(float)

        hhi = portfolio_hhi(weights)

        concentration = {
            "holdings_count": len(weights),
            "hhi": hhi,
            "effective_holdings": 1.0 / hhi,
            "largest_holding_weight": float(weights.max()),
        }

        findings.append({
            "category": "concentration",
            "metric": "hhi",
            "value": hhi,
            "headline": "Invested-holdings concentration",
            "interpretation": (
                f"The portfolio holds {len(weights)} invested positions. "
                f"Its HHI is {hhi:.3f}, equivalent to approximately "
                f"{1.0 / hhi:.2f} equally weighted holdings."
            ),
            "investigation": (
                "Review the largest position weights and whether "
                "multiple holdings share similar underlying exposures."
            ),
        })

    beta = risk["beta"]

    if beta is not None:
        if not np.isfinite(beta):
            raise ValueError("Portfolio beta must be finite")

        findings.append({
            "category": "market_sensitivity",
            "metric": "beta",
            "value": beta,
            "headline": "Sensitivity to benchmark movements",
            "interpretation": (
                f"The portfolio's historical beta against "
                f"{benchmark_ticker} is {beta:.2f}. "
                "This describes its estimated sensitivity to "
                "benchmark price-return movements over the "
                "observed period, not a forecast."
            ),
            "investigation": (
                "Examine whether benchmark sensitivity is stable "
                "across different market periods."
            ),
        })

    volatility = risk["annualized_volatility"]

    findings.append({
        "category": "volatility",
        "metric": "annualized_volatility",
        "value": volatility,
        "headline": "Annualized portfolio volatility",
        "interpretation": (
            f"Annualized historical volatility is "
            f"{volatility:.2%}, calculated from "
            f"{risk['observations']} portfolio return observations."
        ),
        "investigation": (
            "Compare volatility across periods and investigate "
            "which holdings contribute most to return variability."
        ),
    })

    drawdown = risk["maximum_drawdown"]

    findings.append({
        "category": "drawdown",
        "metric": "maximum_drawdown",
        "value": drawdown,
        "headline": "Largest observed peak-to-trough decline",
        "interpretation": (
            f"The maximum observed drawdown was {abs(drawdown):.2%} "
            "over the measurement period."
        ),
        "investigation": (
            "Identify the dates and securities associated with "
            "the largest portfolio decline."
        ),
    })

    var = risk["historical_var"]
    cvar = risk["historical_cvar"]

    findings.append({
        "category": "tail_risk",
        "metric": "historical_var",
        "value": var,
        "headline": "Historical downside risk",
        "interpretation": (
            f"At {confidence:.0%} confidence, historical VaR is "
            f"{var:.2%} and historical CVaR is {cvar:.2%}, "
            "expressed as fractions of portfolio value. "
            "These are historical estimates, not guaranteed "
            "limits on future losses."
        ),
        "investigation": (
            "Review the worst observed return days and whether "
            "the sample contains enough adverse market conditions."
        ),
    })

    return {
        "portfolio_id": portfolio_id,
        "as_of_date": end_date,
        "benchmark": benchmark_ticker,
        "risk_metrics": risk,
        "concentration": concentration,
        "findings": findings,
    }