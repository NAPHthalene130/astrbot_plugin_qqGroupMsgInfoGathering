import json
from dataclasses import asdict, dataclass
from typing import Any


def _to_string(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False, default=str)
    return str(value)


@dataclass
class windowProcessOutputJson:
    type: str
    subject: str
    result_content: str
    links_and_attachments: str
    source_members: str
    urgency: str

    @classmethod
    def from_dict(cls, raw_item: dict[str, Any]) -> "windowProcessOutputJson":
        return cls(
            type=_to_string(raw_item.get("type")),
            subject=_to_string(raw_item.get("subject")),
            result_content=_to_string(raw_item.get("result_content")),
            links_and_attachments=_to_string(raw_item.get("links_and_attachments")),
            source_members=_to_string(raw_item.get("source_members")),
            urgency=_to_string(raw_item.get("urgency")),
        )

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    def __str__(self) -> str:
        return self.to_json()
