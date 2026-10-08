"""Request and validate chat completions for model-generated artifacts."""

from collections.abc import Callable

from openai.types.chat import ChatCompletionMessageParam

from modgud.models import RoutedModelClient


def request_parsed_completion[T](
    routed: RoutedModelClient,
    source_text: str,
    *,
    system_prompt: str,
    parse: Callable[[str], T],
    json_object: bool = False,
) -> T | None:
    """Return a parsed response after at most two malformed responses.

    Request failures propagate. The caller owns the routed client's lifetime.
    """
    messages: list[ChatCompletionMessageParam] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": source_text},
    ]
    for _attempt in range(2):
        if json_object:
            completion = routed.client.chat.completions.create(
                model=routed.model,
                messages=messages,
                response_format={"type": "json_object"},
            )
        else:
            completion = routed.client.chat.completions.create(
                model=routed.model, messages=messages
            )
        try:
            content = completion.choices[0].message.content
            if not isinstance(content, str):
                raise TypeError("model returned no text content")
            return parse(content)
        except (IndexError, TypeError, ValueError):
            continue
    return None
