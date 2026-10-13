import json
import os
from typing import Dict, List, Optional
from models import User, VoteSession


DATA_FILE = "data.json"


class Storage:
    def __init__(self, filepath: str = DATA_FILE):
        self.filepath = filepath
        self._ensure_file()
        self.data = self._load()

    def _ensure_file(self):
        if not os.path.exists(self.filepath):
            with open(self.filepath, "w", encoding="utf-8") as f:
                json.dump({"users": {}, "sessions": []}, f, ensure_ascii=False, indent=2)

    def _load(self):
        with open(self.filepath, "r", encoding="utf-8") as f:
            return json.load(f)

    def _save(self):
        with open(self.filepath, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)

    def get_user(self, user_id: int) -> Optional[User]:
        u = self.data["users"].get(str(user_id))
        if u:
            return User(**{**u, "nickname": u.get("nickname", "")})
        return None

    def set_user(self, user: User):
        self.data["users"][str(user.id)] = {
            "id": user.id,
            "username": user.username,
            "group": user.group,
            "approved": user.approved,
            "nickname": user.nickname,
        }
        self._save()

    def get_all_users(self) -> List[User]:
        return [
            User(**{**u, "nickname": u.get("nickname", "")})
            for u in self.data["users"].values()
        ]

    def get_approved_users(self) -> List[User]:
        return [u for u in self.get_all_users() if u.approved]

    def create_session(self, session: VoteSession):
        self.data["sessions"].append(session.to_dict())
        self._save()

    def get_session(self, session_id: str) -> Optional[VoteSession]:
        for s in self.data["sessions"]:
            if s["id"] == session_id:
                return VoteSession.from_dict(s)
        return None

    def update_session(self, session: VoteSession):
        for i, s in enumerate(self.data["sessions"]):
            if s["id"] == session.id:
                self.data["sessions"][i] = session.to_dict()
                break
        self._save()

    def get_all_sessions(self) -> List[VoteSession]:
        return [VoteSession.from_dict(s) for s in self.data["sessions"]]

    def get_active_sessions(self) -> List[VoteSession]:
        return [s for s in self.get_all_sessions() if s.is_active and not s.is_finished]

    def delete_session(self, session_id: str) -> bool:
        for i, s in enumerate(self.data["sessions"]):
            if s["id"] == session_id:
                self.data["sessions"].pop(i)
                self._save()
                return True
        return False

    def reset_all(self):
        self.data = {"users": {}, "sessions": []}
        self._save()
