"""Accept signed Label links and append confirmed opinions to Item history."""

from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path

from modgud.config import SecretValue
from modgud.database import connect
from modgud.events import ItemLog
from modgud.label_tokens import (
    ExpiredLabelToken,
    InvalidLabelToken,
    WrongItemLabelToken,
    WrongOpinionLabelToken,
    validate_label_token,
)

_OPINION_NAMES = {"worth-it": "Worth it", "not-worth-it": "Not worth it"}


class LabelFailure(Enum):
    """Semantic reasons a Label link cannot be accepted."""

    UNKNOWN_OPINION = "unknown_opinion"
    INVALID_LINK = "invalid_link"
    WRONG_ITEM = "wrong_item"
    WRONG_OPINION = "wrong_opinion"
    EXPIRED_LINK = "expired_link"
    MISSING_ITEM = "missing_item"


class LabelRejected(ValueError):
    """A rejected link, with a reason independent of presentation wording."""

    def __init__(self, reason: LabelFailure) -> None:
        super().__init__(reason.value)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class LabelItem:
    """The Item and opinion resolved from an accepted Label link."""

    item_id: int
    title: str
    url: str
    opinion: str
    opinion_name: str


class LabelLinks:
    """Confirm without writing, or record one opinion in its own transaction.

    Recognition and capability validation precede opening the database. Every
    accepted record appends an event, including repeated uses of the same link.
    Expected rejections raise ``LabelRejected``; storage failures propagate.
    """

    def __init__(
        self,
        database: str | Path,
        *,
        signing_secret: SecretValue | None,
        clock: Callable[[], datetime],
    ) -> None:
        self._database = database
        self._signing_secret = signing_secret
        self._clock = clock

    def confirm(self, item_id: int, opinion: str, token: str | None) -> LabelItem:
        """Resolve a valid link for confirmation without appending a Label."""
        return self._accept(item_id, opinion, token, record=False)

    def record(self, item_id: int, opinion: str, token: str | None) -> LabelItem:
        """Resolve a valid link and append its Label to the Item's history."""
        return self._accept(item_id, opinion, token, record=True)

    def _accept(
        self, item_id: int, opinion: str, token: str | None, *, record: bool
    ) -> LabelItem:
        opinion_name = _OPINION_NAMES.get(opinion)
        if opinion_name is None:
            raise LabelRejected(LabelFailure.UNKNOWN_OPINION)
        if self._signing_secret is None or token is None:
            raise LabelRejected(LabelFailure.INVALID_LINK)
        try:
            validate_label_token(
                token,
                item_id,
                opinion,
                signing_secret=self._signing_secret,
                now=self._clock(),
            )
        except WrongItemLabelToken as error:
            raise LabelRejected(LabelFailure.WRONG_ITEM) from error
        except WrongOpinionLabelToken as error:
            raise LabelRejected(LabelFailure.WRONG_OPINION) from error
        except ExpiredLabelToken as error:
            raise LabelRejected(LabelFailure.EXPIRED_LINK) from error
        except InvalidLabelToken as error:
            raise LabelRejected(LabelFailure.INVALID_LINK) from error

        with closing(connect(self._database)) as connection, connection:
            row = connection.execute(
                """
                SELECT coalesce(title, page_url, canonical_url),
                       coalesce(page_url, canonical_url)
                FROM items
                WHERE id = ?
                """,
                (item_id,),
            ).fetchone()
            if row is None:
                raise LabelRejected(LabelFailure.MISSING_ITEM)
            if record:
                ItemLog(connection, item_id).label(opinion)
        return LabelItem(
            item_id=item_id,
            title=str(row[0]),
            url=str(row[1]),
            opinion=opinion,
            opinion_name=opinion_name,
        )
