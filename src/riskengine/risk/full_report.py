from riskengine.risk.exposure import get_portfolio_exposure
from riskengine.risk.findings import generate_risk_findings
from riskengine.risk.report_formatter import format_risk_report


def generate_full_risk_report(
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
    Generate a complete portfolio risk report with
    risk metrics, findings, and security/sector exposures.
    """

    findings = generate_risk_findings(
        connection=connection,
        portfolio_id=portfolio_id,
        start_date=start_date,
        end_date=end_date,
        benchmark_ticker=benchmark_ticker,
        confidence=confidence,
        trading_days=trading_days,
        max_age_days=max_age_days,
    )

    exposure = get_portfolio_exposure(
        connection=connection,
        portfolio_id=portfolio_id,
        as_of_date=end_date,
        max_age_days=max_age_days,
    )

    report = format_risk_report(findings)

    lines = [
        "",
        "## Security Exposure",
        "",
        "**Weight basis:** Invested holdings, excluding cash.",
        "",
        "| Security | Sector | Market Value | Weight |",
        "|---|---|---:|---:|",
    ]

    for security in exposure["security_exposures"]:
        lines.append(
            f"| {security['ticker']} "
            f"| {security['sector']} "
            f"| {security['market_value']:,.2f} "
            f"| {security['weight']:.2%} |"
        )

    lines.extend([
        "",
        "## Sector Exposure",
        "",
        "| Sector | Holdings | Market Value | Weight |",
        "|---|---:|---:|---:|",
    ])

    for sector in exposure["sector_exposures"]:
        lines.append(
            f"| {sector['sector']} "
            f"| {sector['holdings_count']} "
            f"| {sector['market_value']:,.2f} "
            f"| {sector['weight']:.2%} |"
        )

    lines.extend([
        "",
        "## Exposure Findings",
        "",
    ])

    largest_security = exposure["largest_security"]
    largest_sector = exposure["largest_sector"]

    if largest_security is None:
        lines.append(
            "No invested security exposure was present "
            "on the reporting date."
        )
    else:
        lines.append(
            f"- **Largest security:** "
            f"{largest_security['ticker']} represents "
            f"{largest_security['weight']:.2%} "
            "of invested holdings."
        )

        lines.append(
            f"- **Largest sector:** "
            f"{largest_sector['sector']} represents "
            f"{largest_sector['weight']:.2%} "
            "of invested holdings."
        )

        lines.append(
            "- **Investigation:** Review whether the largest "
            "security and sector exposures create overlapping "
            "sources of portfolio risk."
        )

    return {
        "portfolio_id": portfolio_id,
        "risk_findings": findings,
        "exposure": exposure,
        "markdown": report + "\n" + "\n".join(lines) + "\n",
    }