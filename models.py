from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional


@dataclass
class User:
    id: int
    username: str
    group: str  # "A" - больший вес, "B" - меньший вес
    approved: bool = False
    nickname: str = ""


@dataclass
class VoteItem:
    name: str


@dataclass
class Vote:
    user_id: int
    rankings: Dict[str, int]  # item_name -> rank (1-based)


@dataclass
class VoteSession:
    id: str
    name: str
    items: List[VoteItem]
    votes: List[Vote] = field(default_factory=list)
    is_active: bool = True
    is_finished: bool = False
    results_published: bool = False

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "items": [{"name": i.name} for i in self.items],
            "votes": [
                {"user_id": v.user_id, "rankings": v.rankings} for v in self.votes
            ],
            "is_active": self.is_active,
            "is_finished": self.is_finished,
            "results_published": self.results_published,
        }

    @classmethod
    def from_dict(cls, data):
        items = [VoteItem(**i) for i in data["items"]]
        votes = [
            Vote(user_id=v["user_id"], rankings=v["rankings"])
            for v in data.get("votes", [])
        ]
        return cls(
            id=data["id"],
            name=data["name"],
            items=items,
            votes=votes,
            is_active=data.get("is_active", True),
            is_finished=data.get("is_finished", False),
            results_published=data.get("results_published", False),
        )
