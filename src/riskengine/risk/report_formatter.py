from datetime import datetime, timezone


def format_risk_report(findings_result: dict) -> str:
    """
    Convert structured risk findings into a readable Markdown report.
    """

    risk = findings_result["risk_metrics"]
    concentration = findings_result["concentration"]
    findings = findings_result["findings"]

    lines = [
        "# Portfolio Risk Intelligence Report",
        "",
        f"**Portfolio ID:** {findings_result['portfolio_id']}",
        f"**As of:** {findings_result['as_of_date']}",
        f"**Benchmark:** {findings_result['benchmark']}",
        f"**Return observations:** {risk['observations']}",
        "",
        "## Executive Summary",
        "",
        (
            f"The portfolio recorded annualized historical volatility "
            f"of {risk['annualized_volatility']:.2%}, "
            f"maximum drawdown of {abs(risk['maximum_drawdown']):.2%}, "
            f"and beta of {risk['beta']:.2f} against "
            f"{findings_result['benchmark']}."
        ),
        (
        f"Invested holdings have an HHI of "
        f"{concentration['hhi']:.3f}, with the largest position "
        f"representing {concentration['largest_holding_weight']:.2%} "
        "of invested capital. These concentration figures exclude cash."
        if concentration["hhi"] is not None
        else "No invested holdings were available for concentration analysis."
        ),
        "",
        "",
        "## Risk Metrics",
        "",
        "| Metric | Value |",
        "|---|---:|",
        f"| Annualized volatility | {risk['annualized_volatility']:.2%} |",
        f"| Maximum drawdown | {abs(risk['maximum_drawdown']):.2%} |",
        f"| Historical VaR (loss convention) | {risk['historical_var']:.2%} |",
        f"| Historical CVaR (loss convention) | {risk['historical_cvar']:.2%} |",
        f"| Beta vs {findings_result['benchmark']} | {risk['beta']:.2f} |",
        "",
        "## Concentration",
        "",
        f"- Invested holdings: {concentration['holdings_count']}",
    ]

    if concentration["hhi"] is not None:
        lines.extend([
            f"- HHI: {concentration['hhi']:.4f}",
            (
                f"- Effective equally weighted holdings: "
                f"{concentration['effective_holdings']:.2f}"
            ),
            (
                f"- Largest holding weight: "
                f"{concentration['largest_holding_weight']:.2%}"
            ),
        ])

    lines.extend([
        "",
        "**Note:** Concentration weights exclude cash.",
        "",
        "## Risk Findings",
        "",
    ])

    for number, finding in enumerate(findings, start=1):
        lines.extend([
            f"### {number}. {finding['headline']}",
            "",
            finding["interpretation"],
            "",
            f"**Further investigation:** {finding['investigation']}",
            "",
        ])

    lines.extend([
        "## Methodology and Limitations",
        "",
        (
        "- VaR and CVaR use a loss convention: positive values indicate "
        "estimated losses, while negative values indicate gains even "
        "in the lower tail of observed returns."
        ),  
        "- Portfolio returns adjust for external deposits and withdrawals.",
        "- Benchmark returns use raw closing prices, not dividend-adjusted returns.",
        "- Concentration is measured across invested holdings, excluding cash.",
        "- Risk estimates are historical and do not predict future losses.",
        "- Data coverage is validated against stored benchmark trading dates.",
        "- Historical VaR/CVaR reliability depends on the sample size and market conditions.",
        "",
    ])

    return "\n".join(lines)