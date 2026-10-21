from datetime import date, timedelta


def validate_corporate_action_coverage(
    connection,
    instrument_id,
    start_date,
    end_date,
):
    start = date.fromisoformat(start_date)
    end = date.fromisoformat(end_date)

    if start > end:
        raise ValueError("Invalid coverage date range")

    rows = connection.execute(
        """
        SELECT start_date, end_date
        FROM corporate_action_coverage
        WHERE instrument_id = ?
          AND source = 'tiingo'
          AND end_date >= ?
          AND start_date <= ?
        ORDER BY start_date, end_date
        """,
        (instrument_id, start_date, end_date),
    ).fetchall()

    covered_until = start - timedelta(days=1)

    for row_start, row_end in rows:
        interval_start = date.fromisoformat(row_start)
        interval_end = date.fromisoformat(row_end)

        if interval_start > covered_until + timedelta(days=1):
            raise ValueError(
                "Incomplete corporate action coverage"
            )

        covered_until = max(covered_until, interval_end)

        if covered_until >= end:
            return True

    raise ValueError("Incomplete corporate action coverage")


