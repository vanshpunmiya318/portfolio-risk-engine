"""Explicit portfolio management through the application transaction boundary."""

import logging
import unicodedata
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from riskengine.db.application_transactions import MutationRejected, Transaction, TransactionError
from riskengine.db.portfolios import normalise_currency

BASE_CURRENCIES = ("USD", "EUR")
MAX_CLIENT_ID = 2**53 - 1
logger = logging.getLogger("uvicorn.error")


class Portfolio(BaseModel):
    id: int = Field(gt=0, le=MAX_CLIENT_ID)
    name: str
    currency: str


class UnsupportedPortfolio(BaseModel):
    id: str
    reason: Literal["nonpositive", "outside_client_range"]


class PortfolioList(BaseModel):
    portfolios: list[Portfolio]
    unsupported: list[UnsupportedPortfolio]
    context: str
    generation: str
    management_configured: bool
    write_availability: Literal["available", "unavailable", "unknown"]
    supported_currencies: list[str]


class RenamePortfolio(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(max_length=256)


class CreatePortfolio(RenamePortfolio):
    currency: str = Field(max_length=16)


class PreparePortfolio(RenamePortfolio):
    target: int | None = Field(default=None, strict=True, gt=0, le=MAX_CLIENT_ID)
    currency: str | None = Field(default=None, max_length=16)
    context: str = Field(min_length=1, max_length=128)


class UnsupportedGeneratedId(MutationRejected):
    pass


class DuplicateName(MutationRejected):
    pass


class MissingPortfolio(MutationRejected):
    pass


def _name_key(name: str) -> str:
    return unicodedata.normalize("NFC", " ".join(name.split())).casefold()


def validate_name(value: str) -> str:
    if any(
        unicodedata.category(character) in {"Cc", "Cf", "Cs"}
        and character not in "\u200c\u200d" for character in value
    ):
        raise HTTPException(422, "Portfolio names cannot contain unsafe formatting or control characters.")
    name = unicodedata.normalize("NFC", " ".join(value.split()))
    if not 1 <= len(name) <= 80:
        raise HTTPException(422, "Enter a portfolio name between 1 and 80 characters.")
    if not any(character.isprintable() and not character.isspace()
               and unicodedata.category(character)[0] != "M" and character not in "\u2800\u3164\uffa0\u115f\u1160"
               for character in name):
        raise HTTPException(422, "Enter a portfolio name with visible characters.")
    return name


def validate_currency(value: str) -> str:
    currency = normalise_currency(value)
    if currency not in BASE_CURRENCIES:
        raise HTTPException(422, "Choose USD or EUR as the portfolio base currency.")
    return currency


def _require_unique_name(transaction: Transaction, name: str, exclude: int | None = None) -> None:
    key = _name_key(name)
    if any(identity != exclude and _name_key(existing) == key
           for identity, existing in transaction.execute("SELECT id, name FROM portfolios")):
        raise DuplicateName()


def create_portfolio(transaction: Transaction, name: str, currency: str) -> tuple:
    _require_unique_name(transaction, name)
    row = transaction.execute(
        "INSERT INTO portfolios(name, currency) VALUES (?, ?) RETURNING id, name, currency",
        (name, currency),
    )[0]
    if not 0 < row[0] <= MAX_CLIENT_ID:
        raise UnsupportedGeneratedId()
    return row


def rename_portfolio(transaction: Transaction, identity: int, name: str) -> tuple:
    rows = transaction.execute("SELECT id, name, currency FROM portfolios WHERE id = ?", (identity,))
    if not rows:
        raise MissingPortfolio()
    previous = rows[0]
    if previous[1] == name:
        return previous
    _require_unique_name(transaction, name, exclude=identity)
    return transaction.execute(
        "UPDATE portfolios SET name = ? WHERE id = ? RETURNING id, name, currency",
        (name, identity),
    )[0]


def mutation_http_error(error: TransactionError) -> HTTPException:
    if error.outcome in {"unknown", "committed"}:
        logger.error("Portfolio management requires reconciliation (%s, %s).", error.reason, error.outcome,
                     exc_info=error)
        return HTTPException(500, {
            "code": "reconcile",
            "message": "The change may have been saved. Refresh the portfolio list and check before trying again.",
        })
    if isinstance(error.__cause__, MissingPortfolio):
        return HTTPException(404, "Portfolio not found.")
    if isinstance(error.__cause__, UnsupportedGeneratedId):
        return HTTPException(409, "A portfolio ID supported by this browser could not be allocated.")
    if isinstance(error.__cause__, DuplicateName):
        return HTTPException(409, "A portfolio with that name already exists.")
    if error.reason == "authorization_denied":
        return HTTPException(403, "Portfolio changes require a writable managed database.")
    if error.reason == "lock_contention":
        return HTTPException(503, "The database is busy. Try again after the other operation finishes.")
    if error.reason == "recovery_required":
        return HTTPException(503, "The managed database needs recovery before changes can be saved.")
    logger.error("Portfolio management failed (%s).", error.reason, exc_info=error)
    return HTTPException(500, "The portfolio change could not be saved.")
