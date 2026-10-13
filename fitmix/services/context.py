from dataclasses import dataclass


class ServiceError(ValueError):
    def __init__(self, message: str, status: int = 400, code: str = "bad_request"):
        super().__init__(message)
        self.status = status
        self.code = code


@dataclass(frozen=True)
class UserContext:
    """Provided by the host after login, never taken from model/tool arguments."""
    owner_id: str
    role: str

    def __post_init__(self):
        if not isinstance(self.owner_id, str) or not self.owner_id.strip() or self.role not in {"admin", "visitor"}:
            raise ServiceError("用户身份无效", 401, "auth_required")
