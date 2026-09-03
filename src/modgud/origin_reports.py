"""Plain-text reporting for capture-origin label outcomes."""

import sqlite3
from dataclasses import dataclass

_MANUAL_ORIGIN = "manual"
_MINIMUM_LABELLED_ITEMS = 5

_GROUPED_OUTCOMES = """
    WITH ranked_captures AS (
        SELECT item_id,
               CASE
                   WHEN json_type(payload, '$.origin') = 'text'
                   THEN nullif(trim(json_extract(payload, '$.origin')), '')
               END AS origin,
               row_number() OVER (
                   PARTITION BY item_id ORDER BY id
               ) AS capture_number
        FROM events
        WHERE type = 'captured'
    ),
    first_captures AS (
        SELECT item_id, origin
        FROM ranked_captures
        WHERE capture_number = 1
    ),
    ranked_labels AS (
        SELECT item_id,
               CASE json_extract(payload, '$.label')
                   WHEN 'worth-it' THEN 'worth-it'
                   WHEN 'not-worth-it' THEN 'not-worth-it'
               END AS label,
               row_number() OVER (
                   PARTITION BY item_id ORDER BY id DESC
               ) AS label_number
        FROM events
        WHERE type = 'label'
    ),
    latest_labels AS (
        SELECT item_id, label
        FROM ranked_labels
        WHERE label_number = 1
    )
    SELECT first_captures.origin,
           count(*) AS item_count,
           sum(coalesce(latest_labels.label = 'worth-it', 0)) AS worth_it,
           sum(coalesce(latest_labels.label = 'not-worth-it', 0)) AS not_worth_it,
           sum(latest_labels.label IS NULL) AS unlabelled
    FROM items
    LEFT JOIN first_captures ON first_captures.item_id = items.id
    LEFT JOIN latest_labels ON latest_labels.item_id = items.id
    GROUP BY first_captures.origin
"""


@dataclass(frozen=True, slots=True)
class _OriginGroup:
    origin: str | None
    item_count: int
    worth_it: int
    not_worth_it: int
    unlabelled: int

    @property
    def labelled(self) -> int:
        return self.worth_it + self.not_worth_it


def _load_groups(connection: sqlite3.Connection) -> list[_OriginGroup]:
    return [
        _OriginGroup(
            origin=str(row[0]) if row[0] is not None else None,
            item_count=int(row[1]),
            worth_it=int(row[2]),
            not_worth_it=int(row[3]),
            unlabelled=int(row[4]),
        )
        for row in connection.execute(_GROUPED_OUTCOMES).fetchall()
    ]


def _display_order(
    group: _OriginGroup,
    ranks: dict[str | None, int],
) -> tuple[int, int, str]:
    if group.origin in ranks:
        return (0, ranks[group.origin], group.origin or "")
    if group.origin not in {None, _MANUAL_ORIGIN}:
        return (1, 0, group.origin)
    if group.origin == _MANUAL_ORIGIN:
        return (2, 0, group.origin)
    return (3, 0, "")


def _format_table(rows: list[tuple[str, ...]]) -> list[str]:
    headers = (
        "Rank",
        "Origin",
        "Items",
        "Worth it",
        "Not worth it",
        "Unlabelled",
        "Result",
    )
    widths = [
        max([len(header), *(len(row[index]) for row in rows)])
        for index, header in enumerate(headers)
    ]
    return [
        "  ".join(
            value.ljust(widths[index]) for index, value in enumerate(row)
        ).rstrip()
        for row in (headers, *rows)
    ]


def render_origin_report(connection: sqlite3.Connection) -> str:
    """Render label outcomes for each item's original capture source."""
    groups = _load_groups(connection)
    total = sum(group.item_count for group in groups)
    recorded = sum(group.item_count for group in groups if group.origin is not None)
    manual = sum(group.item_count for group in groups if group.origin == _MANUAL_ORIGIN)
    coverage = 100.0 * recorded / total if total else 0.0
    manual_share = 100.0 * manual / total if total else 0.0

    rankable = [
        group
        for group in groups
        if group.origin not in {None, _MANUAL_ORIGIN}
        and group.labelled >= _MINIMUM_LABELLED_ITEMS
    ]
    rankable.sort(
        key=lambda group: (
            -(group.not_worth_it / group.labelled),
            -group.labelled,
            group.origin or "",
        )
    )
    ranks = {group.origin: rank for rank, group in enumerate(rankable, start=1)}

    table_rows: list[tuple[str, ...]] = []
    for group in sorted(groups, key=lambda group: _display_order(group, ranks)):
        if group.origin in ranks:
            result = f"{100.0 * group.not_worth_it / group.labelled:.1f}% not worth it"
            rank = str(ranks[group.origin])
        elif group.origin == _MANUAL_ORIGIN:
            result = "Manual capture (not ranked)"
            rank = "-"
        elif group.origin is None:
            result = "Origin not recorded"
            rank = "-"
        else:
            result = (
                f"Too little data ({group.labelled}/{_MINIMUM_LABELLED_ITEMS} labelled)"
            )
            rank = "-"
        table_rows.append(
            (
                rank,
                group.origin or "(unknown)",
                str(group.item_count),
                str(group.worth_it),
                str(group.not_worth_it),
                str(group.unlabelled),
                result,
            )
        )

    return "\n".join(
        (
            f"Origin coverage: {recorded}/{total} items ({coverage:.1f}%)",
            f"Manual captures: {manual}/{total} items ({manual_share:.1f}%)",
            "",
            *_format_table(table_rows),
        )
    )
