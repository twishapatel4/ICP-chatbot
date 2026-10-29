"""What we know about a company at signup, before the first question."""

from dataclasses import asdict, dataclass, fields

FREE_MAIL_DOMAINS = frozenset(
    {
        "gmail.com", "googlemail.com", "yahoo.com", "yahoo.in", "outlook.com", "hotmail.com",
        "live.com", "icloud.com", "me.com", "aol.com", "proton.me", "protonmail.com",
        "gmx.com", "mail.com", "zoho.com", "rediffmail.com",
    }
)  # fmt: skip


@dataclass(frozen=True)
class SignupInfo:
    company_name: str
    website: str | None = None
    email: str | None = None
    role: str | None = None
    industry: str | None = None
    company_size: str | None = None
    source: str | None = None

    @property
    def email_domain(self) -> str | None:
        if self.email and "@" in self.email:
            return self.email.rsplit("@", 1)[1].strip().lower() or None
        return None

    @property
    def business_domain(self) -> str | None:
        """The work-email domain, or None for free-mail addresses."""
        domain = self.email_domain
        return None if domain is None or domain in FREE_MAIL_DOMAINS else domain

    @property
    def has_context(self) -> bool:
        return any((self.website, self.role, self.industry, self.company_size, self.source))

    def stored(self) -> dict[str, str | None]:
        """The fields to keep on the session. Of the email only "@domain" is kept: the domain
        is all the website read needs, and the address would otherwise be copied into every
        saved turn."""
        data = asdict(self)
        data["email"] = f"@{self.email_domain}" if self.email_domain else None
        return data

    @classmethod
    def from_stored(cls, data: dict) -> "SignupInfo":
        names = {f.name for f in fields(cls)}
        known = {k: v for k, v in data.items() if k in names}
        return cls(**{"company_name": "", **known})
